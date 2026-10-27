#!/usr/bin/env python3
"""CONFIRMED MERGE v1 + SCALP CAP v1 (trader confirmed 2026-10-28 00:10 IST).

1. Celestial: the "Gamma Blast suggestions" box in Trade Audit becomes one merged table
   "CONFIRMED trades - today": CONFIRMED Gamma Blast + CONFIRMED Scalp suggestions, every
   trade KADIMA took, and manual trades (tagged MANUAL). Same colours as Trade Audit
   (light green T1, dark green T2, red stop). A "Show all suggestions" switch shows everything.
   The main Trade Audit table opens on the same "CONFIRMED + manual" filter ("All trade types"
   still selectable).
   trade_audit.py: new column confirmed_at, stamped once the first time a recorded row has
   tier CONFIRMED (CONFIRMED is live-only and moves, so the plan keeps the first time it passed).
   Filter value strategy=CONFIRMED -> confirmed_at set, OR strategy MANUAL, OR OPEN/CLOSED/PENDING.
2. auto_executor.py: index scalps (no-cap sizing) are capped at scalp_capital.json "max_lots"
   (default 2). Stock blast lanes keep their own sizing.

Anchor-based; nothing written unless every anchor matches once. Backups *.bak-confmerge-20261028.
Usage: --root ~/kadima-py --html /var/www/html/dashboard.html [--check | --rollback]
"""
import argparse, os, shutil, sys

MARK = 'CONFIRMED MERGE v1'
SUFFIX = '.bak-confmerge-20261028'

TA = [
("""    c.execute('CREATE INDEX IF NOT EXISTS trade_audit_orders_signal ON trade_audit_orders(signal_id,recorded_at)')
    return c""",
"""    c.execute('CREATE INDEX IF NOT EXISTS trade_audit_orders_signal ON trade_audit_orders(signal_id,recorded_at)')
    _confirmed_column(c)   # CONFIRMED MERGE v1
    return c"""),
("""                  (text(row.get('stage') or row.get('tier'), 60), blockers, at.isoformat(), aid, at.isoformat()))
""",
"""                  (text(row.get('stage') or row.get('tier'), 60), blockers, at.isoformat(), aid, at.isoformat()))
        if row.get('tier') == 'CONFIRMED' or row.get('stage') == 'CONFIRMED':   # CONFIRMED MERGE v1
            c.execute('UPDATE trade_audit_plans SET confirmed_at=? WHERE audit_id=? AND confirmed_at IS NULL',
                      (at.isoformat(), aid))
"""),
("""    if strategy not in (None, '', 'ALL') and strategy not in STRATEGIES:""",
"""    if strategy not in (None, '', 'ALL', 'CONFIRMED') and strategy not in STRATEGIES:   # CONFIRMED MERGE v1"""),
("""    if strategy and strategy != 'ALL': where.append('a.strategy=?'); params.append(strategy)""",
"""    if strategy and strategy not in ('ALL', 'CONFIRMED'): where.append('a.strategy=?'); params.append(strategy)"""),
("""    final_where = ' WHERE execution_status=?' if status and status != 'ALL' else ''
    final_params = params + ([status] if final_where else [])""",
"""    conds, final_params = [], list(params)
    if status and status != 'ALL':
        conds.append('execution_status=?'); final_params.append(status)
    if strategy == 'CONFIRMED':   # CONFIRMED MERGE v1: CONFIRMED suggestions, manual and taken trades
        conds.append("(confirmed_at IS NOT NULL OR strategy='MANUAL' OR execution_status IN ('OPEN','CLOSED','PENDING'))")
    final_where = (' WHERE ' + ' AND '.join(conds)) if conds else ''"""),
]
TA_TAIL = '''

_confirmed_ready = False


def _confirmed_column(c):
    """CONFIRMED MERGE v1 (2026-10-28): add trade_audit_plans.confirmed_at once."""
    global _confirmed_ready
    if _confirmed_ready:
        return
    cols = {r[1] for r in c.execute('PRAGMA table_info(trade_audit_plans)')}
    if 'confirmed_at' not in cols:
        try:
            c.execute('ALTER TABLE trade_audit_plans ADD COLUMN confirmed_at TEXT')
        except Exception as exc:  # noqa: BLE001 - the other process may have added it first
            if 'duplicate column' not in str(exc).lower():
                raise
    _confirmed_ready = True
'''

