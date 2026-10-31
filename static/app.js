// SettlementResolver dashboard.
// Every animation is triggered by a real agent event from /ws/events.

const { animate, createTimeline, stagger, svg, utils } = anime;
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const ms = (d) => (REDUCED ? 0 : d);

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const inr = (n) => Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 });

const COLORS = { active: '#206bc4', ok: '#2fb344', warn: '#f59f00', bad: '#d63939' };
const TOOL_ICON = {
  lookup_merchant: 'ti-id', check_payment_status: 'ti-credit-card', find_txn_by_amount_time: 'ti-search',
  get_settlement_status: 'ti-book', retry_settlement: 'ti-repeat', trigger_bank_reconciliation: 'ti-building-bank',
  reverse_duplicate_debit: 'ti-arrow-back-up', issue_instant_credit: 'ti-coin-rupee', escalate_to_human: 'ti-user-up',
};

let batchMode = false;
let auditTimer = null;

// ---------- error reporting (always on: lands in logs/errors.log) ----------

function reportError(where, message, stack = '', traceId = '') {
  console.error(`[${where}]`, message);
  fetch('/api/debug/client-error', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ where, message: String(message), stack: String(stack || ''), trace_id: traceId || '' }) }).catch(() => {});
}
window.addEventListener('error', (e) => reportError('js error', e.message, e.error?.stack || `${e.filename}:${e.lineno}`));
window.addEventListener('unhandledrejection', (e) => reportError('unhandled promise', e.reason?.message || e.reason, e.reason?.stack));

// ---------- detailed timing log ----------

const debugLog = { enabled: false, server: new Map(), blocks: new Map() };

// Browser-side timeline; t=0 is the button press / send click.
class ClientTimeline {
  constructor(kind) { this.kind = kind; this.t0 = performance.now(); this.events = []; this.traceId = null; }
  mark(step, detail = '', ms = null) {
    const at = Math.round(performance.now() - this.t0);
    this.events.push({ source: 'browser', step, at_ms: at, ms: ms == null ? null : Math.round(ms), detail: String(detail) });
    if (debugLog.enabled) console.log(`[TIMING] browser +${at}ms${ms != null ? ` (${Math.round(ms)}ms)` : ''} ${step} ${detail}`);
    return at;
  }
  at(step) { return this.events.find((e) => e.step === step)?.at_ms; }
}

function durClass(ms) { return ms >= 3000 ? 'very-slow' : ms >= 1000 ? 'slow' : ''; }

function renderDebugBlock(traceId) {
  if (!debugLog.enabled) return;
  const block = debugLog.blocks.get(traceId) || { kind: 'ticket', client: null };
  debugLog.blocks.set(traceId, block);
  const server = debugLog.server.get(traceId) || [];
  // Server clock starts when the request arrives; place it at the browser's "request sent" time.
  const offset = block.client?.at('request sent') ?? 0;
  const rows = [
    ...(block.client?.events || []),
    ...server.map((e) => ({ ...e, at_ms: e.at_ms + offset })),
  ].sort((a, b) => a.at_ms - b.at_ms);
  const total = rows.length ? rows[rows.length - 1].at_ms : 0;
  const slowest = rows.filter((r) => r.ms != null).sort((a, b) => b.ms - a.ms)[0];

  $('.debug-empty')?.remove();
  let el = document.getElementById(`dbg-${traceId}`);
  if (!el) {
    el = document.createElement('div');
    el.id = `dbg-${traceId}`;
    el.className = 'debug-block';
    $('#debug-log').prepend(el);
    while ($('#debug-log').children.length > 15) $('#debug-log').lastChild.remove();
  }
  el.innerHTML = `
    <div class="d-flex gap-2 align-items-baseline mb-1">
      <b>${esc(traceId)}</b><span class="badge bg-azure-lt">${esc(block.kind)}</span>
      <span class="text-secondary small">total ${(total / 1000).toFixed(2)}s${slowest ? ` · slowest: ${esc(slowest.step)} ${(slowest.ms / 1000).toFixed(2)}s` : ''}</span>
    </div>
    <table><tbody>${rows.map((r) => `
      <tr class="${r.source}"><td class="text-secondary">+${(r.at_ms / 1000).toFixed(2)}s</td>
        <td class="${r.ms != null ? durClass(r.ms) : ''}">${r.ms != null ? `${r.ms}ms` : ''}</td>
        <td class="step">${esc(r.source === 'browser' ? 'browser' : 'server')} · ${esc(r.step)}</td>
        <td class="detail">${esc(r.detail)}</td></tr>`).join('')}
    </tbody></table>`;
}

// Attach a finished browser timeline to its trace id, show it and send it to the server log.
function finishClientTimeline(tl, traceId, serverEvents) {
  if (!debugLog.enabled || !traceId) return;
  tl.traceId = traceId;
  if (serverEvents) debugLog.server.set(traceId, serverEvents);
  debugLog.blocks.set(traceId, { kind: tl.kind, client: tl });
  renderDebugBlock(traceId);
}

function sendClientTimeline(tl) {
  if (!debugLog.enabled || !tl.traceId) return;
  renderDebugBlock(tl.traceId);
  fetch('/api/debug/client-timing', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ trace_id: tl.traceId, events: tl.events }) }).catch(() => {});
}

function setDebug(enabled, push = true) {
  debugLog.enabled = enabled;
  $('#debug-toggle').checked = enabled;
  $('#debug-row').hidden = !enabled;
  if (push) fetch('/api/debug', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }) });
}

