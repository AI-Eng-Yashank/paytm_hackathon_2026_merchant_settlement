// Collections page: merchant A's invoices, AI call / reminder actions, and the outbox
// (reminder drafts A approves, messages sent, notifications to A).

const { animate, stagger } = anime;
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const ms = (d) => (REDUCED ? 0 : d);
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const inr = (n) => Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 });

const STATUS = {
  OPEN: ['bg-azure-lt', 'open'], PROMISED: ['bg-yellow-lt', 'promised'],
  PAID: ['bg-green-lt', 'paid'], DISPUTED: ['bg-red-lt', 'disputed — your call'],
};

const kpi = {};
function updateKpis(s) {
  document.querySelectorAll('.ckpi').forEach((el) => {
    const to = Number(s[el.dataset.k] || 0);
    const from = kpi[el.dataset.k] || 0;
    kpi[el.dataset.k] = to;
    const c = { v: from };
    animate(c, { v: to, duration: ms(700), ease: 'outExpo',
      onUpdate: () => { el.textContent = inr(c.v); }, onComplete: () => { el.textContent = inr(to); } });
    // Animations pause while the page is in the background; make sure the real number still shows
    setTimeout(() => { if (kpi[el.dataset.k] === to) el.textContent = inr(to); }, 900);
  });
}

function dueText(i) {
  if (i.status === 'PAID') return `<span class="text-secondary">${esc(i.due_on)}</span>`;
  if (i.days_overdue) return `<span class="text-danger">${i.days_overdue} days overdue</span><div class="small text-secondary">${esc(i.due_on)}</div>`;
  return `<span>in ${i.due_in_days} days</span><div class="small text-secondary">${esc(i.due_on)}</div>`;
}

function invoiceRow(i) {
  const [cls, label] = STATUS[i.status] || ['bg-secondary-lt', i.status];
  const extra = i.status === 'PROMISED' ? `<div class="small text-secondary">₹${inr(i.promise_amount)} by ${esc(i.promise_date)}</div>`
    : i.status === 'DISPUTED' ? `<div class="small text-secondary text-truncate" style="max-width:180px" title="${esc(i.dispute_reason)}">${esc(i.dispute_reason)}</div>` : '';
  const links = i.links.length ? `<div class="small text-secondary">${i.links.map((l) =>
    `<a href="/pay/${esc(l.link_id)}" target="_blank" class="${l.status === 'PAID' ? 'text-success' : ''}">${esc(l.link_id)} ₹${inr(l.amount)} ${l.status === 'PAID' ? '✓' : ''}</a>`).join(' · ')}</div>` : '';
  const open = i.status !== 'PAID';
  return `
    <tr data-invoice="${esc(i.invoice_id)}">
      <td><b>${esc(i.invoice_id)}</b><div class="small text-secondary">${esc(i.description)}</div></td>
      <td>${esc(i.debtor)}<div class="small text-secondary">${esc(i.debtor_id)} · ${esc(i.debtor_phone)}</div></td>
      <td>₹${inr(i.outstanding)}${i.paid_amount ? `<div class="small text-success">₹${inr(i.paid_amount)} paid</div>` : ''}
        ${i.max_instalments > 1 ? `<div class="small text-secondary">up to ${i.max_instalments} instalments</div>` : ''}</td>
      <td>${dueText(i)}</td>
      <td><span class="badge ${cls}">${label}</span>${extra}${links}</td>
      <td class="text-end text-nowrap">
        ${open && i.status !== 'DISPUTED' ? `<a class="btn btn-sm btn-primary" href="/?call=${esc(i.invoice_id)}"><i class="ti ti-phone-call me-1"></i>AI call</a>
        <button class="btn btn-sm btn-outline-secondary" data-remind="${esc(i.invoice_id)}"><i class="ti ti-brand-whatsapp me-1"></i>Reminder</button>` : ''}
      </td>
    </tr>`;
}

