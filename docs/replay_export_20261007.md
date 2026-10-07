# Restore existing suggestion identity in audit CSV

Parent: PR20 `d584fe0eba27a8f23d2dfd2bac8fb68cb48501ce`.

`trade_audit.record` already freezes an immutable `audit_id`, accepts `signal_id`
and captures `holding_minutes`. `stock_fast.Worker.process` records before and
after `bind_policy`. No schema or stock propagation change is needed for those
fields in this pinned source.

The CSV omitted `audit_id` and exported only execution `signal_id`. A suggestion
that was not dispatched can legitimately have no execution signal. This patch
appends Suggestion ID, Producer, Episode, Exchange, Token and Proposed holding
basis using already persisted fields. Existing 41 columns and their order stay
unchanged. No UPDATE, backfill, ID regeneration or schema migration is introduced.
Re-exporting a surviving original database can recover its existing IDs. Nothing
can recreate immutable historical IDs from the October 5 CSV alone.

The index producer has projection horizons but does not provide a fixed maximum
holding policy. Missing holding minutes remain unavailable, never defaulted to
15. Existing plan freezing, fees/BUY-fill gaps and terminal observations are
unchanged. This does not make the October 5 evidence replayable or prove any
profitability improvement. PR28 capture work is independently based on
`263d52a7e8a825d08ae0b0e92ff744db5612d51f`; these branches must not be blindly
composed or taken as evidence of deployment.

Validation: six new offline tests pass with sockets blocked, covering suggestion
ID export without a signal, frozen plan and later signal linking through restart,
distinct same-contract episodes/exchanges, null original horizons, unchanged
schema and formula-safe text. No deployment or activation performed.

## CI completion

The first complete CI run at `a68b76c5966bb6dc5c01fe4354cb95db7487ad25`
correctly rejected a stale release hash for `kadima/trade_audit.py` (two errors
and two failures across 828 tests). The source patch passed its six focused
tests, but the release manifest needed the corresponding source fingerprint.
Only that manifest entry is updated, from
`66b12fa755e28e4d14e85c6691596f9bc7b6ec5c` to
`f365be0cca09bc41c71f6252fbdeb55bf6f3f507`. The source-check implementation,
all other hashes, exposure checks and activation behavior remain unchanged.
The existing CI workflow now also runs the six export regressions explicitly.
No installer or activation entrypoint is executed by this work.