$('#debug-toggle').addEventListener('change', (e) => setDebug(e.target.checked));
$('#debug-clear').addEventListener('click', () => {
  debugLog.server.clear(); debugLog.blocks.clear();
  $('#debug-log').innerHTML = '<div class="text-secondary debug-empty">Cleared.</div>';
});
$('#debug-copy').addEventListener('click', () => {
  const text = [...document.querySelectorAll('.debug-block')].map((b) =>
    [...b.querySelectorAll('tr')].map((tr) => [...tr.children].map((td) => td.textContent.trim()).join('\t')).join('\n')
      .replace(/^/, `${b.querySelector('b').textContent}\n`)).join('\n\n');
  navigator.clipboard.writeText(text);
});

// ---------- system map animations ----------

function node(name) { return document.querySelector(`#map [data-node="${name}"]`); }

function setNodeState(name, state) {
  const el = node(name);
  if (!el) return;
  el.classList.remove('is-active', 'is-ok', 'is-warn', 'is-bad');
  if (state) el.classList.add(`is-${state}`);
}

function pulse(name) {
  const el = node(name);
  if (el) animate(el, { scale: [1, 1.08, 1], duration: ms(450), ease: 'outQuad' });
}

// Draw a coloured line along an edge and send a packet along it.
function sendPacket(edgeId, color = COLORS.active, reverse = false) {
  const base = document.getElementById(edgeId);
  if (!base || REDUCED) return;
  const map = $('#map');
  const live = base.cloneNode();
  live.removeAttribute('id');
  live.setAttribute('class', 'edge-live');
  live.style.stroke = color;
  map.insertBefore(live, map.querySelector('.node'));
  const [drawable] = svg.createDrawable(live);
  const fade = () => animate(live, { opacity: 0, duration: 400, onComplete: () => live.remove() });

  // Results flow back: just draw the line from the system end
  if (reverse) {
    animate(drawable, { draw: ['1 1', '0 1'], duration: 380, ease: 'inOutQuad', onComplete: fade });
    return;
  }
  const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  dot.setAttribute('r', 6);
  dot.setAttribute('class', 'packet');
  dot.style.fill = color;
  map.appendChild(dot);
  createTimeline({ onComplete: () => { dot.remove(); fade(); } })
    .add(drawable, { draw: ['0 0', '0 1'], duration: 280, ease: 'inOutQuad' })
    .add(dot, { ...svg.createMotionPath(base), duration: 420, ease: 'inOutSine' }, '-=200');
}

function shield(decision) {
  const el = $('#shield');
  el.classList.remove('ok', 'warn', 'bad');
  const cls = { ALLOW: 'ok', NEEDS_APPROVAL: 'warn', BLOCK: 'bad' }[decision];
  el.classList.add(cls);
  if (decision === 'BLOCK') {
    animate(el, { x: [0, -6, 6, -4, 4, 0], duration: ms(420), ease: 'linear' });
  } else {
    animate(el, { scale: [1, 1.35, 1], duration: ms(450), ease: 'outBack' });
  }
  if (decision === 'BLOCK') animate(node('credits'), { x: [0, -6, 6, -4, 4, 0], duration: ms(420) });
}

function resetMap() {
  ['ticket', 'agent', 'payments', 'ledger', 'bank', 'credits', 'crm'].forEach((n) => setNodeState(n, null));
  $('#shield').classList.remove('ok', 'warn', 'bad');
}

// ---------- trace ----------

function addTrace(icon, html, cls = '') {
  if (batchMode && cls === 'reasoning') return;
  const li = document.createElement('li');
  li.className = cls;
  li.innerHTML = `<i class="ti ${icon}"></i><div>${html}</div>`;
  const list = $('#trace');
  list.prepend(li);
  while (list.children.length > 80) list.lastChild.remove();
  animate(li, { opacity: [0, 1], x: [-14, 0], duration: ms(300), ease: 'outQuad' });
}

function summarize(result) {
  if (!result) return '';
  const keys = ['error', 'status', 'settlement_status', 'bank_status', 'new_utr', 'utr', 'refund_ref', 'credit_id',
    'is_duplicate_debit', 'tat_breached', 'compensation_due'];
  const parts = keys.filter((k) => result[k] !== undefined && result[k] !== null && result[k] !== false)
    .map((k) => `${k}=<b>${esc(result[k])}</b>`);
  if (result.matches) parts.push(`matches=<b>${result.matches.map((m) => m.txn_id).join(', ') || 'none'}</b>`);
  if (result.why) parts.push(`why: ${esc(result.why.join('; '))}`);
  return parts.join(' · ');
}

const DECISION_BADGE = {
  ALLOW: '<span class="badge bg-green-lt">allowed</span>',
  NEEDS_APPROVAL: '<span class="badge bg-yellow-lt">needs approval</span>',
  BLOCK: '<span class="badge bg-red-lt">blocked</span>',
  RESOLVED: '<span class="badge bg-green-lt">resolved</span>',
  AWAITING_APPROVAL: '<span class="badge bg-yellow-lt">awaiting approval</span>',
  ESCALATED: '<span class="badge bg-red-lt">escalated</span>',
};

// ---------- KPIs ----------

