// Customer health dashboard: list of merchants (at-risk first) and a detail view with health score,
// money, mood, tickets timeline and conversations (full transcript + recordings).

const { animate, stagger, svg } = anime;
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const ms = (d) => (REDUCED ? 0 : d);

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const inr = (n) => Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 });

const LABEL = {
  healthy: { text: 'Healthy', cls: 'bg-green-lt', color: '#2fb344' },
  watch: { text: 'Watch', cls: 'bg-yellow-lt', color: '#f59f00' },
  at_risk: { text: 'At risk', cls: 'bg-red-lt', color: '#d63939' },
};
const MOOD = {
  calm: { icon: 'ti-mood-smile', cls: 'text-success', text: 'Calm' },
  worried: { icon: 'ti-mood-nervous', cls: 'text-warning', text: 'Worried' },
  angry: { icon: 'ti-mood-angry', cls: 'text-danger', text: 'Angry' },
};
const STATUS = {
  RESOLVED: 'bg-green-lt', AWAITING_APPROVAL: 'bg-yellow-lt', NEEDS_INFO: 'bg-yellow-lt',
  ESCALATED: 'bg-red-lt', IN_PROGRESS: 'bg-azure-lt', OPEN: 'bg-azure-lt',
};

let customers = [];
let current = null;          // merchant_id shown in the detail view
let openConversation = null; // conversation id whose transcript is open

