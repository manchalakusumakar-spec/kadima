"""Show the Hourly Forecast inside KADIMA Celestial (dashboard.html), replacing
the Daily Price Memory section on screen. Display only; backs up first.

    sudo python3 tools/install_celestial_embed.py            # install / update
    sudo python3 tools/install_celestial_embed.py --remove   # undo (Daily Price Memory returns)

Copies web/hourly_forecast.html -> /var/www/html/forecast.html and
web/celestial_forecast_embed.js -> /var/www/html/, then adds one <script> tag
to dashboard.html. Running it again only refreshes the two copied files.
"""
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WWW = os.environ.get('KADIMA_WWW', '/var/www/html')
DASH = os.path.join(WWW, 'dashboard.html')
TAG = '<script src="celestial_forecast_embed.js?v=4"></script><!-- HOURLY FORECAST v1 -->'
OLD = re.compile(r'<script src="celestial_forecast_embed\.js[^"]*"></script><!-- HOURLY FORECAST v1 -->')


def main():
    if not os.path.exists(DASH):
        print('STOPPED - not found:', DASH)
        return 1
    with open(DASH) as f:
        html = f.read()
    stamp = time.strftime('%Y%m%d-%H%M%S')
    if '--remove' in sys.argv:
        if not OLD.search(html):
            print('Nothing to remove.')
            return 0
        shutil.copy2(DASH, DASH + '.bak-' + stamp)
        with open(DASH, 'w') as f:
            f.write(OLD.sub('', html))
        print('REMOVED - Daily Price Memory is back. Backup:', DASH + '.bak-' + stamp)
        return 0
    shutil.copy2(os.path.join(ROOT, 'web', 'hourly_forecast.html'), os.path.join(WWW, 'forecast.html'))
    shutil.copy2(os.path.join(ROOT, 'web', 'celestial_forecast_embed.js'),
                 os.path.join(WWW, 'celestial_forecast_embed.js'))
    if OLD.search(html):
        if TAG not in html:
            with open(DASH, 'w') as f:
                f.write(OLD.sub(TAG, html))
        print('UPDATED - panel files refreshed; dashboard.html already has the forecast.')
        return 0
    shutil.copy2(DASH, DASH + '.bak-' + stamp)
    i = html.lower().rfind('</body>')
    html = html[:i] + TAG + '\n' + html[i:] if i >= 0 else html + '\n' + TAG + '\n'
    with open(DASH, 'w') as f:
        f.write(html)
    print('INSTALL_OK - reload Celestial. Backup:', DASH + '.bak-' + stamp)
    print('Undo: sudo python3 tools/install_celestial_embed.py --remove')
    return 0


if __name__ == '__main__':
    sys.exit(main())
