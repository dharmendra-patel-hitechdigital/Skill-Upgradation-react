# System Design

A walkthrough of how this service is built and **why**, written so the design can
be defended in a review: the constraints, the alternatives that were rejected,
the failure modes, and where the design stops working.

---

## 1. The problem

> Accept documents from users, understand them with AI, and expose the result
> through a secure API.

"Understand" means: get the text out (which for a scan means OCR), work out what
kind of document it is, summarise it, pull out the business-critical fields, and
let a user ask questions about it.

Three properties shape everything below:

| Constraint | Consequence |
|---|---|
| The work is **slow** — OCR plus an LLM call is seconds to minutes | The request cannot wait for it |
| The work is **expensive** — every AI call costs money | Duplicate work must be designed out, not tolerated |
| The work is **unreliable** — it depends on third-party services | Failure is a normal state, not an exception |

Most of the design follows from taking those three seriously.

---

## 2. Layers

```
   HTTP           app/api/          Routing, status codes, auth dependencies
     │                              Thin: no business logic lives here
     ▼
   Use cases      app/services/     Orchestration, business rules
     │                              Raises domain errors, knows nothing of HTTP
     ▼
   Data access    app/repositories/ Queries. No commits — caller owns the transaction
     │
     ▼
   Persistence    app/models/       SQLAlchemy ORM
```

Cross-cutting: `app/core/` (config, database, security, logging, errors) and
`app/schemas/` (the validated boundary).

**The dependency rule is one-directional.** A service never imports from `api/`.
This is not architecture for its own sake — it buys two concrete things:

1. **Testability.** Business logic is callable without an HTTP client.
2. **Reusability.** The same `process_document(id)` runs from an endpoint, a
   background task, a CLI, or a future queue consumer with no changes.

The rule is enforced by a discipline that is easy to check in review: services
raise `AppError` subclasses, never `fastapi.HTTPException`. A single set of
handlers in `main.py` maps those to HTTP responses, so the status code for a
given domain failure is decided in exactly one place.

### The AI layer is behind protocols

```python
class TextExtractor(Protocol):     # bytes -> text
    def supports(self, content_type: str) -> bool: ...
    async def extract(self, data, *, content_type, filename) -> TextExtractionResult: ...

class DocumentAnalyzer(Protocol):  # text -> structure, text+question -> answer
    async def analyze(self, text, *, filename, content_type) -> AnalysisResult: ...
    async def answer_question(self, text, question, *, filename) -> AnswerResult: ...
```

No business code imports `openai` or `boto3`. Four implementations exist:

| Protocol | Cloud | Built-in |
|---|---|---|
| `TextExtractor` | `TextractExtractor` | `LocalTextExtractor` (pypdf) |
| `DocumentAnalyzer` | `OpenAIAnalyzer` | `HeuristicAnalyzer` (rules) |

This is the single highest-leverage decision in the codebase. It is why:

- the feature is **demonstrable immediately** after clone, with no account;
- **CI tests the real pipeline** with no network, no mocks of someone else's SDK,
  and no spend;
- a **provider outage degrades quality** instead of returning 503 to every user;
- swapping to Azure OpenAI, Claude, or Google Document AI is a new file, not a
  refactor.

---

## 3. The document pipeline

### Request path (fast)

```
POST /documents
  │
  ├─ read body in 1 MiB chunks, aborting past the limit   ← not Content-Length
  ├─ identify the real type from magic bytes              ← not the declared type
  ├─ SHA-256 → already uploaded by this user? → 200 + existing record
  ├─ write blob to storage
  ├─ INSERT document (status=pending)
  ├─ COMMIT
  ├─ schedule background task                             ← after commit, always
  └─ 202 Accepted
```

Two ordering details that are deliberate:

- **Blob before row.** An orphaned object is cheap to reap; a row pointing at a
  file that was never written is a permanent 500 on read.
- **Schedule after commit.** The background task opens its *own* session and
  would not see an uncommitted row.

### Background path (slow)

```
0. claim     UPDATE documents SET status='processing'
             WHERE id=? AND status='pending'          ← the DB picks the winner
1. fetch     read bytes back from storage
2. extract   bytes → text     (local pypdf | Textract)
3. analyse   text  → structure (OpenAI | rule engine)
4. persist   write extraction, mark completed
   on error  mark failed with an error code + an actionable message
```

**Why the claim is an atomic UPDATE.** The status check lives in the `WHERE`
clause, so two workers racing on the same document cannot both start a billable
AI pipeline — the database decides, not application code. The loser sees
`rowcount == 0` and backs off. This is the same reason the pattern generalises
unchanged to multiple replicas later.

