#!/usr/bin/env bash
# Install hourly forecast hf-2.0.1 (12 Oct 2026) on KADIMA-BRIDGE.
#   bash install_forecast_201.sh <folder with the verified files>     # check + install, no restart
# Refuses (changes nothing) if any live file differs from the expected "before" hash, or if
# KADIMA holds an open position or an unresolved order. The restart is a separate command.
set -euo pipefail
SRC=${1:?folder with the verified files}
APP=${KADIMA_APP:-$HOME/kadima-py}
WEB=${KADIMA_WEB:-/var/www/html}
DB=${KADIMA_DATA_DIR:-/var/lib/kadima}/kadima.db
SUDO=${SUDO-sudo}

echo "== 1. Files from GitHub (claude/forecast-v21)"
( cd "$SRC" && sha256sum -c <<'SUMS'
f5ccc34abd45895729ad10466c2e4f23fa5487cab9d02403fa37b86cae290548  engine.py
b6eb52d5298c1ee7e61d5564f6120d1ede03b214e947d1770caf12bb8e0fcc57  hourly_forecast.py
97e2e155af809938604c06a6d474545b385ca6dfc27714bbb7687df58d9f78f6  hourly_forecast_model.py
bd3a554a773ee40d4174feba5d786f7d168dcea7530692dc364b62dc31453d26  forecast.html
e53204a69998f75df944f57f426af8afc7b21f155a2cbcdf5b1b4aaa8709201f  test_hourly_forecast_201.py
SUMS
)

echo "== 2. Live files are the expected versions"
sha256sum -c <<SUMS
d4ed0266dd08514e7c2d3c3d4e3f3df1cd34f5cf2b0b6b0ae1d3cd29a616895d  $APP/kadima/engine.py
c702310cfcc7f98a61af334a6b428f08215d770e4a314d82e79a3eb0dee1e870  $APP/kadima/hourly_forecast.py
4856671e3fec23a113648ba0ff1d649a8b49d22e3375f58a0d6636101be11fd3  $APP/kadima/hourly_forecast_model.py
e52ec259cd2ee07db2e6fea578ab0a16d49eb08255eed521ff0fbb1c3ba9972f  $WEB/forecast.html
SUMS
[ "$(grep -c 'forecast.html?embed=1&v=4' $WEB/celestial_forecast_embed.js)" = 1 ] || { echo "embed script not as expected"; exit 1; }
[ "$(grep -c 'celestial_forecast_embed.js?v=4' $WEB/dashboard.html)" = 1 ] || { echo "dashboard.html not as expected"; exit 1; }

echo "== 3. No open position and no unresolved order (read-only)"
$APP/venv/bin/python - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
open_pos = c.execute("SELECT signal_id, symbol, status FROM positions WHERE status != 'closed'").fetchall()
orders = c.execute("SELECT signal_id, side, state FROM execution_orders WHERE state != 'terminal'").fetchall()
print('open positions:', open_pos or 'none'); print('unresolved orders:', orders or 'none')
if open_pos or orders:
    print('STOP: restart would interrupt a live position or order. Try again when flat.'); sys.exit(1)
PY

echo "== 4. Backup and install"
B=$APP/backups/forecast_201_$(date +%Y%m%d_%H%M%S); mkdir -p "$B"
cp -p $APP/kadima/engine.py $APP/kadima/hourly_forecast.py $APP/kadima/hourly_forecast_model.py "$B"/
$SUDO cp -p $WEB/forecast.html $WEB/celestial_forecast_embed.js $WEB/dashboard.html "$B"/
cp "$SRC"/engine.py "$SRC"/hourly_forecast.py "$SRC"/hourly_forecast_model.py $APP/kadima/
cp "$SRC"/test_hourly_forecast_201.py $APP/tools/
$SUDO cp "$SRC"/forecast.html $WEB/forecast.html
$SUDO sed -i 's#forecast.html?embed=1&v=4#forecast.html?embed=1\&v=5#' $WEB/celestial_forecast_embed.js
$SUDO sed -i 's#celestial_forecast_embed.js?v=4#celestial_forecast_embed.js?v=5#' $WEB/dashboard.html
$APP/venv/bin/python -m py_compile $APP/kadima/engine.py $APP/kadima/hourly_forecast.py $APP/kadima/hourly_forecast_model.py
sha256sum $APP/kadima/engine.py $APP/kadima/hourly_forecast.py $APP/kadima/hourly_forecast_model.py $WEB/forecast.html
grep -c 'v=5' $WEB/celestial_forecast_embed.js $WEB/dashboard.html
echo "BACKUP: $B"
echo "Installed. Restart with: sudo systemctl restart kadima"
