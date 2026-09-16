# Updates at any hour

The user-authorized deployment policy allows server modifications 24/7. The
19:00–08:00 IST gate has been removed from the consolidated executor installer
and the policy, stock-connection and forecaster installers. No override flag is
needed. Application trading hours are a separate setting.

To install only the deployment-tool change from a pinned release:

```bash
sudo -n python3 tools/install_update_policy.py
```

This copies the four reviewed installer scripts into `/home/ubuntu/kadima-py`,
adds their manifests only if absent, and saves backups. It validates existing
installer hashes before changing any file and restores files on write failure.
It makes no broker requests and does not restart services, replace application
source, alter trading flags, or write the trading database. An unreviewed local
installer is reported without overwriting it. Existing manifests are preserved;
future application releases should still be installed from their pinned package.

Application installations remain guarded: matching source versions, no unresolved
local positions or execution intents, and broker confirmation of no open exposure
or pending orders are required before service shutdown. The local/broker check is
repeated after shutdown before replacing source. If an entry arrives between the
checks, installation aborts and the prior services are restarted.

Source rollback remains automatic for a failure before restart. Once restart is
attempted, an entry may already have been submitted. A later failure therefore
retains the installed source, attempts to recover services and reports that
service health and broker/local exposure must be checked before another recovery
deployment. The database is never rolled back.

Offline tests exercise all four installers at all 24 hours, exposure blocks
before shutdown, exposure detected after shutdown, pre-restart rollback and
post-restart source retention. Broker calls and service operations are mocked.
