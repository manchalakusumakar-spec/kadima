# Resolve source mismatches before retrying installation

The supplied EC2 screenshot reports:

```
BLOCKED: unreviewed_dependency:kadima/feed.py
```

In the pinned installer at 58380aff0fd4ed0830c7d14dfed32ceba029b148 this guard
runs in backend_plan, before service/process preparation, the broker preflight,
source replacement or service shutdown. The blocked attempt therefore did not
reach any of those operations. This is not a claim about activity by already
running services or about earlier/manual deployments.

The message establishes that the installed feed.py bytes differ from the
reviewed release. It does not identify the installed file's hash, prove damage,
or establish that it is safe to overwrite. The reviewed feed.py blob is
3d0cd2a4614d20646256fdba8638f8b238d6a0ff. Do not guess the installed hash or
broaden the installer allowlist based only on this error.

## Collect the complete static dependency mismatch inventory

From a separate reviewed checkout run:

```sh
sudo -n python3 -I -B -S stock_fast_update/review_dependencies.py
```

This new standalone tool reports all mismatches it finds in one traversal,
not just the first exception. It follows statically referenced shipped Python
modules from the four installation files and package initializer, and explicitly
checks the preserved FIFO limiter. Imports discovered in differing installed
files are also traversed when those modules are shipped in the release.

Expected base entrypoint/scanner upgrades and the two missing new stock modules
are listed as planned updates, not as unreviewed dependencies. Deployment
config.py is preserved and syntax-checked without printing its contents or
fingerprint. Source text, exception strings and credentials are not printed.
Each other mismatch includes its exact installed and reviewed Git blob hashes.
Missing, symlinked, unreadable, nonregular, oversized or invalid-syntax source is
reported rather than approved. Nothing is imported from the live package or
executed from its files. The tool makes no broker or HTTP call, invokes no
installer or service command, and has no apply or force option.

The default text output is intended for a phone terminal. --json prints the
full sanitized inventory, including whether both files have the same parsed
syntax tree. Syntax-tree equivalence is a diagnostic hint only: it does not
approve an unreviewed hash, alter any guard, or authorize installation.

This is a static source inventory, not an atomic deployment lock, runtime
integration test, live-module identity check, gateway audit or broker readiness
assessment. Dynamic imports and modules not shipped in the release are outside
its graph. The existing installer still checks the complete set of its own
required conditions before any installation.

## Follow-through

Use the reported AWS hashes to locate the exact source versions for review.
A locally changed file may not have a GitHub blob and may require its source
through a private upload. Do not publish raw potentially credential-bearing
source as an issue comment. Reconcile verified compatible changes into the
review branch with tests, retain other deployed fixes, and rerun the guarded
installer only after source compatibility is established.

This amendment changes no production file, strategy, permission, installed
feed, installer guard or accepted-version list. It adds only this documentation,
the passive inventory and its tests. Twenty-three targeted offline tests passed
locally, including multiple mismatches, config/source privacy, missing files,
FIFO checks, syntax failures, source nonexecution and unchanged live fixtures.
Uploaded tool and test blob hashes match the locally tested bytes. Full CI
status for the new commit must be checked separately.
