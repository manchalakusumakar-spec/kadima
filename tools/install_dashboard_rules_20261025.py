#!/usr/bin/env python3
"""DASHBOARD RULES v1 (2026-10-25): show tonight's rules and loss room in Celestial.

control_api.py: the Celestial top-20 response gains a read-only `risk_controls` block
(daily loss limit, morning cash, realised P&L, room left, Sniper / Gamma CONFIRMED /
Scalp v2 settings). Every field is wrapped so a failure only blanks the strip.
dashboard.html: a rules strip above the Gamma Blast table, strike concentration under
the tier, and a green outline on CONFIRMED rows. No controls, no writes.

Anchor-based, nothing written unless all anchors match once. Backups *.bak-dashrules-20261025.
Usage: --root ~/kadima-py --html /var/www/html/dashboard.html [--check | --rollback]
"""
import argparse, os, shutil, sys

MARK = 'DASHBOARD RULES v1'
SUFFIX = '.bak-dashrules-20261025'

API = [
    ("""        readiness_note='Candidate prechecks only; execution rechecks alignment, structure, sizing, slots and broker state.',
""",
     """        readiness_note='Candidate prechecks only; execution rechecks alignment, structure, sizing, slots and broker state.',
        risk_controls=_risk_controls(),  # DASHBOARD RULES v1
"""),
]
API_TAIL = '''

def _risk_controls():  # DASHBOARD RULES v1 (2026-10-25): read-only display data
    out = {}
    try:
        from . import daily_limit, exit_manager
        lim, pnl = float(daily_limit.limit()), float(exit_manager.realized_pnl_today())
        out.update(daily_loss_limit=round(lim), morning_cash=daily_limit.morning_cash(),
                   realized_pnl=round(pnl, 2), loss_room=round(lim + min(0.0, pnl)),
                   daily_limit_mode=daily_limit.config()['mode'], daily_limit_pct=daily_limit.config()['pct'])
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import sniper
        c = sniper.config()
        out['sniper'] = {k: c.get(k) for k in ('mode', 'indices', 'window_start', 'window_end', 'max_trades',
                                               'max_lots', 'max_risk_cash_pct', 'stop_after_loss', 'ab_filter')}
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import gamma_confirm
        out['gamma_confirm'] = dict(gamma_confirm.config())
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import scalp_v2
        c = scalp_v2.config()
        out['scalp_v2'] = dict(entries=c.get('entries'), confirm_gate=c.get('confirm_gate'),
                               score=list(getattr(scalp_v2, 'CONFIRM_SCORE', ())),
                               window=['%02d:%02d' % w for w in getattr(scalp_v2, 'CONFIRM_WINDOW', ())])
    except Exception:  # noqa: BLE001
        pass
    try:
        out['score_threshold'] = celestial.read_config()['score_threshold']
        out['auto_trade_enabled'] = celestial.read_config()['auto_trade_enabled']
    except Exception:  # noqa: BLE001
        pass
    return out
'''