**Why three short transactions instead of one.** Stages 1–3 run with no
transaction open. Holding one across a 60-second network call pins a pooled
connection, blocks other writers, and risks the database killing an
idle-in-transaction session. The job's inputs are snapshotted into a frozen
dataclass at claim time, so the slow section cannot accidentally touch a closed
session.

**Why `process_document` never raises.** Every failure is recorded on the row as
a status plus an error code, so the API can always explain what happened. An
escaping exception would only reach the task runner's logger and leave the row
stuck in `processing` forever.

### State machine

```
  PENDING ──▶ PROCESSING ──▶ COMPLETED ──┐
                    │                     │ reprocess
                    └──▶ FAILED ──────────┤
                              retry       │
                    ◀─────────────────────┘
```

Transitions are declared as data (`_ALLOWED_TRANSITIONS`) rather than scattered
`if` statements, which makes the rule checkable and testable in isolation.
`COMPLETED → PROCESSING` is illegal: a re-run must go via `PENDING` so it is
claimed by the same atomic path as any other job.

### Failure modes and their handling

| Failure | Response |
|---|---|
| Unsupported / mislabelled file | 415 at upload, before anything is stored |
| Oversized file | 413, aborted mid-stream — never fully buffered |
| Scan with no text layer, Textract off | `failed`, message names `TEXTRACT_ENABLED` |
| OpenAI outage or quota exhausted | Falls back to the rule engine, result carries a warning |
| OpenAI returns nonsense values | Coerced and clamped; one bad field does not lose the document |
| OpenAI response truncated (`finish_reason=length`) | Clear error, not a confusing `JSONDecodeError` |
| Processing exceeds the timeout | `failed` with `processing_timeout` |
| Worker killed mid-pipeline | Startup sweep returns it to `failed` — see below |
| Storage object missing | `failed` with `storage_error` |
| Unexpected bug in the pipeline | `failed` with `internal_error` + the exception type; full traceback logged |

**Crash recovery.** Background work lives in this process, so a deploy or crash
mid-pipeline leaves rows in `processing` with nothing running. On startup,
`recover_stuck_documents()` sweeps rows whose `processing_started_at` is older
than the timeout and marks them `failed`. Marking them **`failed` rather than
`pending`** is deliberate: automatic retry could re-bill a half-completed AI call
on every restart loop, so a human or an explicit API call decides to retry.

---

## 4. Data model

```
users ──┬──< refresh_tokens
        │
        └──< documents ──┬──1:1── document_extractions
                         └──< document_events
```

| Table | Shape | Why separate |
|---|---|---|
| `documents` | Small, hot, heavily queried | Listing and filtering must stay cheap |
| `document_extractions` | Large (raw text can be MBs), read rarely | Listing 50 documents must not drag 50 MB through the DB |
| `document_events` | Append-only audit trail | Answers "why did this fail at 2am, and which provider was to blame" |

**Denormalisation, on purpose.** `document_type` and `page_count` are copied from
the extraction onto `documents` so the list endpoint can filter and display
without a join. The duplication is written in exactly one place (the persist
stage), so it cannot drift.

**JSON columns for `entities` / `fields` / `keywords`.** The shape varies by
document type — an invoice has line items, a contract has parties — and these
values are never queried *into*. A normalised `extracted_field` table would add
joins and migrations for no query benefit. If field-level search is ever needed,
that is the point to normalise.

**Indexes are chosen from the actual query.** The default listing is "this
owner's documents, optionally filtered by status, newest first", so the composite
index is `(owner_id, status, created_at)`. Sorting always appends a primary-key
tiebreak, so pagination is stable when two uploads share a timestamp.

**`UNIQUE (owner_id, checksum_sha256)`** implements deduplication *per user*. Two
different users uploading the same public PDF get their own records — dedup must
never leak one user's document to another.

### Two subtle correctness fixes worth naming

**Enum storage.** SQLAlchemy persists an enum's member *name* (`"ADMIN"`) by
default. That silently disagrees with the JSON API, the column's `server_default`
(`'admin'`), and anything a human types in a SQL console — so a row inserted by
raw SQL or a data migration reads back as `LookupError`. `portable_enum()` sets
`values_callable`, so the database stores the same lowercase values the API uses.

**Timezones.** SQLite and MySQL `DATETIME` do not persist an offset, so a value
written as timezone-aware comes back *naive*. Comparing that against
`datetime.now(UTC)` raises `TypeError`; arithmetic on it silently treats a UTC
instant as local time. The `UtcDateTime` type normalises on write and re-attaches
UTC on read, so the rest of the codebase only ever handles aware UTC datetimes on
every supported database.