const kpiValues = {};
function updateKpis(m) {
  if (!m) return;
  document.querySelectorAll('.kpi').forEach((el) => {
    const key = el.dataset.kpi;
    const to = Number(m[key] || 0);
    const from = kpiValues[key] || 0;
    kpiValues[key] = to;
    if (from === to) return;
    const decimals = Number(el.dataset.decimals || 0);
    const fmt = (v) => (el.dataset.format === 'inr' ? inr(v) : v.toFixed(decimals));
    const counter = { v: from };
    animate(counter, { v: to, duration: ms(700), ease: 'outExpo', onUpdate: () => { el.textContent = fmt(counter.v); },
      onComplete: () => { el.textContent = fmt(to); } });
    // Animations pause while the page is in the background; make sure the real number still shows
    setTimeout(() => { if (kpiValues[key] === to) el.textContent = fmt(to); }, 900);
  });
}

// ---------- approvals ----------

function renderApproval(a) {
  $('.empty-note')?.remove();
  const args = typeof a.args === 'string' ? JSON.parse(a.args) : a.args;
  const reasons = typeof a.reasons === 'string' ? JSON.parse(a.reasons) : a.reasons;
  const card = document.createElement('div');
  card.className = 'approval';
  card.id = `approval-${a.id}`;
  card.innerHTML = `
    <div class="d-flex justify-content-between align-items-start">
      <div><div class="fw-bold">₹${inr(args.amount)} instant credit</div>
        <div class="text-secondary small">${esc(a.ticket_id)} · ${esc(a.merchant_id)} · ${esc(args.txn_id)}</div></div>
      <span class="badge bg-yellow-lt">pending</span>
    </div>
    <div class="small mt-2"><b>Agent:</b> ${esc(a.agent_reason)}</div>
    <div class="small text-warning">${reasons.map(esc).join('<br>')}</div>
    <div class="d-flex gap-2 mt-2">
      <button class="btn btn-success btn-sm" data-approve="${a.id}"><i class="ti ti-check me-1"></i>Approve</button>
      <button class="btn btn-outline-danger btn-sm" data-reject="${a.id}"><i class="ti ti-x me-1"></i>Reject</button>
    </div>`;
  $('#approvals').prepend(card);
  animate(card, { opacity: [0, 1], y: [-12, 0], duration: ms(350), ease: 'outBack' });
  updateApprovalCount();
}

function removeApproval(id, approved) {
  const card = document.getElementById(`approval-${id}`);
  if (!card) return;
  animate(card, { opacity: 0, x: approved ? 60 : -60, duration: ms(350), ease: 'inQuad',
    onComplete: () => { card.remove(); updateApprovalCount(); } });
}

function updateApprovalCount() {
  const n = document.querySelectorAll('.approval').length;
  $('#approval-count').textContent = n;
  if (!n && !$('.empty-note')) $('#approvals').innerHTML = '<div class="empty-note text-secondary">Nothing waiting. Over-cap credits land here.</div>';
}

