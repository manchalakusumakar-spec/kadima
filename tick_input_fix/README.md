# Validate incoming tick prices before updating engine state

The post-restart report supplied on 18 September showed fresh OI for the four
main indices, 38 reconciliation completions, and 28 Shield evaluations with
failed score/hold checks. No OI polling failures or confirmed entries appeared
in those retained records. The fast lane separately reported invalid/unclosed
bars, stale observations and a candle gap.

An offline source-level reproduction found that the tick handler forwarded
missing, zero, negative, NaN, infinite and boolean prices to the engine and
refreshed its feed heartbeat. The engine then updated spot freshness and could
invalidate Shield state; the candle builder could persist invalid OHLC prices.
This is a reproduced validation defect. The aggregate server counts do not
establish that these ticks caused the reported historical rejections.

The fix rejects invalid prices and missing token identities before updating
feed liveness, queuing persistence or calling the engine. Valid prices retain
the existing paise conversion, volume and depth handling. A rejected tick leaves
the last valid observation untouched so its existing freshness checks can expire.
The fix does not repair historical bars or invent replacement market data.

Five offline tests exercise the actual callback, including invalid-price
rejection, token validation, valid normalization, valid/invalid/valid continuity
and a full storage queue. No SDK connection or order is used.

## Install

From a separate extracted copy of this commit:

```bash
python3 -B -m unittest discover -s tests -p test_tick_price_validation.py -v
python3 -B tick_input_fix/install.py --root /home/ubuntu/kadima-py
sudo python3 -B tick_input_fix/install.py --root /home/ubuntu/kadima-py --apply
```

The installer verifies the original `feed.py`, creates a backup and atomically
replaces that file. Unknown local versions are refused. Reapplying the same
version is a no-op. Existing quote scheduling and entry conditions are unchanged.
The operator must reload the running `kadima.service` in the appropriate
maintenance window for the new handler to take effect. No service is changed
by this installer and no live trading control is activated.

After reload, examine new `tick_rejected` counts, valid spot/OI update ages,
newly closed candle quality and confirmation reasons. Existing bad bars can
remain in lookback windows and require separate source-backed investigation.
Absence of an entry still requires checking the actual setup and execution
conditions; this repair cannot guarantee a trade or validate broker fills.

For rollback in the same operator maintenance procedure, restore the printed
`feed.py.tick-backup-*` file to `kadima/feed.py` and reload the engine.