function ago(ts) {
  if (!ts) return 'never';
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function moodChip(mood, withText = false) {
  const m = MOOD[mood];
  if (!m) return '<span class="text-secondary small">-</span>';
  return `<span class="${m.cls}" title="${m.text}"><i class="ti ${m.icon}"></i>${withText ? ` ${m.text}` : ''}</span>`;
}

const status = (s) => `<span class="badge ${STATUS[s] || 'bg-secondary-lt'}">${esc((s || '').replace('_', ' ').toLowerCase())}</span>`;

// ---------- list ----------

function renderList() {
  const q = $('#search').value.trim().toLowerCase();
  const rows = customers.filter((c) => !q || [c.merchant_id, c.name, c.city, c.business].some((v) => String(v).toLowerCase().includes(q)));
  $('#list-count').textContent = `${rows.length} of ${customers.length}`;
  $('#customer-list').innerHTML = rows.map((c) => `
    <a href="?m=${c.merchant_id}" class="list-group-item list-group-item-action cust-row ${c.merchant_id === current ? 'active' : ''}"
       data-merchant="${c.merchant_id}">
      <div class="d-flex align-items-center gap-2">
        <span class="health-pill" style="--c:${LABEL[c.label].color}">${c.health}</span>
        <div class="flex-fill text-truncate">
          <div class="fw-bold text-truncate">${esc(c.name)}</div>
          <div class="small text-secondary">${esc(c.merchant_id)} · ${esc(c.city)} · ${c.tickets} ticket${c.tickets === 1 ? '' : 's'}</div>
        </div>
        <div class="text-end small">
          ${moodChip(c.mood)}
          ${c.open_issues ? `<div class="text-warning">${c.open_issues} open</div>` : `<div class="text-secondary">${ago(c.last_contact)}</div>`}
        </div>
      </div>
    </a>`).join('');
}

async function loadList() {
  customers = await (await fetch('/api/customers')).json();
  renderList();
}

$('#search').addEventListener('input', renderList);
$('#customer-list').addEventListener('click', (e) => {
  const row = e.target.closest('[data-merchant]');
  if (!row) return;
  e.preventDefault();
  history.replaceState(null, '', `?m=${row.dataset.merchant}`);
  openConversation = null;
  showCustomer(row.dataset.merchant);
});

// ---------- detail ----------

function gauge(score, label) {
  const r = 52;
  const len = 2 * Math.PI * r;
  return `
    <svg class="gauge" viewBox="0 0 130 130" role="img" aria-label="Health score ${score} of 100">
      <circle cx="65" cy="65" r="${r}" class="gauge-track"/>
      <circle cx="65" cy="65" r="${r}" class="gauge-value" id="gauge-value"
        style="stroke:${LABEL[label].color}" stroke-dasharray="${len}" stroke-dashoffset="${len}"
        transform="rotate(-90 65 65)"/>
      <text x="65" y="66" class="gauge-num" id="gauge-num">0</text>
      <text x="65" y="86" class="gauge-label">${LABEL[label].text}</text>
    </svg>`;
}

function animateGauge(score) {
  const el = document.getElementById('gauge-value');
  const len = Number(el.getAttribute('stroke-dasharray'));
  const counter = { v: 0 };
  animate(el, { strokeDashoffset: [len, len * (1 - score / 100)], duration: ms(900), ease: 'outExpo' });
  animate(counter, { v: score, duration: ms(900), ease: 'outExpo',
    onUpdate: () => { document.getElementById('gauge-num').textContent = Math.round(counter.v); } });
}

async function showCustomer(merchantId) {
  current = merchantId;
  renderList();
  const res = await fetch(`/api/customers/${merchantId}`);
  if (!res.ok) { $('#detail').innerHTML = '<div class="card"><div class="card-body">Unknown customer.</div></div>'; return; }
  const d = await res.json();
  const m = d.merchant;
  const h = d.health;

  $('#detail').innerHTML = `
    <div class="card mb-3">
      <div class="card-body d-flex flex-wrap gap-3 align-items-center">
        <div class="flex-fill">
          <h2 class="mb-1">${esc(m.name)}</h2>
          <div class="text-secondary">${esc(m.business)} · ${esc(m.city)} · ${esc(m.merchant_id)}</div>
          <div class="mt-2 d-flex flex-wrap gap-2">
            <span class="badge ${m.kyc_tier === 'FULL' ? 'bg-green-lt' : 'bg-orange-lt'}">KYC ${esc(m.kyc_tier)}</span>
            <span class="badge ${m.risk_score > 0.7 ? 'bg-red-lt' : 'bg-secondary-lt'}">risk ${m.risk_score}</span>
            <span class="badge bg-secondary-lt">${esc(m.settlement_bank)} · ${esc(m.settlement_cycle)}</span>
            <span class="badge bg-secondary-lt">account ${m.account_age_days} days</span>
            <span class="badge bg-secondary-lt">last contact ${ago(d.last_contact)}</span>
          </div>
        </div>
        <div class="d-flex align-items-center gap-3">
          ${gauge(h.score, h.label)}
          <div class="small" style="max-width:220px">
            <div class="fw-bold mb-1">Why this score</div>
            ${h.factors.length ? h.factors.map((f) => `<div><span class="text-danger">${f.points}</span> ${esc(f.why)}</div>`).join('')
              : '<div class="text-secondary">No problems found.</div>'}
          </div>
        </div>
      </div>
    </div>

    <div class="row row-cards mb-3">
      ${[['Recovered', d.money.recovered, 'ti-arrow-back-up'], ['Credited total', d.money.credited_total, 'ti-coin-rupee'],
         ['Waiting approval', d.money.pending_approval, 'ti-clock'], ['RBI compensation due', d.money.compensation_due, 'ti-scale']]
        .map(([label, v, icon]) => `
        <div class="col-6 col-md-3"><div class="card card-sm money-card"><div class="card-body">
          <div class="subheader"><i class="ti ${icon} me-1"></i>${label}</div>
          <div class="h2 mb-0">₹${inr(v)}</div></div></div></div>`).join('')}
    </div>

    <div class="row row-cards">
      <div class="col-md-6">
        <div class="card h-100">
          <div class="card-header"><h3 class="card-title"><i class="ti ti-timeline me-2"></i>Tickets</h3>
            <div class="card-actions small text-secondary">${d.counts.tickets} total · ${d.counts.resolved} resolved · ${d.counts.contacts_7d} in 7 days</div></div>
          <div class="list-group list-group-flush scroll-list">
            ${d.tickets.length ? d.tickets.map((t) => `
              <a href="#" class="list-group-item list-group-item-action timeline-item" data-conv="${esc(t.conversation_id || '')}">
                <div class="d-flex justify-content-between gap-2">
                  <span class="small text-secondary">${esc(t.ticket_id)} · ${ago(t.created_at)}</span>${status(t.status)}
                </div>
                <div class="text-truncate">${esc(t.text)}</div>
                <div class="small text-secondary">${t.steps} steps${t.recovered_amount ? ` · ₹${inr(t.recovered_amount)} recovered` : ''}${t.human_involved ? ' · human involved' : ''}</div>
              </a>`).join('') : '<div class="list-group-item text-secondary">No tickets yet.</div>'}
          </div>
        </div>
      </div>
      <div class="col-md-6">
        <div class="card h-100">
          <div class="card-header"><h3 class="card-title"><i class="ti ti-messages me-2"></i>Conversations</h3>
            <div class="card-actions">${d.mood ? `${moodChip(d.mood.mood, true)} <span class="small text-secondary">${esc(d.mood.reason || '')}</span>` : ''}</div></div>
          <div class="list-group list-group-flush scroll-list">
            ${d.conversations.length ? d.conversations.map((c) => `
              <a href="#" class="list-group-item list-group-item-action timeline-item" data-conv="${esc(c.conversation_id)}">
                <div class="d-flex justify-content-between gap-2">
                  <span><b>${esc(c.conversation_id)}</b> <span class="small text-secondary">${esc(c.channel)} · ${c.turns} messages</span></span>
                  ${status(c.status)}
                </div>
                <div class="small d-flex justify-content-between"><span class="text-secondary">${ago(c.updated_at)}</span>${moodChip(c.mood, true)}</div>
              </a>`).join('') : '<div class="list-group-item text-secondary">No conversations yet.</div>'}
          </div>
        </div>
      </div>
    </div>

    ${d.pending_approvals.length ? `
    <div class="card mt-3"><div class="card-body">
      <div class="fw-bold mb-1"><i class="ti ti-clock me-1"></i>Waiting for approval</div>
      ${d.pending_approvals.map((p) => `<div class="small">₹${inr(p.amount)} for ${esc(p.txn_id)} (${esc(p.ticket_id)}): ${p.reasons.map(esc).join('; ')}</div>`).join('')}
      <a href="/" class="small">Approve or reject on the agent dashboard</a>
    </div></div>` : ''}

    <div id="transcript"></div>`;

  animateGauge(h.score);
  animate('#detail .card', { opacity: [0, 1], y: [10, 0], delay: stagger(50), duration: ms(350), ease: 'outQuad' });
  if (openConversation) showTranscript(openConversation);
}

$('#detail').addEventListener('click', (e) => {
  const item = e.target.closest('[data-conv]');
  if (!item) return;
  e.preventDefault();
  if (item.dataset.conv) showTranscript(item.dataset.conv);
});

// ---------- transcript ----------

async function showTranscript(conversationId) {
  openConversation = conversationId;
  const res = await fetch(`/api/conversations/${conversationId}`);
  if (!res.ok) return;
  const c = await res.json();
  $('#transcript').innerHTML = `
    <div class="card mt-3">
      <div class="card-header">
        <h3 class="card-title"><i class="ti ti-file-text me-2"></i>Transcript ${esc(c.conversation_id)}</h3>
        <div class="card-actions">${moodChip(c.mood, true)} <span class="small text-secondary ms-1">${esc(c.mood_reason || '')}</span></div>
      </div>
      ${c.call_recording_url ? `
      <div class="card-body border-bottom call-recording">
        <div class="d-flex align-items-center gap-2 mb-1">
          <i class="ti ti-headphones"></i><b>Full call recording</b>
          <span class="small text-secondary">merchant + AI, ${c.recorded_turns} recorded turn${c.recorded_turns === 1 ? '' : 's'} in order</span>
          <a class="small ms-auto" href="${esc(c.call_recording_url)}" download="call-${esc(c.conversation_id)}.mp3"><i class="ti ti-download"></i> download MP3</a>
        </div>
        <audio controls preload="metadata" src="${esc(c.call_recording_url)}"></audio>
      </div>` : `<div class="card-body border-bottom small text-secondary"><i class="ti ti-headphones-off me-1"></i>No voice turns in this conversation, so there is no call recording (text only).</div>`}
      <div class="card-body transcript">
        ${c.messages.map((m) => `
          <div class="bubble ${m.role === 'merchant' ? 'merchant' : 'agent'}">
            <div>${esc(m.content)}</div>
            ${m.audio_url ? `<audio controls preload="metadata" src="${esc(m.audio_url)}"></audio>
              <a class="small rec-download" href="${esc(m.audio_url)}" download><i class="ti ti-download"></i> download</a>` : ''}
            ${m.facts ? `<div class="facts">${m.facts.map((f) => `<span class="badge bg-secondary-lt">${esc(f.tool)}</span>`).join(' ')}</div>` : ''}
            <span class="meta">${m.role === 'merchant' ? 'Merchant' : 'AI'} · ${esc(m.channel || 'text')} · ${new Date(m.created_at * 1000).toLocaleTimeString()}${m.ticket_id ? ` · ${esc(m.ticket_id)}` : ''}</span>
          </div>`).join('')}
      </div>
    </div>`;
  animate('#transcript .bubble', { opacity: [0, 1], y: [8, 0], delay: stagger(40), duration: ms(250), ease: 'outQuad' });
  // If a browser refuses a recording, say so in logs/errors.log
  document.querySelectorAll('#transcript audio, .call-recording audio').forEach((a) => a.addEventListener('error', () => {
    fetch('/api/debug/client-error', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ where: 'recording playback (customers page)',
        message: `${a.error?.code}: ${a.error?.message || 'cannot play'} (${a.getAttribute('src')}) ${navigator.userAgent}` }) });
  }));
  document.getElementById('transcript').scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth', block: 'nearest' });
}

// ---------- live updates ----------

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    await loadList();
    if (current) showCustomer(current);
  }, 600);
}

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/events`);
  ws.onmessage = (msg) => {
    const e = JSON.parse(msg.data);
    if (['ticket_done', 'mood', 'approval_decided', 'reset', 'batch_done'].includes(e.type)) scheduleRefresh();
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

(async () => {
  await loadList();
  const fromUrl = new URLSearchParams(location.search).get('m');
  const first = fromUrl || customers.find((c) => c.tickets > 0)?.merchant_id || customers[0]?.merchant_id;
  if (first) showCustomer(first);
  animate('.cust-row', { opacity: [0, 1], x: [-10, 0], delay: stagger(15, { start: 100 }), duration: ms(300) });
  connect();
})();