$('#approvals').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-approve], button[data-reject]');
  if (!btn) return;
  const approve = !!btn.dataset.approve;
  const id = btn.dataset.approve || btn.dataset.reject;
  btn.disabled = true;
  const res = await fetch(`/api/approvals/${id}/${approve ? 'approve' : 'reject'}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  if (!res.ok) { addTrace('ti-alert-triangle text-danger', esc((await res.json()).detail)); btn.disabled = false; }
});

// ---------- batch grid ----------

function buildBatchGrid(ids) {
  const grid = $('#batch-grid');
  grid.innerHTML = ids.map((id) => `<div class="tile" data-ticket="${id}" title="${id}"></div>`).join('');
  animate('.tile', { scale: [0, 1], opacity: [0, 1], delay: stagger(12, { grid: [10, 5], from: 'center' }), duration: ms(400) });
}

function markTile(ticketId, status) {
  const tile = document.querySelector(`.tile[data-ticket="${ticketId}"]`);
  if (!tile) return;
  tile.classList.remove('running', 'ok', 'warn', 'bad');
  tile.classList.add({ RESOLVED: 'ok', AWAITING_APPROVAL: 'warn', NEEDS_INFO: 'warn', ESCALATED: 'bad', running: 'running' }[status] || 'running');
  animate(tile, { scale: [1.4, 1], duration: ms(300), ease: 'outBack' });
}

// ---------- audit table ----------

async function refreshAudit() {
  const rows = await (await fetch('/api/audit')).json();
  $('#audit').innerHTML = rows.slice(0, 60).map((r) => `
    <tr><td class="text-secondary">${new Date(r.ts * 1000).toLocaleTimeString()}</td>
      <td>${esc(r.ticket_id)}</td><td>${esc(r.actor)}</td>
      <td>${r.tool ? `<code>${esc(r.tool)}</code>` : esc(r.event)}</td>
      <td>${DECISION_BADGE[r.decision] || esc(r.decision)}</td>
      <td class="reason">${esc(r.reason)}</td></tr>`).join('');
}
function scheduleAudit() { clearTimeout(auditTimer); auditTimer = setTimeout(refreshAudit, 250); }

// ---------- chat ----------

function bubble(who, text, meta = '') {
  const el = document.createElement('div');
  el.className = `bubble ${who}`;
  // Links in replies (e.g. payment links); a sentence's full stop or comma is not part of the URL
  const linked = esc(text).replace(/(https?:\/\/[^\s<]+?)([.,;:!?)]*)(?=\s|$)/g,
    '<a href="$1" target="_blank" rel="noopener">$1</a>$2');
  el.innerHTML = `${linked}${meta ? `<span class="meta">${esc(meta)}</span>` : ''}`;
  $('#chat').appendChild(el);
  $('#chat').scrollTop = $('#chat').scrollHeight;
  animate(el, { opacity: [0, 1], y: [10, 0], duration: ms(250), ease: 'outQuad' });
  return el;
}

// ▶ Play button on a chat bubble for its recording (your voice, or the AI's spoken reply)
function addPlayButton(el, url, label) {
  if (!url) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'play-rec';
  btn.innerHTML = `<i class="ti ti-player-play"></i> ${label}`;
  let audio = null;
  btn.addEventListener('click', () => {
    if (audio && !audio.paused) { audio.pause(); return; }
    audio = audio || new Audio(url);
    audio.onplay = () => { btn.innerHTML = `<i class="ti ti-player-pause"></i> ${label}`; };
    audio.onpause = audio.onended = () => { btn.innerHTML = `<i class="ti ti-player-play"></i> ${label}`; };
    audio.onerror = () => {
      reportError('recording playback', `${audio.error?.code}: ${audio.error?.message || 'cannot play'} (${url})`);
      btn.outerHTML = `<a class="play-rec" href="${url}" download><i class="ti ti-download"></i> download recording</a>`;
    };
    audio.play().catch((e) => reportError('recording playback', `${e.name}: ${e.message} (${url})`));
  });
  (el.querySelector('.meta') || el).before(btn);
}

// Plays the reply and records when audio actually starts and ends; then ships the browser timeline.
function playAudio(b64, tl) {
  if (!b64) { tl?.mark('no audio to play'); if (tl) sendClientTimeline(tl); return; }
  const decodeStart = performance.now();
  const audio = new Audio(`data:audio/wav;base64,${b64}`);
  tl?.mark('audio decoded', `${Math.round(b64.length * 0.75 / 1024)} KB`, performance.now() - decodeStart);
  audio.addEventListener('playing', () => tl?.mark('audio playing', `${audio.duration.toFixed(1)}s of speech`), { once: true });
  audio.addEventListener('ended', () => { tl?.mark('audio finished'); if (tl) sendClientTimeline(tl); }, { once: true });
  audio.play().catch((err) => {
    tl?.mark('audio blocked', err.message);
    reportError('audio playback', err.message, '', tl?.traceId);
    if (tl) sendClientTimeline(tl);
  });
}

// ---------- conversation (memory) ----------
// One conversation per merchant session; the server keeps the transcript and gives the agent earlier turns.
let conversationId = null;
// Bumped by "New": a reply still in flight from the previous conversation is then ignored,
// instead of landing in the new chat and switching it back to the old conversation.
let chatEpoch = 0;

function setConversation(id) {
  conversationId = id;
  const badge = $('#conv-badge');
  badge.textContent = id || 'new';
  badge.className = `badge ${id ? 'bg-azure-lt' : 'bg-secondary-lt'}`;
}

function newConversation() {
  chatEpoch += 1;
  setConversation(null);
  recordedTurns = 0;
  stopCallAudio();
  $('#btn-call-rec').disabled = true;
  $('#chat').innerHTML = '<div class="chat-note"><i class="ti ti-plus me-1"></i>New conversation started. '
    + 'The AI won\'t see the earlier messages.</div>';
  animate('#conv-badge', { scale: [1.35, 1], duration: ms(350), ease: 'outBack' });
  animate('.chat-note', { opacity: [0, 1], y: [-6, 0], duration: ms(250) });
}

$('#btn-new-conv').addEventListener('click', () => { if (callInvoice) endCall(); newConversation(); });

// ---------- collections call: merchant A's AI calls merchant B (you play merchant B) ----------
let callInvoice = null;

function updateCallBanner(inv) {
  const status = { OPEN: 'open', PROMISED: `promised ₹${inr(inv.promise_amount)} by ${inv.promise_date}`,
    PAID: 'PAID ✓', DISPUTED: 'disputed, handed to merchant A' }[inv.status] || inv.status;
  $('#call-banner').innerHTML = `
    <div class="d-flex align-items-center gap-2">
      <span><span class="live-dot"></span><b>AI call</b> for ${esc(inv.creditor)} → ${esc(inv.debtor)}</span>
      <button class="btn btn-sm btn-outline-danger ms-auto" id="btn-end-call"><i class="ti ti-phone-off me-1"></i>End call</button>
    </div>
    <div class="small text-secondary">You are <b>${esc(inv.debtor)}</b> · ${esc(inv.invoice_id)} · ₹${inr(inv.outstanding)} outstanding · <b>${esc(status)}</b></div>`;
  $('#btn-end-call').addEventListener('click', endCall);
}

async function startCall(invoiceId) {
  newConversation();
  const res = await fetch(`/api/collections/${invoiceId}/call`, { method: 'POST' });
  const data = await res.json();
  if (!res.ok) { bubble('agent', `Can't start the call: ${data.detail}`); return; }
  callInvoice = invoiceId;
  setConversation(data.conversation_id);
  $('#call-banner').hidden = false;
  updateCallBanner(data.invoice);
  $('#merchant').disabled = true;
  document.querySelectorAll('[data-scenario]').forEach((b) => { b.disabled = true; });
  animate('#call-banner', { opacity: [0, 1], y: [-8, 0], duration: ms(300) });
  const greet = bubble('agent', data.greeting, `${data.invoice.creditor}'s AI · call started`);
  addPlayButton(greet, data.agent_audio_url, 'replay AI');
  noteRecorded(data);
  playAudio(data.audio_b64);
  addTrace('ti-phone-call', `AI calling <b>${esc(data.invoice.debtor)}</b> for ${esc(data.invoice.creditor)} about ${esc(invoiceId)}`);
}

