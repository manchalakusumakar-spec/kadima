# KADIMA Celestial dashboard

Source for the existing `/dashboard.html` page: Top 20 candidates, managed and
adopted positions, source status, threshold dropdown and authenticated controls.
No live deployment is implied by this directory. See
[current release boundaries](../docs/celestial_release.md).

The gateway keeps the existing broker token server-side. Browser actions use
a Secure/HttpOnly/SameSite session cookie and exact-origin checks. Only named
read/write routes are allowed. Threshold changes, BUY, EXIT, stop/trailing,
cancel, pause/resume and kill pass through the central executor's checks.
BUY accepts only a ranked candidate ID; EXIT is limited to verified managed
long positions. Order acceptance is not fill confirmation.

`install.py` is the historical fresh-install path. It refuses an existing
gateway. It is not the updater and its PM2 assumptions must be verified on the
actual host.

For an existing gateway, `upgrade.py` verifies the installed HTML/gateway
against `upgrade_manifest.json` or the current release, verifies the backend
config route and gateway process owner, and defaults to check-only. With
`--apply`, it backs up both files, replaces them, restarts only the dashboard
gateway, checks its health and verifies final hashes. Failure restores the
old HTML/gateway. Existing private config, password and Nginx are preserved.
It does not enable trading or deploy/restart the engine.

```bash
sudo -n python3 dashboard_live/upgrade.py
# Only after the release and deployed source have been reviewed:
sudo -n python3 dashboard_live/upgrade.py --apply
```

Real EC2 source ownership, browser rendering, source freshness and broker
reconciliation must be verified before live use. Locally intercepted fixture:
`node tests/test_celestial_dashboard.cjs`. All fixture traffic stays at
`kadima.test`; it sends no live orders.
