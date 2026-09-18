/* Stock Fast controls: no write on page load, no credentials in the browser. */
(() => {
  'use strict';
  if (document.getElementById('kadima-stock-fast')) return;
  const panel = document.createElement('section');
  panel.id = 'kadima-stock-fast';
  panel.style.cssText = 'margin:20px;padding:20px;border:1px solid currentColor;border-radius:12px';
  const title = document.createElement('h2'); title.textContent = 'Stock Fast Momentum';
  const status = document.createElement('p');
  const note = document.createElement('p');
  note.textContent = 'Live requested is not a fill. Existing broker, risk and kill-switch checks remain active. Pausing entries does not cancel position protection.';
  const controls = document.createElement('div');
  const select = document.createElement('select');
  select.setAttribute('aria-label', 'Maximum holding time for new stock-fast positions');
  [5, 10, 15].forEach(minutes => {
    const option = document.createElement('option'); option.value = String(minutes);
    option.textContent = `${minutes}-minute maximum hold`; select.appendChild(option);
  });
  const arm = document.createElement('button'); arm.textContent = 'Request live stock entries';
  const pause = document.createElement('button'); pause.textContent = 'Pause stock entries';
  const save = document.createElement('button'); save.textContent = 'Save holding time';
  const table = document.createElement('pre');
  table.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere';
  [select, save, arm, pause].forEach(el => { el.style.marginRight = '10px'; controls.appendChild(el); });
  [title, status, controls, note, table].forEach(el => panel.appendChild(el));
  document.body.appendChild(panel);
  let current = null, pending = false, dirty = false;
  select.addEventListener('change', () => { dirty = true; });
  function availability() {
    [save, arm, pause].forEach(b => { b.disabled = pending || !current; });
  }
  async function refresh() {
    if (pending) return;
    try {
      const response = await fetch('/dashboard-api/stock_fast', {credentials:'same-origin', cache:'no-store'});
      if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to view stock-fast controls.' : 'Stock-fast backend unavailable.');
      const data = await response.json(); current = data.controls;
      if (!dirty) select.value = String(current.holding_minutes);
      const health = data.worker || {};
      status.textContent = `Stock live requested: ${current.live_enabled ? 'YES' : 'NO'} | Worker: ${data.worker_alive ? 'running' : 'not running'} | Fast master: ${data.fast_master_permission ? 'enabled' : 'disabled'} | Global auto: ${data.global_auto_requested ? 'requested' : 'paused'} | Fresh roots: ${health.fresh_roots ?? 'unknown'} | Warmed: ${health.warmed_roots ?? 'unknown'} | Waiting for candle preparation: ${health.preparation_pending ?? 'unknown'}`;
      const positionLines = (data.positions || []).map(p => `${p.symbol}: ${p.execution_stage}, owned quantity ${p.filled_owned_quantity}, maximum hold ${p.holding_minutes} minutes`);
      const lines = (data.rows || []).map(r => `${r.underlying} ${r.symbol || ''} | ${r.stage} | score ${r.score ?? '—'} | ask reference ${r.entry ?? '—'} | stop ${r.stop ?? '—'} | quote ${r.quote_at || 'unknown'} | ${(r.blocked_reasons || []).join(', ') || 'strategy ready; shared execution gates still apply'}`);
      table.textContent = [...positionLines, `Displaying up to 20 of ${data.total_observed_rows} observed rows.`, ...lines].join('\n');
    } catch (error) {
      current = null; status.textContent = error.message;
      table.textContent = 'Unavailable — no cached candidate is treated as live.';
    }
    availability();
  }
  async function write(patch) {
    if (pending || !current) return;
    pending = true; availability();
    try {
      const response = await fetch('/dashboard-api/celestial/kadima/stock-fast/config', {
        method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({patch, expected_revision:current.revision})
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Control change rejected; refresh before retrying.');
      current = result; dirty = false;
    } catch (error) {
      status.textContent = error.message;
    } finally {
      pending = false; availability();
    }
  }
  arm.addEventListener('click', () => write({live_enabled:true}));
  pause.addEventListener('click', () => write({live_enabled:false}));
  save.addEventListener('click', () => write({holding_minutes:Number(select.value)}));
  availability(); refresh(); setInterval(refresh, 5000);
})();