function endCall() {
  if (!callInvoice) return;
  addTrace('ti-phone-off', `Call about ${esc(callInvoice)} ended`);
  callInvoice = null;
  $('#call-banner').hidden = true;
  $('#merchant').disabled = false;
  document.querySelectorAll('[data-scenario]').forEach((b) => { b.disabled = false; });
  const note = document.createElement('div');
  note.className = 'chat-note';
  note.innerHTML = '<i class="ti ti-phone-off me-1"></i>Call ended. Transcript and full recording are on the Customers page.';
  $('#chat').appendChild(note);
  setConversation(null);
}

// ---------- full call recording (you + AI, one file) ----------
let recordedTurns = 0;
let callAudio = null;

function noteRecorded(data) {
  recordedTurns += [data.merchant_audio_url, data.agent_audio_url].filter(Boolean).length;
  $('#btn-call-rec').disabled = recordedTurns === 0;
}

function stopCallAudio() {
  if (callAudio) callAudio.pause();
  callAudio = null;
  $('#btn-call-rec').innerHTML = '<i class="ti ti-headphones me-1"></i>Call';
}

$('#btn-call-rec').addEventListener('click', () => {
  if (callAudio && !callAudio.paused) { stopCallAudio(); return; }
  // ?n= changes with each new recorded turn, so the browser fetches the longer version
  const url = `/api/conversations/${conversationId}/call-recording.mp3?n=${recordedTurns}`;
  callAudio = new Audio(url);
  callAudio.onended = stopCallAudio;
  callAudio.onerror = () => { reportError('call recording playback', `${callAudio?.error?.code} (${url})`); stopCallAudio(); };
  callAudio.play().then(() => { $('#btn-call-rec').innerHTML = '<i class="ti ti-player-stop me-1"></i>Stop'; })
    .catch((e) => reportError('call recording playback', `${e.name}: ${e.message}`));
});
$('#merchant').addEventListener('change', newConversation);

async function sendTicket(text, ticketId) {
  const tl = new ClientTimeline('text');
  tl.mark('send clicked', text);
  const merchant_id = $('#merchant').value;
  const epoch = chatEpoch;
  bubble('merchant', text);
  const typing = bubble('agent', '…');
  tl.mark('request sent');
  const res = callInvoice
    ? await fetch(`/api/collections/${callInvoice}/turn`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, conversation_id: conversationId }) })
    : await fetch('/api/tickets', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ merchant_id, text, ticket_id: ticketId, speak: $('#speak').checked,
        conversation_id: conversationId }) });
  const data = await res.json();
  tl.mark('response received', `round trip`, performance.now() - tl.t0 - tl.at('request sent'));
  typing.remove();
  if (epoch !== chatEpoch) {  // "New" was clicked while this reply was on its way
    tl.mark('reply ignored', 'a new conversation was started meanwhile');
    return;
  }
  if (!res.ok) {
    bubble('agent', `Error: ${data.detail}`);
    reportError(`POST /api/tickets ${res.status}`, data.detail, JSON.stringify(tl.events));
    return;
  }
  setConversation(data.conversation_id);
  const reply = bubble('agent', data.reply, `${data.ticket_id} · ${data.status.replace('_', ' ').toLowerCase()} · ${data.steps} steps`);
  addPlayButton(reply, data.agent_audio_url, 'replay AI');
  noteRecorded(data);
  if (data.invoice) updateCallBanner(data.invoice);
  tl.mark('reply shown');
  finishClientTimeline(tl, data.ticket_id, data.timing);
  playAudio(data.audio_b64, tl);
}

$('#chat-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const text = $('#chat-input').value.trim();
  if (!text) return;
  $('#chat-input').value = '';
  sendTicket(text);
});

// Voice input: tap to start, tap again to send. Holding also works: releasing after HOLD_MS sends.
// MediaRecorder -> /api/voice (Sarvam Saaras -> agent -> Bulbul)
const MIN_RECORDING_MS = 500;
const HOLD_MS = 500;
const mic = $('#btn-mic');
const micStatus = $('#mic-status');
// state: idle -> starting (waiting for mic) -> recording -> sending -> idle
const voice = { state: 'idle', tl: null, recorder: null, stream: null, chunks: [], pressAt: 0, recStartAt: 0,
  stopRequested: false, pressWasStop: false, timer: null, pulse: null };

function setMicStatus(html, cls = '') {
  micStatus.hidden = !html;
  micStatus.className = `small ${cls}`;
  micStatus.innerHTML = html || '';
}

function showListening() {
  const tick = () => {
    const s = Math.floor((performance.now() - voice.recStartAt) / 1000);
    setMicStatus(`<span class="dot"></span>Listening ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} · tap the mic again to send`, 'listening');
  };
  tick();
  voice.timer = setInterval(tick, 250);
  mic.classList.add('recording');
  if (!REDUCED) voice.pulse = animate(mic, { scale: [1, 1.12, 1], duration: 900, loop: true, ease: 'inOutSine' });
}

function stopListeningUi() {
  clearInterval(voice.timer);
  voice.pulse?.pause();
  utils.set(mic, { scale: 1 });
  mic.classList.remove('recording');
}

