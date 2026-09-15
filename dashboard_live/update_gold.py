"""Apply gold accents and restrained motion to the deployed dashboard only."""
import os
from pathlib import Path
import shutil
import tempfile

STYLE = '''
<style id="kadima-gold-polish">
:root{--gold:#d4af37;--gold-light:#f1d27a;--line:#d4af373d}
h1,h2,a,.warn,.notice{color:var(--gold-light)}
h1{color:var(--gold-light);text-shadow:0 2px 18px #d4af372b}
h2{border-left:3px solid var(--gold);padding-left:12px}
header{border-bottom-color:#d4af3766}
.panel,.card{box-shadow:0 5px 24px #08061124;transition:border-color .22s ease,box-shadow .22s ease,transform .22s ease}
.index,.chartbox{transition:border-color .22s ease,background-color .22s ease}
.index .value{color:var(--gold-light)}
button{transition:background-color .2s ease,border-color .2s ease,box-shadow .2s ease}
button.primary{background:linear-gradient(115deg,#b58c24,#f1d27a 52%,#d4af37);color:#191223;font-weight:650}
button:focus-visible,select:focus-visible,input:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--gold-light);outline-offset:3px}
button:active:not(:disabled){transform:translateY(1px)}
th{color:var(--gold-light)}
tbody tr{transition:background-color .2s ease}
summary{cursor:pointer;color:var(--gold-light);padding:12px 0}
#forecastPanel details{border-bottom:1px solid var(--line)}
#kadimaRain span{color:#d4af37;text-shadow:0 0 9px #d4af3759;opacity:.19}
body>main{animation:kadimaGoldEnter .55s ease-out both}
@keyframes kadimaGoldEnter{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}
@media(hover:hover) and (pointer:fine){
.panel:hover,.card:hover{border-color:#d4af3780;box-shadow:0 8px 28px #0806114d,0 0 18px #d4af3709}
.card:hover{transform:translateY(-2px)}
.index:hover,.chartbox:hover{border-color:#d4af3780;background-color:#d4af3705}
button:hover:not(:disabled){border-color:var(--gold-light);box-shadow:0 0 12px #d4af3726}
tbody tr:hover{background-color:#d4af370b}
}
@media(prefers-reduced-motion:reduce){
body>main{animation:none}
.panel,.card,.index,.chartbox,button,tbody tr{transition:none}
.card:hover,button:active:not(:disabled){transform:none}
}
</style>
'''


def transform(html):
    if '<title>KADIMA' not in html or html.count('</head>')!=1:
        raise ValueError('Unrecognized dashboard; no changes made')
    marker='<style id="kadima-gold-polish">'
    if marker in html:
        start=html.index(marker)
        end=html.index('</style>',start)+len('</style>')
        if html[end:end+1]=='\n':
            end+=1
        html=html[:start]+html[end:]
    # Replace the former pale-yellow accent in CSS, favicon and chart stroke.
    html=html.replace('e8c878','d4af37').replace('E8C878','D4AF37')
    return html.replace('</head>',STYLE.strip()+'\n</head>',1)


def main():
    if os.geteuid()!=0:
        raise SystemExit('Run with sudo -n python3 update_gold.py')
    target=Path('/var/www/html/dashboard.html')
    old=target.read_text();new=transform(old)
    if old==new:
        print('Gold dashboard already installed.')
        return
    backup=Path(tempfile.mkdtemp(prefix='kadima-gold-',dir='/var/backups'))
    shutil.copy2(target,backup/'dashboard.html')
    fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.gold-dashboard-')
    try:
        with os.fdopen(fd,'w') as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp,target.stat().st_mode & 0o777)
        os.chown(tmp,target.stat().st_uid,target.stat().st_gid)
        os.replace(tmp,target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print('Gold accents and subtle motion installed. Refresh with Ctrl+F5.')
    print('Backup:',backup/'dashboard.html')
    print('No services restarted. Trading logic and 96 px/sec scrolling unchanged.')


if __name__=='__main__':
    main()