AE = [(
"""            allowed_lots = min(allowed_lots, int(allocation // (limit_price * (1 + trade_policy.COST_RESERVE) * lot_size)))
""",
"""            allowed_lots = min(allowed_lots, int(allocation // (limit_price * (1 + trade_policy.COST_RESERVE) * lot_size)))
            if not _sb:   # SCALP CAP v1 (CONFIRMED MERGE v1 deploy): index scalps max N lots
                allowed_lots = min(allowed_lots, _scalp_max_lots())
""")]
AE_TAIL = '''

SCALP_MAX_LOTS_DEFAULT = 2   # SCALP CAP v1 (trader 2026-10-28): index scalps at most 2 lots


def _scalp_max_lots():  # SCALP CAP v1
    import json, os
    from .config import DATA_DIR
    try:
        with open(os.path.join(DATA_DIR, 'scalp_capital.json')) as f:
            v = json.load(f).get('max_lots')
        if type(v) is int and 1 <= v <= NOCAP_MAX_LOTS:
            return v
    except (OSError, ValueError, AttributeError):
        pass
    return SCALP_MAX_LOTS_DEFAULT
'''

BOX_START = "const box=document.createElement('div');box.id='gbSuggest';"
BOX_END = "load();setInterval(load,20000);"
BOX_NEW = r"""const box=document.createElement('div');box.id='gbSuggest';let showAll=false;/* CONFIRMED MERGE v1 */
box.innerHTML='<div class="toolbar"><h3>CONFIRMED trades · today · Gamma Blast + Scalp + Manual</h3><button type="button" class="small" id="gbShowAll" aria-pressed="false">Show all suggestions</button></div>'+
 '<p class="note" id="gbSuggestNote">Only CONFIRMED suggestions, trades KADIMA took and your manual trades. <span class="audit-key audit-t1">T1 observed</span> <span class="audit-key audit-t2">T2 observed</span> <span class="audit-key audit-sl">Stop observed</span> Results are quote observations; P&amp;L is from real fills, before fees.</p>'+
 '<div class="scroll" tabindex="0" role="region" aria-label="CONFIRMED trades" style="max-height:420px;overflow:auto"><table><thead><tr><th>Contract</th><th>Type</th><th>Suggested IST</th><th>Score</th><th>Entry ₹</th><th>Stop ₹</th><th>T1 ₹</th><th>T2 ₹</th><th>Last LTP ₹</th><th>High ₹</th><th>Result</th><th>Execution</th><th>Actual entry ₹</th><th>Actual exit ₹</th><th>P&amp;L ₹</th></tr></thead><tbody></tbody></table></div><p class="note" id="gbSuggestStatus">Loading…</p>';
const bar=audit.querySelector('.toolbar');if(bar)bar.after(box);else audit.prepend(box);
const body=box.querySelector('tbody'),status=box.querySelector('#gbSuggestStatus'),toggle=box.querySelector('#gbShowAll');
const kindOf=r=>r.strategy==='MANUAL'?'MANUAL':(r.strategy_label||r.strategy||'—')+(r.confirmed_at?' · CONFIRMED':'');
toggle.onclick=()=>{showAll=!showAll;toggle.setAttribute('aria-pressed',String(showAll));toggle.textContent=showAll?'Show CONFIRMED only':'Show all suggestions';load()};
async function load(){const c=new AbortController(),t=setTimeout(()=>c.abort(),10000);
 try{const r=await fetch('/dashboard-api/audit?day='+istDay()+'&strategy='+(showAll?'ALL':'CONFIRMED')+'&status=ALL&limit=200&offset=0',{credentials:'same-origin',cache:'no-store',signal:c.signal});
  if(!r.ok)throw Error('HTTP '+r.status);const d=await r.json();body.replaceChildren();let pnl=0,closed=0;
  for(const r of d.rows||[]){const p=r.plan||{},tr=document.createElement('tr');
   tr.className=r.observed_result==='SL_OBSERVED'?'audit-sl':r.observed_result==='T2_OBSERVED'?'audit-t2':r.observed_result==='T1_OBSERVED'?'audit-t1':'';
   if(typeof r.realized_pnl==='number'&&isFinite(r.realized_pnl)){pnl+=r.realized_pnl;closed++}
   [(p.strike?fmt(p.strike)+' '+(r.direction||'')+' · ':'')+r.symbol,kindOf(r),ist(r.suggested_at),fmt(r.score),fmt(p.entry??p.entry_reference),fmt(p.stop),fmt(p.t1),fmt(p.t2),fmt(r.ltp),fmt(r.high_ltp),
    String(r.observed_result||'—').replaceAll('_',' '),r.execution_status||'—',fmt(r.actual_entry),fmt(r.actual_exit),fmt(r.realized_pnl)].forEach((v,i)=>{const td=document.createElement('td');td.textContent=v;if(i===1&&r.strategy==='MANUAL')td.style.fontWeight='800';tr.append(td)});body.append(tr)}
  if(!(d.rows||[]).length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=15;td.textContent=showAll?'No suggestions yet today.':'No CONFIRMED or taken trades yet today.';tr.append(td);body.append(tr)}
  status.textContent=(d.total??0)+(showAll?' suggestions':' CONFIRMED / taken / manual trades')+' today'+(closed?' · realized P&L ₹'+fmt(pnl)+' ('+closed+' with fills)':'')+(d.total>200?' · showing latest 200':'')+' · updated '+ist(new Date().toISOString())}
 catch(e){status.textContent='CONFIRMED trades unavailable: '+e.message}finally{clearTimeout(t)}}
"""
HTML = [
("""for(const [k,v] of Object.entries(strategies))option($('Strategy'),k,v);""",
"""option($('Strategy'),'CONFIRMED','CONFIRMED + manual');for(const [k,v] of Object.entries(strategies))option($('Strategy'),k,v);$('Strategy').value='CONFIRMED';/* CONFIRMED MERGE v1 */"""),
("""let filters={day:$('Day').value,strategy:'ALL',status:'ALL',symbol:''};""",
"""let filters={day:$('Day').value,strategy:'CONFIRMED',status:'ALL',symbol:''};"""),
]