async function startRecording() {
  const tl = new ClientTimeline('voice');
  voice.tl = tl;
  voice.state = 'starting';
  voice.stopRequested = false;
  tl.mark('button pressed', 'start listening');
  try {
    tl.mark('mic permission requested');
    const askedAt = performance.now();
    voice.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    tl.mark('mic ready', voice.stream.getAudioTracks()[0]?.label || 'audio track', performance.now() - askedAt);
    voice.chunks = [];
    voice.recorder = new MediaRecorder(voice.stream);
    voice.recorder.ondataavailable = (e) => { if (e.data.size) voice.chunks.push(e.data); };
    voice.recorder.onstop = () => onRecordingStopped(tl);
    voice.recorder.start();
    voice.recStartAt = performance.now();
    voice.state = 'recording';
    tl.mark('recording started', `${voice.recorder.mimeType} · speak now`);
    showListening();
    if (voice.stopRequested) stopRecording('send was tapped while the mic was starting');
  } catch (err) {
    voice.state = 'idle';
    tl.mark('mic error', err.message);
    reportError('microphone', err.message, JSON.stringify(tl.events));
    setMicStatus(`Microphone unavailable: ${esc(err.message)}`, 'hint');
  }
}

function stopRecording(reason) {
  if (voice.state === 'starting') {
    voice.stopRequested = true;
    voice.tl.mark('send requested before mic ready', reason);
    return;
  }
  if (voice.state !== 'recording') return;
  voice.state = 'sending';
  voice.tl.mark('stop', reason);
  stopListeningUi();
  setMicStatus('Sending…');
  voice.recorder.stop();
}

async function onRecordingStopped(tl) {
  voice.stream.getTracks().forEach((t) => t.stop());
  const recordedMs = performance.now() - voice.recStartAt;
  const blob = new Blob(voice.chunks, { type: voice.recorder.mimeType || 'audio/webm' });
  tl.mark('recording stopped', `${Math.round(blob.size / 1024)} KB, ${(recordedMs / 1000).toFixed(1)}s of audio`, recordedMs);

  // Too short to contain speech: don't send an empty file to Sarvam
  if (recordedMs < MIN_RECORDING_MS || blob.size < 1000) {
    tl.mark('not sent: too short', `under ${MIN_RECORDING_MS}ms or 1 KB`);
    finishClientTimeline(tl, `MIC-${Date.now().toString(36).toUpperCase()}`);
    sendClientTimeline(tl);
    setMicStatus("Didn't catch that. Tap the mic, speak, then tap again to send.", 'hint');
    voice.state = 'idle';
    return;
  }

  setMicStatus('Sending…');
  const epoch = chatEpoch;
  // Your voice message appears on your side straight away; its text fills in once transcribed
  const mine = bubble('merchant', '');
  mine.classList.add('voice-pending');
  mine.innerHTML = `<i class="ti ti-microphone me-1"></i>Voice message · ${(recordedMs / 1000).toFixed(1)}s`
    + '<span class="meta">transcribing…</span>';
  const typing = bubble('agent', '…');
  try {
    const form = new FormData();
    form.append('merchant_id', $('#merchant').value);
    if (conversationId) form.append('conversation_id', conversationId);
    if (callInvoice) form.append('invoice_id', callInvoice);
    form.append('audio', blob, 'audio.webm');
    tl.mark('request sent', `uploading ${Math.round(blob.size / 1024)} KB`);
    const res = await fetch('/api/voice', { method: 'POST', body: form });
    const data = await res.json();
    tl.mark('response received', 'round trip', performance.now() - tl.t0 - tl.at('request sent'));
    typing.remove();
    if (epoch !== chatEpoch) {  // "New" was clicked while this reply was on its way
      tl.mark('reply ignored', 'a new conversation was started meanwhile');
      return;
    }
    mine.classList.remove('voice-pending');
    if (!res.ok) {
      mine.querySelector('.meta').textContent = 'not understood';
      bubble('agent', data.detail || 'Something went wrong. Please try again.');
      // Browser timeline goes with the error so mic/recording problems are visible in errors.log
      reportError(`POST /api/voice ${res.status}`, data.detail, JSON.stringify(tl.events));
      return;
    }
    mine.innerHTML = `${esc(data.transcript)}<span class="meta"><i class="ti ti-microphone"></i> voice</span>`;
    addPlayButton(mine, data.merchant_audio_url, 'your recording');
    setConversation(data.conversation_id);
    const reply = bubble('agent', data.reply, `${data.ticket_id} · ${data.status.replace('_', ' ').toLowerCase()}`);
    addPlayButton(reply, data.agent_audio_url, 'replay AI');
    noteRecorded(data);
    if (data.invoice) updateCallBanner(data.invoice);
    tl.mark('transcript + reply shown');
    finishClientTimeline(tl, data.ticket_id, data.timing);
    playAudio(data.audio_b64, tl);
  } catch (err) {
    typing.remove();
    mine.classList.remove('voice-pending');
    mine.querySelector('.meta').textContent = 'not sent';
    bubble('agent', 'Could not reach the server. Please try again.');
    reportError('voice upload', err.message, JSON.stringify(tl.events));
  } finally {
    voice.state = 'idle';
    setMicStatus('');
  }
}

