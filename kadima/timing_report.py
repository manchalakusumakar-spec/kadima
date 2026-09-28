"""
Gamma timing -- automatic Telegram reports (phase 1, read only).

    python -m kadima.timing_report check      # recorder health, market hours
    python -m kadima.timing_report outcome    # today's outcome summary

`check` counts today's recorded Gamma signals and evaluations and any
`timing_recorder_error` in the audit log. `outcome` sends the summary
table written by the 15:50 outcome job. Both only read files and send
one Telegram message through alerts.send(); they never touch the broker,
the database or the running engine.
"""
from __future__ import annotations

import html
import json
import os
import sys
from datetime import datetime

from .config import AUDIT_PATH, IST
from . import timing_outcome


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _count_audit(day: str, event: str) -> int:
    path = os.path.join(AUDIT_PATH, f"{day}.jsonl")
    if not os.path.exists(path):
        return 0
    needle = f'"event":"{event}"'
    with open(path, errors="replace") as fh:
        return sum(1 for line in fh if needle in line)


def check_text(day: str | None = None) -> str:
    day = day or _today()
    recs = timing_outcome.load_records(day)
    signals = {r["signal_id"] for r in recs if r.get("type") == "timing_signal"}
    evals = [r for r in recs if r.get("type") == "timing_eval"]
    errors = _count_audit(day, "timing_recorder_error")
    now = datetime.now(IST).strftime("%H:%M")
    head = "⚠️" if errors else "✅"
    lines = [f"{head} <b>Timing recorder check {now}</b>"]
    if not recs:
        lines.append("No Gamma signal evaluated yet today (normal if none reached CONFIRMED).")
    else:
        last = evals[-1] if evals else {}
        entered = sum(1 for e in evals if e.get("decision_ok"))
        lines.append(f"Signals {len(signals)} · evaluations {len(evals)} · accepted {entered}")
        if last:
            lines.append(f"Last: #{last.get('signal_id')} {html.escape(str(last.get('index')))} "
                         f"{last.get('direction')} at {str(last.get('ts'))[11:16]} — "
                         f"{html.escape(str(last.get('decision_reason')))}")
    lines.append(f"Recorder errors: <b>{errors}</b>")
    return "\n".join(lines)


def outcome_text(day: str | None = None) -> str:
    day = day or _today()
    path = os.path.join(timing_outcome.OUTCOME_DIR, f"{day}.jsonl")
    if not os.path.exists(path):
        return f"⏱ <b>Timing outcomes {day}</b>\nNo outcome file (no Gamma signals recorded today)."
    with open(path) as fh:
        outcomes = [json.loads(l) for l in fh if l.strip()]
    traded = [o for o in outcomes if o.get("status") == "traded"]
    stops = sum(1 for o in outcomes if o.get("stop_touched"))
    body = html.escape(timing_outcome.summary(outcomes))
    if len(body) > 3500:                     # Telegram limit is 4096 characters
        body = body[:3500] + "\n…"
    return (f"⏱ <b>Timing outcomes {day}</b>\n"
            f"Signals {len(outcomes)} · traded {len(traded)} · stop touched {stops}\n"
            f"<pre>{body}</pre>")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "check"
    text = outcome_text() if mode == "outcome" else check_text()
    from . import alerts
    ok = alerts.send(text, bypass=True)
    print(text)
    print("telegram_sent" if ok else "telegram_not_sent (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
