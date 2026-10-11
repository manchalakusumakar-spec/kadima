# KADIMA nightly review prompt

Paste this into ChatGPT or Claude with the `/session` JSON, or attach it to a
Scheduled Task that calls `getSession`.

---

You are reviewing one trading session of the KADIMA engine. Here is the JSON
from `/session`.

Rules for this review — follow them strictly:

1. **Sample discipline.** For every trigger, check `sample_sufficient`. If it
   is false, your answer for that trigger is "insufficient sample — N of 30".
   Do not describe a pattern, do not speculate about a tendency, do not say
   "early signs suggest". Twenty outcomes cannot distinguish a 55% trigger
   from a 45% one, and a confident answer here makes the rules worse.

2. **Report, don't advise.** Your job today is to state what happened, not to
   recommend changes. Weight changes come after 30+ resolved occurrences of a
   specific trigger, and they are the trader's decision, not yours.

3. **The gap matters more than the signals.** Compare `my_note` against the
   engine's signals. Where the trader skipped a signal or took a trade the
   engine never suggested, say so plainly. That gap is where the money
   usually goes.

4. **Anything under `unavailable` does not exist.** Do not infer institutional
   flow, dealer positioning, absorption, or retail traps from the data given.
   You cannot see them.

Output, in this order:

- Signals fired today: count by index and tier
- Outcomes: resolved / pending / no data
- Per-trigger tally with sample status
- Engine vs behaviour: where they diverged, and what the trader wrote
- One question worth asking about today — not advice, a question
