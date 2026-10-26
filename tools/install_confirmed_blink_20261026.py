import shutil, sys
p = '/var/www/html/dashboard.html'
s = open(p).read()
if 'CONFIRMED BLINK v1' in s:
    sys.exit('already installed')
if s.count('</body>') != 1:
    sys.exit('anchor count %d' % s.count('</body>'))
css = """<style>/* CONFIRMED BLINK v1 (2026-10-26): only CONFIRMED rows blink (green); score>50 rows steady */
#candidates tr.gb-hot td{animation:none;background:rgba(255,212,0,.14);color:inherit;font-weight:600}
#candidates tr.gb-hot{outline:1px solid #ffd400;outline-offset:-1px}
@keyframes gbConfirmBlink{0%,49%{background:#22c55e;color:#04210f}50%,100%{background:#065f46;color:#fff}}
#candidates tr.gb-confirmed td{animation:gbConfirmBlink 1s step-end infinite;font-weight:800}
#candidates tr.gb-confirmed{outline:2px solid #22c55e;outline-offset:-2px}
#gbAlert{animation:none}
@media (prefers-reduced-motion:reduce){#candidates tr.gb-confirmed td{animation:none;background:#22c55e;color:#04210f}}
</style>
</body>"""
shutil.copy2(p, p + '.bak-blink-20261026')
open(p, 'w').write(s.replace('</body>', css))
print('installed')