---

## 5. Security

### Token design

| | Access | Refresh |
|---|---|---|
| Lifetime | 30 min | 7 days |
| Server state | None | `jti` only |
| Reusable | Yes | **No — single use** |

Both carry a `typ` claim that is checked on use, so a long-lived refresh token
cannot authorise an API call. Both carry `iss`/`aud`, so a token minted here
cannot be replayed against a sibling service sharing a key.

Access tokens are stateless — no database hit on the hot path. Refresh tokens are
tracked, which buys three things statelessness cannot: real logout, session
listing, and **reuse detection**.

### Reuse detection, and the distinction that makes it usable

Redeeming a refresh token revokes it and issues a new pair. Presenting an
*already redeemed* token means either a buggy client or a stolen token, and they
are indistinguishable — so the safe assumption is theft and **every session for
that user is revoked**.

The non-obvious part: this must only fire for tokens consumed by **rotation**. A
token revoked by **logout** being presented again is a mundane stale client (a
background tab, a retried request). Conflating the two would let any user log out
and then accidentally lock every one of their own devices out. Hence
`revoked_reason` on the token row — it is load-bearing, not bookkeeping.

The mass revocation is also **committed explicitly** before the 401 is raised.
The request is about to fail, and the session dependency rolls back on exception
— without that commit, the entire security response would be silently discarded.

### Other measures

| Concern | Measure |
|---|---|
| Password storage | bcrypt, per-password salt, never logged |
| Account enumeration | Unknown email burns one bcrypt verify; identical message and timing to a wrong password |
| Privilege escalation | `role` is not in `UserUpdate`, so it cannot be set by a profile PATCH |
| Stale authorisation | Active status read from the DB per request, so deactivation is immediate |
| Password compromise | Changing a password revokes every session |
| Admin lockout | An admin cannot demote, deactivate, or delete themselves |
| IDOR | Ownership enforced *inside* the query; a foreign id returns 404, not 403 |
| Path traversal | Object keys are generated, never derived from the filename; the local backend additionally verifies the resolved path stays under the root |
| Content-type spoofing | Real format detected from magic bytes; the declared type is not trusted |
| Upload DoS | Streaming size cap on bytes received, not on `Content-Length` |
| Header injection | `Content-Disposition` filenames are ASCII-folded and quoted, with RFC 5987 for Unicode |
| Info leak | Unhandled exceptions return an opaque 500; the traceback goes to logs only |
| Misconfiguration | Production refuses the default `SECRET_KEY`, `DEBUG=true`, or wildcard CORS |

---

## 6. Cost and reliability of the AI layer

AI calls are the expensive, unreliable part. Controls, in the order they apply:

1. **Deduplication** — identical bytes are never analysed twice for a user.
2. **Right engine for the job** — a digital PDF uses the local reader even when
   Textract is enabled. The characters are already in the file; paying for OCR
   adds cost and latency for no accuracy gain.
3. **Input cap** — `LLM_MAX_INPUT_CHARS` truncates at a paragraph boundary, and
   the truncation is surfaced as a warning rather than silently losing content.
4. **Concurrency cap** — `PROCESSING_MAX_CONCURRENCY` bounds in-flight pipelines,
   so a hundred simultaneous uploads do not open a hundred concurrent AI calls.
5. **Atomic claim** — no double processing.
6. **Reprocess guard** — 409 if a document is already queued or running.
7. **SDK-level retries** — the OpenAI client retries connection errors, 429s and
   5xx with jittered backoff, and does *not* retry deterministic 400s.
8. **Per-document attribution** — provider, model, prompt and completion tokens,
   and stage timings are stored on every extraction, so cost is attributable per
   document rather than as one opaque monthly bill.

### Output quality controls

Structured outputs with `strict: true` constrain the model at decode time to our
exact keys, types and enum members — no parsing prose out of a code fence. The
schema's enums are generated from the same `StrEnum`s the API serves, so the
model's allowed values, the stored values and the OpenAPI docs cannot drift.

But **strict schemas constrain shape, not sanity**. A model can still return a
confidence of `1.4`, a 30 000-character summary, or an entity type it invented
despite the enum. `_coerce_analysis()` repairs those before validation, so one
bad value degrades a field instead of discarding an otherwise good analysis.

For Q&A, grounding is enforced three ways: the prompt forbids outside knowledge,
`answer_found` gives the model an explicit way to say "not in this document", and
`quotes` returns verbatim supporting passages so a caller can verify the answer
rather than trust it.

---

## 7. Observability

