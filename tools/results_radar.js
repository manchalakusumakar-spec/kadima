/* KADIMA RESULTS RADAR tab (Claude, 2026-11-11) -- display only.
   Adds tabs above the "Gap-news signals" table in Trade Audit v2:
   [Gap-news signals] [Results · Today] [Results · Tomorrow]. Reads /nb/results_radar.json every 30 s. */
(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const f = n => n == null || !isFinite(n) ? '—' : Number(n).toLocaleString('en-IN', {maximumFractionDigits: 2});
  const pc = n => n == null || !isFinite(n) ? '—' : (n > 0 ? '+' : '') + Number(n).toFixed(1) + '%';
  const cl = n => n == null || !isFinite(n) ? '' : n > 0 ? 'g' : n < 0 ? 'r' : '';
  const arrow = n => n == null || !isFinite(n) ? '' : n > 0 ? '▲ ' : n < 0 ? '▼ ' : '';
  let tab = 'gap', data = null, timer = null;

  const css = `#rrTabs{display:flex;gap:6px;margin:16px 0 6px;flex-wrap:wrap}
#rrTabs button{background:#13213a;border:1px solid #23324d;color:#cfe2ff;border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer}
#rrTabs button.on{background:#1d3a66;border-color:#3d6bb3;color:#fff;font-weight:600}
#rrTabs .cnt{font-size:11px;opacity:.8;margin-left:4px}
#rrBox table{width:100%;border-collapse:collapse;font-size:13px}#rrBox td,#rrBox th{padding:6px 8px;border-bottom:1px solid #23324d;text-align:left;vertical-align:top}
#rrBox .l{font-size:12px;color:#93a3bf}#rrBox .g{color:#3ecf8e}#rrBox .r{color:#ff6b6b}#rrBox .y{color:#f5c542}
#rrBox .b{font-size:11px;padding:1px 6px;border-radius:4px;margin-right:4px;white-space:nowrap}
#rrBox .bd{background:#174d33;color:#c8f7dc}#rrBox .bu{background:#2a3550;color:#cfe2ff}
#rrBox .lean{font-weight:700;padding:1px 6px;border-radius:4px}
#rrBox .lUP{background:#174d33;color:#c8f7dc}#rrBox .lDOWN{background:#5a1d1d;color:#ffd0d0}#rrBox .lMIXED,#rrBox .lWAIT{background:#2a3550;color:#cfe2ff}
#rrBox .rng{white-space:nowrap}`;

  function setup() {
    const tbody = $('tav2g');
    if (!tbody || $('rrTabs')) return !!$('rrTabs');
    const wrap = tbody.closest('div'), h3 = wrap && wrap.previousElementSibling;
    if (!wrap || !h3 || h3.tagName !== 'H3') return false;
    const st = document.createElement('style'); st.textContent = css; document.head.append(st);
    const bar = document.createElement('div'); bar.id = 'rrTabs';
    bar.innerHTML = '<button data-t="gap" class="on">Gap-news signals</button>' +
      '<button data-t="today">Results · Today<span class="cnt" id="rrCt"></span></button>' +
      '<button data-t="tomorrow">Results · <span id="rrNd">Tomorrow</span><span class="cnt" id="rrCn"></span></button>';
    h3.before(bar);
    const box = document.createElement('div'); box.id = 'rrBox'; box.hidden = true; wrap.after(box);
    bar.addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      tab = b.dataset.t;
      bar.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
      h3.hidden = wrap.hidden = tab !== 'gap'; box.hidden = tab === 'gap';
      render();
    });
    return true;
  }

  function rangeCell(r) {
    const g = r.range; if (!g) return '<span class="l">no data</span>';
    return '<span class="rng">₹' + f(g.lo) + ' – ₹' + f(g.hi) + '</span><br><span class="l">±' + f(g.pct) + '% · 68% · ' +
      (g.source === 'options' ? 'options' : 'past moves') + '</span>';
  }

  function resultCell(r) {
    if (r.status !== 'Declared') return '<span class="b bu">DUE</span><span class="l">' + esc(r.date) + '</span>';
    const p = r.profit_yoy != null ? r.profit_yoy : null;
    let h = '<span class="b bd">DECLARED</span><span class="l">' + esc(r.declared_at || '') + ' · ' + esc(r.session || '') + '</span><br>';
    h += 'Profit <b class="' + cl(p ?? r.profit_qoq) + '">' + arrow(p ?? r.profit_qoq) + (p != null ? pc(p) + ' YoY' : pc(r.profit_qoq) + ' QoQ') + '</b>';
    if (r.turn) h += ' <span class="y">' + esc(r.turn) + '</span>';
    h += '<br><span class="l">₹' + f(r.profit_q_cr) + ' cr · revenue <span class="' + cl(r.rev_yoy ?? r.rev_qoq) + '">' +
      (r.rev_yoy != null ? pc(r.rev_yoy) + ' YoY' : pc(r.rev_qoq) + ' QoQ') + '</span>' + (r.basis ? ' · ' + esc(r.basis) : '') + '</span>';
    return h;
  }

  function leanCell(r) {
    if (r.status !== 'Declared') return '<span class="l">after results</span>';
    const t = {UP: 'UP', DOWN: 'DOWN', MIXED: 'MIXED', WAIT: 'WAIT'}[r.lean] || 'WAIT';
    const why = t === 'WAIT' ? 'waiting for price' : t === 'MIXED' ? 'price vs profit disagree' : 'profit + price agree';
    return '<span class="lean l' + t + '">' + t + '</span><br><span class="l">' + why + '</span>';
  }

  function render() {
    const box = $('rrBox'); if (!box || tab === 'gap') return;
    if (!data) { box.innerHTML = '<p class="l">Results Radar loading…</p>'; return; }
    const rows = tab === 'today' ? data.today : data.tomorrow;
    const a = data.audit || {};
    const score = 'Forecast check: range ' + (a.range_n ? a.range_hit + '% in band (' + a.range_n + ')' : 'not scored yet') +
      ' · lean ' + (a.lean_n ? a.lean_hit + '% right (' + a.lean_n + ')' : 'not scored yet');
    let h = '<p class="l">' + esc(score) + '</p><div style="overflow:auto"><table><thead><tr><th>Stock</th><th>Result</th><th>Price</th><th>Next-session range</th><th>Lean</th></tr></thead><tbody>';
    if (!rows || !rows.length) h += '<tr><td colspan="5" class="l">No F&amp;O stocks with results ' + (tab === 'today' ? 'today' : 'on ' + esc(data.next_day)) + '.</td></tr>';
    for (const r of rows || []) {
      h += '<tr><td><b>' + esc(r.symbol) + '</b><br><span class="l">' + esc((r.company || '').slice(0, 28)) + '</span></td>' +
        '<td>' + resultCell(r) + '</td>' +
        '<td>' + (r.ltp != null ? '₹' + f(r.ltp) + ' <span class="' + cl(r.chg) + '">' + pc(r.chg) + '</span>' : '<span class="l">prev ₹' + f(r.prev_close) + '</span>') +
        (r.gap != null ? '<br><span class="l">gap <span class="' + cl(r.gap) + '">' + pc(r.gap) + '</span></span>' : '') + '</td>' +
        '<td>' + rangeCell(r) + '</td><td>' + leanCell(r) + '</td></tr>';
    }
    h += '</tbody></table></div><p class="l">' + esc(data.note || '') + ' Updated ' + esc(data.updated || '') +
      ((data.errors || []).length ? ' · <span class="y">source issue: ' + esc(data.errors[0]) + '</span>' : '') + '</p>';
    box.innerHTML = h;
  }

  async function load() {
    try {
      const r = await fetch('/nb/results_radar.json', {cache: 'no-store'});
      if (r.ok) data = await r.json();
    } catch (e) { /* keep last */ }
    if (data) {
      const ct = $('rrCt'), cn = $('rrCn'), nd = $('rrNd');
      if (ct) ct.textContent = '(' + (data.today || []).length + ')';
      if (cn) cn.textContent = '(' + (data.tomorrow || []).length + ')';
      if (nd && data.next_day) nd.textContent = new Date(data.next_day + 'T00:00:00+05:30').toLocaleDateString('en-GB', {day: '2-digit', month: 'short', timeZone: 'Asia/Kolkata'});
    }
    render();
  }

  const boot = setInterval(() => {
    if (setup()) { clearInterval(boot); load(); timer = timer || setInterval(load, 30000); }
  }, 1000);
})();