mic.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  try { mic.setPointerCapture(e.pointerId); } catch { /* keep going without capture */ }  // pointerup even if the pointer slides off
  voice.pressAt = performance.now();
  voice.pressWasStop = voice.state === 'recording' || voice.state === 'starting';
  if (voice.pressWasStop) { stopRecording('tapped to send'); return; }
  if (voice.state === 'sending') return;
  startRecording();
});
mic.addEventListener('pointerup', () => {
  if (voice.pressWasStop || !voice.tl) return;  // this press was the "send" tap
  const held = performance.now() - voice.pressAt;
  const hold = held >= HOLD_MS;
  voice.tl.mark('button released', `held ${Math.round(held)}ms → ${hold ? 'hold mode: sending' : 'tap mode: keep listening'}`);
  if (hold) stopRecording('released after holding');
});
// Keyboard (Enter/Space) produces a click with detail 0: toggle
mic.addEventListener('click', (e) => {
  if (e.detail !== 0) return;
  if (voice.state === 'recording' || voice.state === 'starting') stopRecording('keyboard: send');
  else if (voice.state === 'idle') startRecording();
});

// ---------- event stream ----------

const HANDLERS = {
  connected(e) { updateKpis(e.metrics); },

  ticket_start(e) {
    resetMap();
    $('#current-ticket').textContent = e.ticket_id;
    $('#ticket-label').textContent = e.ticket_id;
    setNodeState('ticket', 'active');
    sendPacket('edge-ticket');
    pulse('agent');
    markTile(e.ticket_id, 'running');
    addTrace('ti-inbox', `<b>${esc(e.ticket_id)}</b> from ${esc(e.merchant_id)}: “${esc(e.text)}”`);
  },

  thinking() { pulse('agent'); },

  reasoning(e) { addTrace('ti-bulb', esc(e.text), 'reasoning'); },

  tool_call(e) {
    setNodeState(e.system, 'active');
    sendPacket(`edge-${e.system}`);
    const args = Object.entries(e.args || {}).map(([k, v]) => `${k}=${esc(v)}`).join(', ');
    addTrace(TOOL_ICON[e.tool] || 'ti-tool',
      `<code>${esc(e.tool)}(${args})</code><div class="text-secondary">${esc(e.reason)}</div>`);
  },

  guardrail(e) {
    shield(e.decision);
    addTrace('ti-shield-check', `Guardrail on <code>${esc(e.tool)}</code>: ${DECISION_BADGE[e.decision]}
      ${e.reasons?.length ? `<div class="text-secondary">${e.reasons.map(esc).join('; ')}</div>` : ''}`);
  },

  tool_result(e) {
    const state = { ALLOW: 'ok', NEEDS_APPROVAL: 'warn', BLOCK: 'bad' }[e.decision] || 'ok';
    setNodeState(e.system, e.result?.error ? 'bad' : state);
    sendPacket(`edge-${e.system}`, COLORS[e.result?.error ? 'bad' : state], true);
    addTrace('ti-corner-down-left', `<span class="text-secondary">${esc(e.tool)} →</span> ${summarize(e.result)}`);
  },

  approval_created(e) { renderApproval(e.approval); },

  approval_decided(e) {
    removeApproval(e.approval_id, e.status === 'APPROVED');
    if (e.status === 'APPROVED') {
      shield('ALLOW');
      setNodeState('credits', 'ok');
      sendPacket('edge-credits', COLORS.ok);
      addTrace('ti-user-check', `Human <b>approved</b> ${esc(e.ticket_id)}: credit <b>${esc(e.result.credit_id)}</b> ₹${inr(e.result.amount)}`);
      markTile(e.ticket_id, 'RESOLVED');
      if (e.reply) bubble('agent', e.reply, `${e.ticket_id} · resolved after approval`);
    } else {
      addTrace('ti-user-x', `Human <b>rejected</b> ${esc(e.ticket_id)}`);
      markTile(e.ticket_id, 'ESCALATED');
    }
    scheduleAudit();
  },

  ticket_done(e) {
    setNodeState('ticket', { RESOLVED: 'ok', AWAITING_APPROVAL: 'warn', NEEDS_INFO: 'warn', ESCALATED: 'bad' }[e.status]);
    markTile(e.ticket_id, e.status);
    const icon = { RESOLVED: 'ti-circle-check text-success', AWAITING_APPROVAL: 'ti-clock text-warning',
      NEEDS_INFO: 'ti-help-circle text-warning', ESCALATED: 'ti-user-up text-danger' }[e.status];
    addTrace(icon, `<b>${esc(e.ticket_id)}</b> ${esc(e.status.replace('_', ' ').toLowerCase())} in ${e.steps} steps: ${esc(e.reply)}`);
    scheduleAudit();
  },

  metrics(e) { updateKpis(e.metrics); },

  factcheck(e) {
    if (e.ok) {
      addTrace('ti-checks text-success', `Fact-check <span class="badge bg-green-lt">passed</span>${e.rewrite ? ' after rewrite' : ''} <span class="text-secondary">every ID, amount and claim backed by tools</span>`);
    } else {
      addTrace('ti-alert-octagon text-danger', `Fact-check <span class="badge bg-red-lt">rejected reply</span>${e.rewrite ? ' again: safe reply sent, human will review' : ': asking AI to rewrite'}
        <div class="text-secondary">${e.issues.map(esc).join('; ')}</div>`);
      if (!REDUCED) animate(node('agent'), { x: [0, -6, 6, -4, 4, 0], duration: 420 });
    }
  },

  mood(e) {
    const icon = { calm: 'ti-mood-smile text-success', worried: 'ti-mood-nervous text-warning', angry: 'ti-mood-angry text-danger' }[e.mood];
    addTrace(icon || 'ti-mood-empty', `Mood for ${esc(e.conversation_id)}: <b>${esc(e.mood)}</b> <span class="text-secondary">${esc(e.reason)}</span>`);
  },

  batch_start(e) {
    batchMode = true;
    buildBatchGrid(e.tickets);
    $('#batch-note').textContent = `Running ${e.tickets.length} tickets…`;
    $('#btn-batch').disabled = true;
  },

  batch_done(e) {
    batchMode = false;
    $('#btn-batch').disabled = false;
    const m = e.metrics;
    $('#batch-note').innerHTML = `Done: <b>${m.auto_resolution_rate}%</b> auto-resolved · ₹${inr(m.recovered_amount)} recovered · ${m.avg_steps} steps/ticket`;
  },

  reset(e) {
    resetMap();
    kpiValues.tickets = -1; // force KPI animation back to zero
    updateKpis(e.metrics);
    $('#trace').innerHTML = '';
    $('#chat').innerHTML = '';
    setConversation(null);
    recordedTurns = 0;
    stopCallAudio();
    $('#btn-call-rec').disabled = true;
    $('#batch-grid').innerHTML = '';
    $('#batch-note').textContent = 'Runs 50 generated tickets through the agent.';
    $('#approvals').innerHTML = '';
    updateApprovalCount();
    $('#current-ticket').textContent = 'idle';
    $('#ticket-label').textContent = 'waiting';
    batchMode = false;
    $('#btn-batch').disabled = false;
    refreshAudit();
  },

  stt(e) { addTrace('ti-microphone', esc(e.detail)); },

  invoice_paid(e) {
    addTrace('ti-cash text-success', `<b>₹${inr(e.amount)}</b> received from ${esc(e.invoice.debtor)} for ${esc(e.invoice.invoice_id)} via UPI link`);
    sendPacket('edge-payments', COLORS.ok, true);
    setNodeState('payments', 'ok');
    if (callInvoice === e.invoice.invoice_id) {
      updateCallBanner(e.invoice);
      const note = document.createElement('div');
      note.className = 'chat-note';
      note.innerHTML = `<i class="ti ti-cash me-1"></i>Payment received: ₹${inr(e.amount)}`;
      $('#chat').appendChild(note);
      animate(note, { scale: [0.8, 1], opacity: [0, 1], duration: ms(400), ease: 'outBack' });
    }
  },

  // Live server steps while a request is still running
  timing_step(e) {
    if (!debugLog.enabled) return;
    const list = debugLog.server.get(e.trace_id) || [];
    list.push(e);
    debugLog.server.set(e.trace_id, list);
    renderDebugBlock(e.trace_id);
  },
  error(e) { addTrace('ti-alert-triangle text-danger', esc(e.detail)); },
};

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/events`);
  ws.onmessage = (msg) => {
    const e = JSON.parse(msg.data);
    HANDLERS[e.type]?.(e);
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

// ---------- init ----------

async function init() {
  const [health, merchants, scenarios, approvals] = await Promise.all([
    fetch('/health').then((r) => r.json()),
    fetch('/api/merchants').then((r) => r.json()),
    fetch('/api/scenarios').then((r) => r.json()),
    fetch('/api/approvals?status=PENDING').then((r) => r.json()),
  ]);

  const badge = $('#mode-badge');
  badge.textContent = health.mode === 'sarvam' ? `Sarvam live · ${health.llm_model}` : 'Offline scripted mode';
  badge.className = `badge ms-2 ${health.mode === 'sarvam' ? 'bg-green-lt' : 'bg-orange-lt'}`;
  if (health.mode !== 'sarvam') $('#btn-mic').disabled = true;
  const dbBadge = $('#db-badge');
  dbBadge.hidden = false;
  dbBadge.innerHTML = health.db.backend === 'neon'
    ? '<i class="ti ti-database me-1"></i>Neon Postgres' : '<i class="ti ti-database-off me-1"></i>Local SQLite';
  dbBadge.className = `badge ${health.db.backend === 'neon' ? 'bg-green-lt' : 'bg-orange-lt'}`;
  dbBadge.title = health.db.host;
  setDebug(!!health.detailed_log, false);

  $('#merchant').innerHTML = merchants.slice(0, 5).concat(merchants.slice(5))
    .map((m) => `<option value="${m.merchant_id}">${esc(m.merchant_id)} · ${esc(m.name)}, ${esc(m.city)}</option>`).join('');

  $('#scenarios').innerHTML = scenarios.map((s) =>
    `<button class="btn btn-sm btn-outline-secondary" data-scenario="${s.ticket_id}">${esc(s.label)}</button>`).join('');
  $('#scenarios').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-scenario]');
    if (!btn) return;
    const s = scenarios.find((x) => x.ticket_id === btn.dataset.scenario);
    $('#merchant').value = s.merchant_id;
    newConversation();  // each demo scenario is its own conversation
    sendTicket(s.text, s.ticket_id);
  });

  approvals.reverse().forEach(renderApproval);
  updateApprovalCount();

  $('#btn-batch').addEventListener('click', () => fetch('/api/batch', { method: 'POST' }));
  $('#btn-reset').addEventListener('click', () => fetch('/api/reset', { method: 'POST' }));

  // Entrance: cards rise in
  animate('.card', { opacity: [0, 1], y: [16, 0], delay: stagger(60), duration: ms(500), ease: 'outQuad' });

  refreshAudit();
  connect();

  // Opened from the Collections page: /?call=INV-1001 starts the AI call (you play merchant B)
  const callParam = new URLSearchParams(location.search).get('call');
  if (callParam) {
    history.replaceState(null, '', '/');
    startCall(callParam);
  }
}

init();
