N50 direct EC2 preparation

Run tools/n50_deploy_check.py --repo /home/ubuntu/kadima-py from a commit-pinned export, after fetching reviewed source a715e3c6e9ae7821cfab7ffc182a30834f63546b through the existing origin authentication. The helper uses only Python's standard library and git. It emits source hashes, service state and memory counters; never config/environment/remote URLs. It does not install, restart, enable trading, clear swap or overwrite local files. A disk source match does not verify loaded Python identity or activation. Source mismatches require an adapter reviewed against the installed version. Current all-50 weights, feed coverage, reconciliation and safe service handoff still need verification. BANKEX/FINNIFTY expiry-only logic is not implemented by this check.

Validation: two focused offline tests passed for classification and failure confidentiality/non-mutation. AWS execution was not performed.