async function loadInvoices(flash) {
  const [invoices, summary] = await Promise.all([
    fetch('/api/collections/invoices').then((r) => r.json()),
    fetch('/api/collections/summary').then((r) => r.json()),
  ]);
  if (invoices[0]) $('#creditor-name').textContent = invoices[0].creditor;
  $('#invoices').innerHTML = invoices.map(invoiceRow).join('');
  updateKpis(summary);
  if (flash) {
    const row = document.querySelector(`tr[data-invoice="${flash}"]`);
    if (row) animate(row, { backgroundColor: ['rgba(47,179,68,.35)', 'rgba(47,179,68,0)'], duration: ms(1600), ease: 'outQuad' });
  }
}

// ---------- outbox ----------

function outboxItem(m) {
  const icon = { whatsapp: 'ti-brand-whatsapp text-success', email: 'ti-mail', dashboard: 'ti-bell' }[m.channel] || 'ti-send';
  const to = m.channel === 'dashboard' ? `To you (${esc(m.to_name)})` : `To ${esc(m.to_name)} · ${esc(m.to_contact)}`;
  if (m.status === 'DRAFT') {
    return `
      <div class="outbox-item draft" data-msg="${m.id}">
        <div class="small"><i class="ti ${icon} me-1"></i><b>Draft</b> · ${to}</div>
        <textarea class="form-control form-control-sm mt-1" rows="4">${esc(m.body)}</textarea>
        <div class="d-flex gap-2 mt-2 align-items-center">
          <button class="btn btn-sm btn-success" data-send="${m.id}"><i class="ti ti-check me-1"></i>Approve &amp; send</button>
          <span class="small text-secondary">Sent in your name only after you approve</span>
        </div>
      </div>`;
  }
  return `
    <div class="outbox-item" data-msg="${m.id}">
      <div class="small"><i class="ti ${icon} me-1"></i>${to} <span class="text-secondary">· ${new Date((m.sent_at || m.created_at) * 1000).toLocaleTimeString()}</span>
        ${m.approved_by ? `<span class="badge bg-green-lt ms-1">sent · approved by ${esc(m.approved_by)}</span>` : ''}</div>
      ${m.subject ? `<div class="fw-bold small">${esc(m.subject)}</div>` : ''}
      <div class="small">${esc(m.body)}</div>
    </div>`;
}

async function loadOutbox() {
  const msgs = await (await fetch('/api/outbox')).json();
  $('#outbox').innerHTML = msgs.length ? msgs.map(outboxItem).join('')
    : '<div class="text-secondary small">Nothing yet. Reminders you approve and updates for you appear here.</div>';
}

document.addEventListener('click', async (e) => {
  const remind = e.target.closest('[data-remind]');
  if (remind) {
    remind.disabled = true;
    remind.innerHTML = '<i class="ti ti-loader me-1"></i>Drafting…';
    const r = await fetch(`/api/collections/${remind.dataset.remind}/reminder`, { method: 'POST' });
    if (!r.ok) alert((await r.json()).detail);
    await loadOutbox();
    await loadInvoices();
    const draft = document.querySelector('.outbox-item.draft');
    if (draft) animate(draft, { opacity: [0, 1], y: [-10, 0], duration: ms(350), ease: 'outBack' });
  }
  const send = e.target.closest('[data-send]');
  if (send) {
    const body = send.closest('.outbox-item').querySelector('textarea').value;
    send.disabled = true;
    await fetch(`/api/outbox/${send.dataset.send}/send`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body, approved_by: $('#creditor-name').textContent }) });
    await loadOutbox();
  }
});

// ---------- live updates ----------

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/events`);
  ws.onmessage = (msg) => {
    const e = JSON.parse(msg.data);
    if (['invoice_paid', 'invoice_updated'].includes(e.type)) { loadInvoices(e.invoice?.invoice_id); loadOutbox(); }
    if (['outbox', 'reset'].includes(e.type)) { loadOutbox(); loadInvoices(); }
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

(async () => {
  await Promise.all([loadInvoices(), loadOutbox()]);
  animate('#invoices tr', { opacity: [0, 1], x: [-10, 0], delay: stagger(60), duration: ms(350) });
  connect();
})();
