# One-time setup: automatically arm global and stock trading on market days

This setup replaces the daily operator activation step, **not** the signal,
risk, news or execution rules. Its installer is intended for the observed
already-installed stock-fast release at `966030d2ee9dbc1c5e543d331281bffc19330634`
with preserved AWS feed and FIFO limiter, from PR #10. No direct broker order
is sent by the setup or scheduler. Once enabled, the running trading engine can
submit real orders when its existing gates and the new session gate pass.

## Run once, including on a weekend

Extract this reviewed commit into a temporary release checkout, then run:

```sh
sudo -n python3 -I -B -S session_auto/install.py --apply
```

The default without `--apply` is plan-only. Apply requires the existing global
and stock controls to be paused, broker order permission already on, kill off,
reviewed source/launch identities, and current empty local and broker position /
pending-order books. It performs the broker check again after verified shutdown.
It may refresh authentication, temporarily restart engine/scanner, add dedicated
systemd environment files and install a small entry-only executor patch. It does
not replace the whole checkout, modify PM2/dashboard/passwords, reset kills,
reduce scores or remove stops. Do not trade manually or run other maintenance
concurrently. Broker preflights are consecutive observations, not an account lock.

Installed scheduler files are root-owned under `/opt/kadima-session-auto`.
The nonsecret policy is `/etc/kadima-session-auto/policy.json`, outside the
potentially private `/etc/kadima` tree. The root-only systemd environment file
stays at `/etc/kadima/session-auto.env`. No existing directory permissions are
relaxed. Directory validation runs before the broker preflight, and the
installed policy is read as the engine owner before services restart. See
`docs/session_auto_private_directory_fix.md` for the blocked-attempt recovery.

A systemd timer checks on boot and 60 seconds after each preceding check finishes.
No ChatGPT task, open browser, live chat or daily button press is needed. EC2 must
remain powered on and the services and broker/data connections must be operational.

## What happens on each ordinary market working day

From 09:00 IST the scheduler checks the installed stock API, worker health,
reconciliation and news health. When ready it requests the existing authenticated
global resume endpoint, then the revision-checked stock-live configuration
endpoint. The separate stock-live switch is included automatically; no manual
"Request live stock entries" step remains. Market data/contract/score/size/risk/
slot checks are still enforced by the central executor for each trade.

This arms the existing Celestial global strategies and the fast-master setting,
not just stocks. Index entries still require real index news coverage. The
existing collector's stock coverage must not be misrepresented as index coverage.
No new index-news data, new CAS implementation, or MCX schedule is supplied here.

Readiness failures are retried on later timer runs; health records are never
invented. Once armed, degraded health revokes a short-lived entry permit, and
recovery can restore the permit without repeatedly toggling controls. A lost
write response is reconciled against predicted revisions instead of blindly
repeating permission changes. Unexpected revisions fail closed.

The entry permit is checked both at the start and immediately before submission
in `AutoExecutor.evaluate_and_enter`. It expires after at most 150 seconds,
is tied to the current boot/day, and also checks the actual current calendar
and time. Missing/invalid calendar, missing/expired permit, scheduler stoppage,
wrong boot, future timestamps and clock reversal deny **new** NFO/BFO entries.
No guard is added to exits. Stops, existing time exits, partial-fill handling,
cancellations and reconciliation remain the existing executor's responsibility.

Entries are allowed no earlier than 09:15 IST. The outer entry ceiling is 15:35,
matching the reviewed existing signal cutoff, not asserting an exchange close.
Earlier strategy/broker limits remain authoritative; in particular stock-fast's
5/10/15-minute horizon and its 15:15 exit-request policy are unchanged. Outside
the window, weekends and listed holidays revoke the permit and request that both
auto intents pause. Exit logic is not stopped to pause new entries. An already
pending broker order can still fill: revoking the permit does not cancel it.

An **observed** intraday manual/risk pause is respected for the rest of that
session; the scheduler does not switch it back on. A kill latch is never reset.
The next ordinary market day can arm again unless the operator disabled the
scheduler or a kill/readiness gate remains. A user-authorized same-day retry is
available with `runner.py --resume-today`; it only removes the scheduler hold,
not any broker, news, risk, calendar or kill gate. Configuration changes while
waiting/arming also cause a hold rather than override an operator action.

## Calendar boundary — explicitly not a perpetual calendar guarantee

The shipped normal-session policy covers **19 September–31 December 2026**,
excludes Saturdays/Sundays and the published 2026 equity/F&O holiday dates, and
applies the shared normal-session exclusions conservatively to NFO and BFO.
Sources checked on 19 September 2026:

- NSE official trading holiday API, `FO` segment:
  https://www.nseindia.com/api/holiday-master?type=trading
- NSE official holiday page:
  https://www.nseindia.com/resources/exchange-communication-holidays
- BSE's published notice 20251212-8 for Equity/Equity Derivatives (distributed
  copy); it provides the same remaining 2026 ordinary-session holiday dates.

There is **no automatic online calendar refresh in this version**. New exchange
closures/circulars require updating the reviewed policy. Beyond the valid date
range the system blocks with `calendar_update_required`, rather than guessing
2027 working days. Special Saturday, Budget or Muhurat sessions are excluded
until explicitly reviewed. MCX has a different calendar and is not altered.

## Verify, pause, or re-enable the schedule

```sh
systemctl is-enabled kadima-session-auto.timer
systemctl is-active kadima-session-auto.timer
sudo -n python3 -I -B -S /opt/kadima-session-auto/runner.py --status
```

Status `non_trading_day` on a weekend means the schedule is installed and waiting,
not that installation failed. `AUTO_AND_STOCK_ARMED_SUBJECT_TO_ENTRY_GATES` means
both permissions and the expiring permit were confirmed; it is not a filled order
or proof of strategy profitability. The existing dashboard shows actual global /
stock requests separately. An active timer is not a claim that today's entries
are enabled. Scheduler reasons are also in `journalctl -u kadima-session-auto`.

To persistently disable scheduled entry permission without stopping protective
position monitoring:

```sh
sudo -n python3 -I -B -S /opt/kadima-session-auto/runner.py --disable
```

The disable marker survives reboot. `--enable` explicitly removes it and allows
guarded arming again. `--resume-today` clears only the scheduler's observed manual
pause for this day; neither command clears a kill latch or relaxes trade gates.

## Deployment / validation limitations

The executor patch is derived from one exact reviewed source hash with exactly
two anchored insertions. Unknown/custom source is rejected. It changes only
`evaluate_and_enter`, never exit methods. The installed executor's hash changes;
future releases/activation helpers must reconcile this intentional change, not
restore an older unguarded version. Metadata and old bytes are backed up under
`/var/backups/kadima-session-auto-*`. Pre-restart failures restore changed files;
after a restart attempt, guarded code is retained for exposure-aware recovery.
The database is never rolled back. The scheduler is enabled only after permission
and process postchecks pass. A repeated setup is status-only, not a hidden rearm.

Local tests cover the calendar, permit expiry/boot/clock, retries, both-control
arming, missing/stale health, pause/kill preservation, lost-response recovery,
cutoff crossing, and entry-only patch construction. The full-checkout patch test
must pass in repository CI. Tests use temporary fixtures and mocked local APIs;
they do not contact AWS, log in to Angel One, validate live fills or backtest the
strategy. Existing whole-repository CI remains separate from operational checks.
The new one-time installer has not been run on AWS by this development session.