Every request gets an id (generated, or taken from an inbound `X-Request-ID`)
stored in a `ContextVar`. Because contextvars follow the async task, **every log
line down the stack is stamped with it automatically — including the background
AI pipeline**, since the id is explicitly captured into the task. That is what
makes a multi-stage async pipeline debuggable.

The same id is returned in the response header *and* inside the error envelope,
so a user reporting a failure can quote one value that finds the exact trace.

- `LOG_JSON=true` emits one JSON object per line for CloudWatch/Loki/Datadog.
- The `document_events` table is a durable, queryable audit trail independent of
  log retention.
- `/health/live` and `/health/ready` are separate because orchestrators use them
  for opposite decisions: liveness failure means *restart me* (so it must never
  touch a dependency), readiness failure means *stop routing traffic here*.
- `/health/providers` answers "why did my scan fail?" without shell access.

One trap worth naming, because it bit this codebase during development: passing
`extra={"filename": ...}` to the logger raises `KeyError`, since `filename` is a
reserved `LogRecord` attribute. The failure only fires when that line is actually
emitted — which was on an error path, so the error handler itself crashed.
`safe_extra()` prefixes colliding keys, and a regression test covers it.

---

## 8. Where this design stops working

Honest limits, with the upgrade path for each.

| Limit | Symptom | Fix |
|---|---|---|
| **In-process background work** | Work lost on restart; no distribution across replicas | Move `schedule_processing` to Celery/RQ/SQS. The pipeline entry point is already `process_document(document_id)` reading state from the DB, so this changes *how it is invoked*, not the pipeline. |
| **Local storage** | With >1 replica, a file uploaded to A is invisible to B | `STORAGE_BACKEND=s3` — one config line, no code change |
| **SQLite** | Serialised writers, "database is locked" | MySQL/PostgreSQL |
| **Polling for status** | Chatty clients | Webhooks or SSE on the existing `document_events` |
| **Whole document in one prompt** | Very long documents get truncated | Chunk + embed + retrieve (RAG); the extraction table already stores the text to chunk |
| **No full-text search** | Cannot search inside document text | Postgres `tsvector`, or OpenSearch fed from `document_extractions` |
| **HS256 shared secret** | Every service verifying tokens can also mint them | RS256/asymmetric keys, or a real IdP |
| **No per-user rate limiting** | One user can monopolise the AI budget | Redis token bucket at the API gateway or in middleware |

The reason these are cheap to fix is the layering: each one is a swap at a
boundary that already exists, not a rewrite.

---

## 9. Request lifecycle, end to end

Putting it together — an upload from HTTP to stored result:

```
1  RequestContextMiddleware   assign request id, start timer
2  CORSMiddleware             origin check
3  SecurityHeadersMiddleware  nosniff, DENY, no-referrer
4  Route match                POST /api/v1/documents
5  Dependency: get_db         open AsyncSession
6  Dependency: CurrentUser    decode JWT (typ/iss/aud/exp) → load user → active?
7  Endpoint                   delegate to document_service.upload_document
8    validate                 size cap, magic-byte sniff, allow-list
9    dedupe                   SHA-256 → existing? → 200 + existing record
10   store                    StorageBackend.save (temp file + atomic rename)
11   persist                  INSERT document + "uploaded" event
12 Commit                     endpoint owns the transaction boundary
13 Schedule                   task_runner.submit (after commit; id propagated)
14 Response                   202 + X-Request-ID + X-Process-Time-Ms
15 Access log                 one structured line, stamped with the request id
   ─────────────────────────── request ends; the rest is background ───────────
16 Semaphore                  wait for a concurrency slot
17 Claim                      atomic PENDING → PROCESSING (own session, commit)
18 Extract                    storage.load → TextExtractor  (no transaction open)
19 Analyse                    DocumentAnalyzer, with fallback on provider error
20 Persist                    upsert extraction + mark completed + event (commit)
```

Any failure from 17 onward lands the document in `failed` with a code and a
message the API can show, and a log line carrying the same request id as step 1.

---

## 10. Summary

The design is driven by three facts about the work: it is slow, expensive, and
unreliable.

- **Slow** → 202 + polling, background processing, short transactions, async I/O.
- **Expensive** → deduplication, atomic claim, concurrency and input caps,
  cheapest-adequate engine, per-document cost attribution.
- **Unreliable** → protocol-based providers, graceful degradation to built-in
  engines, an explicit state machine, an audit trail, crash recovery, and errors
  that name the setting that fixes them.

Everything else — the layering, the single error envelope, the request-id
propagation, the typed schemas shared between the API and the model — exists to
keep those three properties true as the codebase grows.
