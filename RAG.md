# KADIMA audit RAG — first implementation

This standalone module answers historical audit questions from explicitly selected
JSONL logs and text/Markdown documents. It runs outside the trading process. It
does not import the broker, alter positions, start services or modify execution rules.

Prepared against `main` commit `a2c2aa4b0cb9cc4dd847c68475ba20e486052ffd`.
The input schema matches `kadima/audit.py`: `ts`, `event`, and event-specific fields.
The changes are additive: `kadima/rag.py`, `test_rag.py`, and this guide.
The deployed AWS overlays and other pending branches have not been inspected or replaced.

## What is implemented

- Streaming ingestion of explicitly selected `.jsonl`, `.md` and `.txt` files.
- A separate SQLite FTS5 index with BM25 ranking; no embeddings or vector service.
- Required identifier terms and explicit IST date filters.
- Evidence IDs, source line ranges, event timestamps, source hashes and index build time.
- Offline context export, an injectable generation function, and an optional OpenAI
  Responses API adapter for cited answers. A compatible model must be selected explicitly.
- No-evidence abstention, invalid-citation rejection, incomplete-model-response rejection,
  and visible counters for malformed, oversized and undated records.
- Atomic replacement of the module's own index. Rebuild failure preserves the old index;
  successful rebuild removes records from sources that are no longer selected.

Python 3.10+ with SQLite FTS5 is required. No new pip package is required.
The Python bundled with the AWS deployment still needs this feature checked.

## Operator use

Run from a reviewed checkout containing this change, without replacing the running
AWS checkout. Use a completed daily log or a stable private snapshot. A log changing
during indexing causes the build to abort. No engine pause is needed for completed logs.

```bash
python3 -m unittest -v test_rag
python3 -m kadima.rag build \
  --root /var/lib/kadima/audit \
  --file 2026-09-24.jsonl \
  --index /var/lib/kadima/rag/audit.sqlite
python3 -m kadima.rag context \
  --index /var/lib/kadima/rag/audit.sqlite \
  --day 2026-09-24 --term POLICYBZR \
  'What does the log record about blocked POLICYBZR entries?'
```

The operator needs read access to the selected sources and write access to the
separate index directory. The index is created with mode 0600. Do not point `--index`
at `kadima.db` or another operational database. Dates filter event timestamps in IST,
not filenames. Undated documents are excluded when a date filter is set.

`context` is offline and prints a retrieval packet plus model messages. You can
review those records in ChatGPT or pass them to an approved generator. The example
question is illustrative; this change has not inspected the actual POLICYBZR logs.

For direct model answers, configure `OPENAI_API_KEY` securely in the invoking process
and set `KADIMA_RAG_MODEL` to a Responses/Structured Outputs compatible model available
to your API project. Do not put credentials in this repository or on a command line.

```bash
python3 -m kadima.rag ask \
  --index /var/lib/kadima/rag/audit.sqlite \
  --model "$KADIMA_RAG_MODEL" \
  --day 2026-09-24 --term POLICYBZR \
  'What does the log record about blocked POLICYBZR entries?'
```

`ask` sends the question and bounded retrieved evidence/manifest to the OpenAI API
and may incur API charges. It does not transmit the whole index. It requests
`store=false`; this is not a claim of zero provider retention. It uses no model
tools, a fixed HTTPS endpoint, a 45-second socket timeout, no redirects and no
automatic retries. Without matching evidence it makes no model request.
API errors, missing credentials, refusals or bad citations produce
`generation_failed` and no claims (CLI exit 2). Inspect configuration and API account
health separately; raw provider errors are deliberately not exposed.

## Limits and interpretation

- All results are historical snapshots. Index build time is not market-data freshness.
- Search is lexical, not semantic: use exact broker symbols such as `POLICYBZR`.
  `--term` requires that identifier in the record text; it is not an exact symbol-field
  filter. Multiple terms must all match. Matches to question words rank first,
  with identifier-only matches filling any remaining places.
- Top matches are not a full event timeline. No matching record cannot establish
  that a trade was missed, that no order existed, or that the broker was flat.
- Citation IDs prove that a referenced record was retrieved. They do not establish
  that the generated interpretation follows from it. Answers are labelled drafts.
- Rule documents are evidence, not automatically approved/current configuration.
  Generated answers are never written back into the knowledge base or engine.
- Common credential keys and text patterns are masked before indexing, but this
  is best-effort redaction, not a comprehensive data-loss-prevention system. Select
  reviewed audit exports/documents; never index environment files, credentials or
  unrestricted private directories. Inspect offline context before cloud use.
- Selected sources must resolve inside `--root`. Hidden paths, leaf symlinks,
  non-regular files and unsupported extensions are rejected.
- At most 128 source arguments, 64 MiB per file, 256 MiB per build, 64 KiB per
  input line, 100,000 chunks, 4,000 characters per chunk and 12 retrieved chunks.
  Oversized audit records are omitted and counted; oversized lines abort a build.
  There is a SQLite cache target of 4 MiB and a 2-second query progress deadline.
  These bounds are not an AWS memory/performance guarantee.

## Validation and activation status

Offline tests cover source preservation, redaction, IST boundaries, malformed and
oversized records, replacement without stale rows, failed-build preservation,
path containment, query syntax, prompt separation, abstention, citation validation,
provider failure and mocked Responses API transport.

No production logs or paid API requests were used for development verification.
Before AWS activation: test with a reviewed log snapshot, confirm FTS5 support,
configure the model/API project, and evaluate answers against manually verified
events. The dashboard and ChatGPT plugin routes are not wired in this first version.
There is no scheduler, service restart, deployment or auto-trading activation here.

Implementation references:

- [SQLite FTS5 and BM25](https://www.sqlite.org/fts5.html)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
