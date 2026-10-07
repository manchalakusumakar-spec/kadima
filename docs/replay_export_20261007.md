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
