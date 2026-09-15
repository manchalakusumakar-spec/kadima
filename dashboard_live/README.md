# KADIMA — Celestial Watch live dashboard update

Built for the existing AWS dashboard at https://18-61-12-184.sslip.io/dashboard.html.

## What changes

- Retains Celestial Watch's purple/gold identity and dashboard sections.
- Uses /state, /dashboard_data, /universe_snapshot and /macro_snapshot through an authenticated local gateway.
- Refreshes state/positions every 4 seconds and scanner/macro every 20 seconds, without overlapping requests.
- Tracks endpoint connection failures independently; timestamps render in IST.
- Clears all displayed account data on logout. Sessions expire after 8 hours and are revoked on gateway restart.
- Removes random strikes, scores, predictions and fabricated confidence/grades. Candidate strikes and forecasts are unavailable because the existing responses do not provide them.
- Uses backend Gamma tiers and scores rather than turning Shield scores into Gamma probabilities.
- Charts actual observed spot samples in 5-minute buckets while the browser is open; this is not historical OHLC or a forecast.
- Preserves explicit manual-exit interaction for all reported open positions. Never automatically submits trades or retries exit requests.
- Escapes external strings, blocks non-HTTP news links and disables position actions if the dashboard response is stale/disconnected.

## Known backend limitations

The current _position_view() substitutes peak/entry prices if broker LTP polling fails and has no quote timestamp. The dashboard labels LTP freshness unverified and does not claim the displayed P&L is a current broker mark. Source snapshots can be stale even when their HTTP endpoint responds successfully. Full F&O coverage depends on the separate scanner; the page shows only reported instruments. MCX feeds/calendars, ranked candidate strikes, full historical candles and forward forecasts are not supplied by these endpoints. This update does not fabricate those capabilities or change the trading engine.

## Authentication

The old uploaded HTML contained a production API token. No original credential is included in these files. The new gateway stores the existing API token in /etc/kadima-dashboard/config.json, owned root:ubuntu mode 0640 under a 0750 directory. It accepts a separate dashboard password, hashed with scrypt and a random salt. A Secure, HttpOnly, SameSite=Strict cookie authorizes browser calls. Mutations check the exact configured Origin. Only four read endpoints and manual_exit are proxied; no arbitrary URL or endpoint proxy exists. Login is capped at 8 attempts per five minutes globally for this single-user deployment.

The original shared API token is NOT rotated by this installer, to avoid breaking existing connected clients. Since the old page exposed it, schedule a coordinated rotation of KADIMA_API_TOKEN in the engine, existing clients, and gateway config. Until that rotation, the old credential may still authorize direct calls to the existing APIs. Old backups also contain that credential and are kept in a root-only backup directory. External AWS firewall/Nginx access policies are not changed by this update except for the new dashboard-api route.

## Install on AWS

Run `python3 install.py` as ubuntu from this directory. It verifies dependencies and existing authenticated local state access, locates the exact existing Nginx dashboard block, asks for a new dashboard password privately, and backs up dashboard/Nginx before modifying them. It obtains the existing token from PM2 configuration when available, otherwise prompts privately. Do not paste credentials in chat or command arguments.

Creates a separate systemd gateway on 127.0.0.1:8082 and adds /dashboard-api/ to the existing Nginx server. Checks gateway health and nginx syntax before serving the updated page. Does not restart PM2, the trading engine, or Angel One. The browser URL stays unchanged. A rollback command is printed. On installation failure the original HTML/Nginx are restored and the gateway is stopped. The installer refuses to overwrite an existing dashboard gateway installation.

After installation: open the dashboard, sign in using the new password, verify each connection badge and source timestamp. Inspect market/position data; do not click Exit solely as a test, since it is a real action. Installation and AWS connectivity cannot be validated from the editing environment, which cannot reach the server.