HTML = [
    ("""<span id="gammaSource">Celestial contract ranking · +5% premium radar · No fixed upper profit target</span></p>""",
     """<span id="gammaSource">Celestial contract ranking · +5% premium radar · No fixed upper profit target</span></p>
<div id="riskStrip" class="krules" role="status" aria-live="polite">Auto-trade rules: waiting for the server</div><!-- DASHBOARD RULES v1 -->"""),
    ("""if(gbHot(r))tr.className='gb-hot';""",
     """if(gbHot(r))tr.className='gb-hot';if(r.tier==='CONFIRMED')tr.classList.add('gb-confirmed');"""),
    ("""format(r.final_score,1),r.tier,format(r.volume_ratio,1),""",
     """format(r.final_score,1),(r.tier||'—')+(r.strike_concentration_hhi!=null?'\\nHHI '+Number(r.strike_concentration_hhi).toFixed(3):''),format(r.volume_ratio,1),"""),
    ("""if(!data.candidates.length)empty(body,25,'No fresh qualifying candidates. The desk never fills empty places with weak or stale contracts.');""",
     """if(!data.candidates.length)empty(body,25,'No fresh qualifying candidates. The desk never fills empty places with weak or stale contracts.');(window.renderRiskStrip||(()=>{}))(data.risk_controls);"""),
    ("""</body>""",
     """<style>/* DASHBOARD RULES v1 */
.krules{margin:8px 0 10px;padding:8px 10px;border-radius:8px;border:1px solid #2b3a55;background:#101a2e;font-size:13px;line-height:1.6}
.krules b{color:#e6edf7}.krules .ok{color:#22c55e}.krules .warn{color:#f59e0b}.krules .bad{color:#ef4444}.krules .off{color:#94a3b8}
#candidates tr.gb-confirmed{outline:2px solid #22c55e;outline-offset:-2px}
#candidates tr.gb-confirmed td{font-weight:700}
</style>
<script>/* DASHBOARD RULES v1: read-only rules strip */
(function(){
  const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const inr=v=>v==null||isNaN(v)?'—':(Number(v)<0?'−':'')+'₹'+Math.abs(Math.round(Number(v))).toLocaleString('en-IN');
  const mode=m=>'<span class="'+(m==='live'?'ok':m==='shadow'?'warn':'off')+'">'+esc(m||'unknown')+'</span>';
  window.renderRiskStrip=function(rc){
    const el=document.getElementById('riskStrip'); if(!el) return;
    if(!rc||typeof rc!=='object'){el.textContent='Auto-trade rules: not reported by the server';return}
    const g=rc.gamma_confirm||{}, s=rc.sniper||{}, sc=rc.scalp_v2||{};
    const room=rc.loss_room, roomCls=room==null?'off':room<=0?'bad':room<(rc.daily_loss_limit||0)*0.5?'warn':'ok';
    const parts=[
      '<b>Gamma CONFIRMED</b> '+mode(g.mode)+': score ≥ '+esc(rc.score_threshold??'—')+', HHI ≥ '+esc(g.min_hhi??'—')+', '+esc(g.window_start||'—')+'–'+esc(g.window_end||'—')+', risk ≤ '+(g.risk_fraction!=null?Math.round(g.risk_fraction*100)+'%':'—'),
      '<b>Sniper</b> '+mode(s.mode)+': '+esc((s.indices||'').replaceAll(',',', '))+' · '+esc(s.max_lots??'—')+' lot · max '+esc(s.max_trades??'—')+'/day'+(s.stop_after_loss?' · stops after a loss':''),
      '<b>Scalp v2</b> '+mode(sc.entries)+': score '+esc((sc.score||[]).join('–'))+', '+esc((sc.window||[]).join('–')),
      '<b>Daily loss limit</b> '+inr(rc.daily_loss_limit)+(rc.morning_cash?' ('+esc(rc.daily_limit_pct)+'% of morning cash '+inr(rc.morning_cash)+')':' (morning cash not read yet)')+' · realised today '+inr(rc.realized_pnl)+' · <span class="'+roomCls+'">room left '+inr(room)+'</span>',
      '<b>Auto trading</b> '+(rc.auto_trade_enabled?'<span class="ok">armed</span>':'<span class="warn">paused</span>')
    ];
    el.innerHTML=parts.join('<br>');
  };
})();
</script>
</body>"""),
]


def patch(path, items, tail=''):
    src = open(path).read()
    if MARK in src:
        sys.exit(f'{os.path.basename(path)}: already contains {MARK}; nothing changed')
    for old, new in items:
        n = src.count(old)
        if n != 1:
            sys.exit(f'{os.path.basename(path)}: anchor found {n} times: {old.strip()[:70]!r}; nothing changed')
        src = src.replace(old, new)
    return src + tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--html', required=True)
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rollback', action='store_true')
    a = ap.parse_args()
    api = os.path.join(os.path.expanduser(a.root), 'kadima', 'control_api.py')
    if a.rollback:
        for p in (api, a.html):
            if os.path.exists(p + SUFFIX):
                shutil.copy2(p + SUFFIX, p); print('restored', p)
        return
    new_api = patch(api, API, API_TAIL)
    compile(new_api, api, 'exec')
    new_html = patch(a.html, HTML)
    if a.check:
        print('check ok: control_api.py, dashboard.html'); return
    for p, text in ((api, new_api), (a.html, new_html)):
        shutil.copy2(p, p + SUFFIX)
        open(p, 'w').write(text); print('written', p)


if __name__ == '__main__':
    main()
