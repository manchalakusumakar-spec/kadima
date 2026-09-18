# Review and staging instructions

Backend files for this change are `kadima/stock_fast.py`,
`kadima/stock_fast_api.py`, `kadima/full_universe_scanner.py` and root `run.py`.
The base is `bde4c2952b54996b6f3be9a891df3167a00f20e7`. Existing dependency
branches must be resolved before promotion; do not run an old installer that
omits the new modules and then assume stock-fast is installed.

No production installer or global flag writer is provided in this patch. This
avoids overwriting unknown AWS-local or newer CAS/dashboard changes. Use the
project's reviewed flat-account deployment procedure after reconciling source
hashes, service entry points, configuration and broker/local pending exposure.
Keep a complete rollback backup; do not roll back across unresolved exposure.

Validation from a complete reviewed checkout:

```sh
python -m unittest discover -s tests -v
python -m compileall -q kadima stock_fast_update run.py
node --check stock_fast_update/panel.js
```

The dashboard/gateway utility is additive and check-only by default. Point it
at the actual reviewed staging files, not guessed service paths:

```sh
python stock_fast_update/upgrade_dashboard.py --dashboard /staging/dashboard.html --gateway /staging/gateway.py
```

After reviewing the planned files, append `--apply` to write them with backups.
This does not restart a service or enable trading. Copy/restart only through
the reviewed deployment process. GET `/dashboard-api/stock_fast` and POST
`/dashboard-api/celestial/kadima/stock-fast/config` reuse the existing protected
gateway; an unauthenticated/origin-mismatched request must fail.

Opening the page never arms the strategy. After deployment verification, the
operator's stock live request remains conditional on the already authorized
fast master, broker and global trading permissions. It does not require a new
manual confirmation for each qualified trade. See `docs/stock_fast_live.md` for
scope, holding policy, tests and outstanding live-validation requirements.