def patch(path, items, tail='', mark=MARK):
    src = open(path).read()
    if mark in src:
        sys.exit(f'{os.path.basename(path)}: already contains {mark}; nothing changed')
    for old, new in items:
        if src.count(old) != 1:
            sys.exit(f'{os.path.basename(path)}: anchor found {src.count(old)} times: {old.strip()[:70]!r}; nothing changed')
        src = src.replace(old, new)
    return src + tail


def patch_html(path):
    src = patch(path, HTML)
    if src.count(BOX_START) != 1 or src.count(BOX_END) != 1:
        sys.exit(f'dashboard: box anchors {src.count(BOX_START)}/{src.count(BOX_END)}; nothing changed')
    a = src.index(BOX_START)
    b = src.index(BOX_END, a)
    if b - a > 4000:
        sys.exit('dashboard: box block unexpectedly long; nothing changed')
    return src[:a] + BOX_NEW + src[b:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--html', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    k = os.path.join(os.path.expanduser(a.root), 'kadima')
    ta, ae = os.path.join(k, 'trade_audit.py'), os.path.join(k, 'auto_executor.py')
    if a.rollback:
        for p in (ta, ae, a.html):
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        return
    out = {ta: patch(ta, TA, TA_TAIL), ae: patch(ae, AE, AE_TAIL, 'SCALP CAP v1'), a.html: patch_html(a.html)}
    for p in (ta, ae):
        compile(out[p], p, 'exec')
    if a.check:
        print('check ok:', ', '.join(os.path.basename(p) for p in out)); return
    for p, text in out.items():
        shutil.copy2(p, p + SUFFIX)
        open(p, 'w').write(text); print('written', p)


if __name__ == '__main__':
    main()
