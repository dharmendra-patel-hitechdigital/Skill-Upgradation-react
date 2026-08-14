# Intelligent Document Service

An async **FastAPI** backend with **JWT authentication**, **SQLAlchemy 2.0**, and an
**AI document-processing pipeline** that extracts text from uploaded files,
classifies and summarises them, pulls out structured fields and entities, and
answers natural-language questions grounded in the document's own text.

Built to be explainable end to end: every non-obvious decision is documented at
the point where it matters, and the trade-offs are stated rather than hidden.

> **It works with no third-party credentials.** Clone, install, run. The AI
> feature is fully functional out of the box using a built-in PDF reader and a
> rule-based analyser. Adding an OpenAI key or AWS Textract upgrades quality and
> adds scanned-image support — it does not switch the feature on.

---

## Contents

- [Quick start](#quick-start)
- [What it does](#what-it-does)
- [Architecture](#architecture)
- [The AI pipeline](#the-ai-pipeline)
- [Authentication](#authentication)
- [API reference](#api-reference)
- [Configuration](#configuration)
- [Database & migrations](#database--migrations)
- [Testing](#testing)
- [Docker](#docker)
- [Production checklist](#production-checklist)
- [Design decisions](#design-decisions)

---

## Quick start

Requires **Python 3.11+**.

```powershell
cd "d:\Project Work\Skill-Upgradation\Python"

# 1. Virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Dependencies
pip install -r requirements.txt

# 3. Configuration (works as-is for local development)
copy .env.example .env

# 4. Schema
alembic upgrade head

# 5. Run
uvicorn app.main:app --reload
```

Open **<http://127.0.0.1:8000/docs>**.

### Try the whole flow in 60 seconds

In Swagger UI:

1. **`POST /api/v1/auth/register`** — create an account.
   The first account on a fresh install becomes an `admin`.
2. **`POST /api/v1/auth/login`** — put your **email** in the `username` field
   (it is an OAuth2 password form). Copy the `access_token`.
3. Click **Authorize** at the top right and paste the token.
4. **`POST /api/v1/documents`** — upload a PDF. You get **202 Accepted** and
   `status: "pending"`.
5. **`GET /api/v1/documents/{id}`** — poll until `status` is `completed`. The
   response now carries the full `extraction`.
6. **`POST /api/v1/documents/{id}/ask`** — ask *"What is the total amount due?"*

No sample PDF handy? Generate a realistic one:

```powershell
python -c "import sys; sys.path.insert(0,'.'); from tests.pdf_builder import invoice_pdf; open('invoice.pdf','wb').write(invoice_pdf())"
```

**Real output** from that invoice, with no API keys configured:

```jsonc
{
  "status": "completed",
  "document_type": "invoice",
  "page_count": 1,
  "processing_duration_ms": 753,
  "extraction": {
    "ocr_provider": "local",          // pypdf text layer, 574 ms
    "analysis_provider": "heuristic", // rule engine, 12 ms
    "confidence": 0.78,
    "summary": "123 Industrial Estate, Manchester, M1 4AB. Bill To: Northwind Trading Company...",
    "fields": [
      { "key": "invoice_number", "value": "INV-2024-00871" },
      { "key": "due_date",       "value": "13/04/2024" },
      { "key": "subtotal",       "value": "1240.00" },
      { "key": "total_amount",   "value": "1488.00" }
    ],
    "entities": [
      { "text": "accounts@northwind-trading.example", "type": "email" },
      { "text": "1488.00",                            "type": "money" },
      { "text": "Acme Technologies Ltd",              "type": "organization" }
    ]
  }
}
```

---

## What it does

| Capability | Detail |
|---|---|
| **Upload & process** | PDF, PNG, JPEG, TIFF, plain text. Type detected from magic bytes, not the client's claim. |
| **Text extraction** | PDF text layer via `pypdf`, or AWS Textract OCR for scans and photos. |
| **Classification** | invoice, receipt, contract, resume, report, letter, form, identity document, bank statement, other. |
| **Summarisation** | 2–4 sentences describing what the document is and its key content. |
| **Field extraction** | Business key/values — invoice number, totals, due dates, account numbers. |
| **Entity extraction** | People, organisations, dates, money, emails, phones, identifiers. |
| **Grounded Q&A** | Natural-language questions answered from the document text, with verbatim supporting quotes and an explicit `answer_found: false` when the answer is absent. |
| **Deduplication** | SHA-256 per user — re-uploading the same file returns the existing record instead of paying for a second AI run. |
| **Audit trail** | Every lifecycle transition recorded with provider, model, token counts and timings. |
| **Auth** | JWT access tokens + single-use rotating refresh tokens, real logout, session listing, RBAC. |

---

## Architecture

```
app/
├── main.py                    App factory, middleware, error handlers, lifespan
├── core/                      Cross-cutting concerns
│   ├── config.py              Settings (env/.env), production safety rails
│   ├── database.py            Async engine, session factory, declarative Base
│   ├── security.py            bcrypt hashing, JWT issue/verify
│   ├── exceptions.py          Domain error hierarchy + the one error envelope
│   ├── logging.py             Structured logs + request-id contextvar
│   └── middleware.py          Request correlation, timing, security headers
├── models/                    SQLAlchemy ORM
│   ├── base.py                UtcDateTime, TimestampMixin, portable_enum
│   ├── user.py                User + UserRole
│   ├── refresh_token.py       Session registry + RevocationReason
│   └── document.py            Document, DocumentExtraction, DocumentEvent
├── schemas/                   Pydantic request/response contracts
│   ├── common.py              Page[T], error envelope, pagination
│   ├── user.py, token.py
│   └── document.py            DocumentAnalysis — shared by the API *and* the LLM
├── repositories/              Data access (no commits; caller owns the transaction)
│   ├── user.py, refresh_token.py, document.py
├── services/                  Use cases / business logic
│   ├── auth_service.py        Registration, login, rotation, logout
│   ├── document_service.py    Upload validation, dedupe, delete, Q&A
│   ├── document_processor.py  The AI pipeline + state machine
│   ├── task_runner.py         Bounded background execution
│   ├── storage.py             Blob storage: local filesystem or S3
│   └── ai/                    AI provider layer
│       ├── base.py            Protocols: TextExtractor, DocumentAnalyzer
│       ├── local_text.py      PDF text layer + plain text  (no credentials)
│       ├── textract.py        AWS Textract OCR            (sync + async paths)
│       ├── openai_analyzer.py OpenAI structured outputs
│       ├── heuristic.py       Rule-based analyser          (no credentials)
│       └── registry.py        Provider selection & graceful degradation
└── api/                       HTTP layer
    ├── deps.py                Auth, pagination, ownership-scoped loaders
    └── v1/endpoints/          auth, users, documents, health
```

**The dependency rule:** each layer depends only on the ones below it.
`api → services → repositories → models`. Services raise *domain* errors and
never import `fastapi.HTTPException`, so the same logic is callable from a CLI,
a worker, or a test. The AI layer is reached only through protocols, so no
business code imports `openai` or `boto3`.

---

## The AI pipeline

Uploading returns **202 Accepted** immediately; processing runs out of band.

```
POST /documents ──▶ validate ──▶ store blob ──▶ INSERT (pending) ──▶ 202 Accepted
                                                       │
                                                       ▼  background task
    ┌──────────────────────────────────────────────────────────────────────┐
    │ 0. claim     atomic UPDATE … WHERE status='pending'  (DB picks winner)│
    │ 1. fetch     read bytes back from storage                             │
    │ 2. extract   bytes → text     local pypdf  │  AWS Textract            │
    │ 3. analyse   text  → structure   OpenAI    │  rule engine             │
    │ 4. persist   write extraction, mark completed                         │
    │    on error  mark failed with a code and an actionable message        │
    └──────────────────────────────────────────────────────────────────────┘
                                                       │
GET /documents/{id} ◀── poll until completed / failed ──┘
```

**Why the request does not wait.** OCR plus an LLM call takes seconds to
minutes. Holding the connection open causes client timeouts, and retries would
duplicate billable work. 202-plus-polling is the correct shape here, and it is
what lets the same pipeline later run on a queue with no API change.

**Three short transactions, not one long one.** Stages 1–3 run with *no*
transaction open. Holding one across a 60-second network call pins a pooled
connection, blocks writers, and risks the database killing an idle-in-transaction
session.

### Provider selection and graceful degradation

| Stage | Preferred | Built-in fallback | Selection |
|---|---|---|---|
| Text extraction | AWS Textract (scans, photos, multi-page) | `pypdf` text layer + plain text | `OCR_PROVIDER` |
| Analysis | OpenAI structured outputs | rule-based analyser | `LLM_PROVIDER` |

Under `auto`:

- A **digital PDF** uses the local reader even when Textract is enabled — the
  characters are already in the file, so paying for OCR adds cost and latency
  for no accuracy gain.
- A **scanned image** requires Textract. Without it the upload fails fast with a
  message naming the setting that fixes it, rather than storing empty text and
  reporting success.
- If **OpenAI fails** (outage, quota), analysis falls back to the rule engine and
  the result is tagged with a warning. An outage degrades quality; it does not
  lose the document.

`GET /api/v1/health/providers` reports which engines are actually active and why.

### Grounding — how hallucination is limited

1. **Structured outputs.** The OpenAI request carries a JSON Schema with
   `strict: true`, so the model is constrained at decode time to our exact keys,
   types and enum members. No parsing prose out of a code fence.
2. **The schema's enums are generated from the same `StrEnum`s the API serves**,
   so the model's allowed values, the database values and the OpenAPI docs
   cannot drift apart.
3. **Prompts forbid outside knowledge** and require values to be copied verbatim.
4. **`answer_found` is an explicit field.** A model that cannot find the answer
   is given a way to say so, and `quotes` carries the verbatim passages the
   answer rests on — so a caller can verify it.
5. **Responses are repaired, not trusted.** Strict schemas constrain shape, not
   sanity: a confidence of `92` becomes `0.92`, an invented entity type becomes
   `other`, an over-long summary is clipped. One bad value degrades a field
   instead of discarding the whole document.

---

## Authentication

Two token types, both signed with the same key but **not** interchangeable — a
`typ` claim is verified on every use, so a long-lived refresh token cannot
authorise an API call.

| | Access | Refresh |
|---|---|---|
| Lifetime | 30 minutes (configurable) | 7 days |
| Stored server-side | No — stateless, no DB hit | Yes, only its `jti` |
| Purpose | Every API call | Obtaining a new pair |
| Reusable | Yes, until expiry | **No — single use** |

### Refresh rotation with reuse detection

Redeeming a refresh token revokes it and issues a new pair. If an
already-redeemed token is presented again, there are two explanations — a buggy
client or a stolen token — and we cannot tell them apart, so **every session for
that user is revoked**. This is the standard OAuth 2 recommendation for public
clients.

Crucially, the system distinguishes *why* a token was revoked. A token spent on
a **rotation** implies theft. One revoked by **logout** is a mundane stale
client, and treating it as an attack would let any user accidentally sign out all
their own devices. Only the former triggers the mass revocation.

Other properties:

- Login is **timing-equalised** — an unknown email burns one bcrypt verification
  so response latency does not reveal whether an account exists, and the error
  message is byte-identical to a wrong password.
- Deactivating a user takes effect on their **next request**, because the
  active-user check reads the database rather than trusting the token.
- Changing a password **revokes every session**, because that is the point of
  changing it.
- Tokens carry `iss`/`aud`, so one minted for this service cannot be replayed
  against a sibling service that shares a key.

---

## API reference

All paths are prefixed with `/api/v1`. Full interactive docs at `/docs`.

### Authentication

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | — | Create an account (first one becomes admin) |
| POST | `/auth/login` | — | OAuth2 password form → token pair + profile |
| POST | `/auth/refresh` | — | Rotate a refresh token |
| POST | `/auth/logout` | — | Revoke this session, or all of them |
| GET | `/auth/sessions` | ✔ | List your live sessions |

### Users

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/users/me` | ✔ | Your profile |
| PATCH | `/users/me` | ✔ | Update your profile |
| POST | `/users/me/password` | ✔ | Change password (signs out everywhere) |
| DELETE | `/users/me/sessions` | ✔ | Sign out of all devices |
| GET | `/users` | admin | List all users (paginated) |
| PATCH | `/users/{id}` | admin | Change a role or active status |
| DELETE | `/users/{id}` | admin | Delete an account |

### Documents

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/documents` | ✔ | Upload → **202** (or **200** if deduplicated) |
| GET | `/documents` | ✔ | List, filter, sort, paginate |
| GET | `/documents/stats` | ✔ | `{status: count}` for a dashboard |
| GET | `/documents/{id}` | ✔ | Detail + extraction + audit trail |
| GET | `/documents/{id}/text` | ✔ | Full extracted text |
| GET | `/documents/{id}/download` | ✔ | The original bytes |
| POST | `/documents/{id}/ask` | ✔ | Grounded natural-language Q&A |
| POST | `/documents/{id}/reprocess` | ✔ | Re-run the pipeline |
| DELETE | `/documents/{id}` | ✔ | Delete record, extraction, and blob |

### Health

| Method | Path | Description |
|---|---|---|
| GET | `/health/live` | Liveness — touches no dependency |
| GET | `/health/ready` | Readiness — checks the database, 503 when degraded |
| GET | `/health/providers` | Which AI providers are active, and why |

Liveness and readiness are separate because orchestrators use them for opposite
decisions: a failed liveness means *restart me*, so it must never fail on a
database blip; a failed readiness means *stop routing traffic here*.

### Error format

Every non-2xx response uses one envelope:

```json
{
  "error": {
    "code": "not_found",
    "message": "Document 42 was not found.",
    "request_id": "9f2c1ab34de5f678"
  }
}
```

`request_id` is also returned as the `X-Request-ID` header and appears on every
server log line for that request — including the background AI processing it
triggered.

### curl walkthrough

```bash
BASE=http://127.0.0.1:8000/api/v1

curl -X POST $BASE/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"jane@example.com","password":"Sup3rSecretPass","full_name":"Jane"}'

# Note: the field is `username`, and it holds the email (OAuth2 password flow).
TOKEN=$(curl -s -X POST $BASE/auth/login \
  -d 'username=jane@example.com&password=Sup3rSecretPass' | jq -r .access_token)

DOC=$(curl -s -X POST $BASE/documents -H "Authorization: Bearer $TOKEN" \
  -F 'file=@invoice.pdf;type=application/pdf' | jq -r .id)

# Poll until completed
curl -s $BASE/documents/$DOC -H "Authorization: Bearer $TOKEN" | jq '.status, .extraction.fields'

curl -s -X POST $BASE/documents/$DOC/ask -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is the total amount due?"}' | jq
```

---

## Configuration

Every setting lives in [`app/core/config.py`](app/core/config.py) and is
documented in [`.env.example`](.env.example). Nothing else reads `os.environ`.

Most-used settings:

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `local` | `local`/`development`/`staging`/`production`/`test` |
| `DATABASE_URL` | `sqlite:///./app.db` | Write the blocking URL; async/sync drivers are derived |
| `SECRET_KEY` | placeholder | **Required** in production — startup fails otherwise |
| `CORS_ORIGINS` | localhost:5173 | Comma-separated or JSON; `*` rejected in production |
| `STORAGE_BACKEND` | `local` | `local` or `s3` |
| `OCR_PROVIDER` | `auto` | `auto`/`local`/`textract`/`none` |
| `LLM_PROVIDER` | `auto` | `auto`/`openai`/`heuristic`/`none` |
| `OPENAI_API_KEY` | — | Set to enable OpenAI analysis |
| `TEXTRACT_ENABLED` | `false` | Set to enable OCR for scanned images |
| `MAX_UPLOAD_SIZE_MB` | `20` | Enforced on bytes received, not `Content-Length` |
| `LLM_MAX_INPUT_CHARS` | `24000` | Caps prompt size so one huge scan cannot blow the bill |
| `PROCESSING_MAX_CONCURRENCY` | `4` | Concurrent AI pipelines |
| `LOG_JSON` | `false` | `true` emits one JSON object per line |

**Production safety rails.** The settings validator refuses to start the app with
the default `SECRET_KEY`, with `DEBUG=true`, or with wildcard CORS when
`ENVIRONMENT=production`. The most damaging misconfigurations fail loudly at
boot rather than silently in production.

### Enabling the cloud AI providers

```dotenv
# Better analysis quality
OPENAI_API_KEY="sk-..."
OPENAI_MODEL="gpt-4o-mini"

# OCR for scans and photographs
TEXTRACT_ENABLED=true
AWS_REGION="us-east-1"
# Leave the keys blank to use the IAM role / default credential chain.

# Multi-page PDFs via Textract need S3 (its async API reads only from S3)
STORAGE_BACKEND="s3"
S3_BUCKET="my-documents-bucket"
```

Restart, then confirm with `GET /api/v1/health/providers`.

---

## Database & migrations

Works with **SQLite** (default, zero setup), **MySQL**, and **PostgreSQL**. Write
the familiar blocking URL in `DATABASE_URL`; the app derives an async driver for
itself and a blocking one for Alembic, so one value serves both and they cannot
drift.

```bash
alembic upgrade head        # apply migrations
alembic revision --autogenerate -m "add x"   # create one after a model change
alembic check               # fail if models and migrations have diverged
alembic downgrade -1        # roll back
```

`alembic check` in CI is what stops a model change shipping without its
migration.

### MySQL

```bash
mysql -u root -p < db/schema_mysql.sql   # optional: provision by hand
```

Then set `DATABASE_URL="mysql+pymysql://appuser:password@localhost:3306/appdb"`.
If you provision with the SQL script rather than Alembic, run `alembic stamp head`
so later migrations apply cleanly.

> **SQLite is for development.** It serialises writers, so concurrent uploads can
> hit "database is locked". Use MySQL or PostgreSQL for anything concurrent.

Outside production the app also runs `create_all` on startup for convenience.
That is disabled in production, where `create_all` is actively wrong: it cannot
express an `ALTER`, so it silently diverges from the models over time.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest                       # 262 tests
pytest --cov=app --cov-report=term-missing
pytest tests/test_documents.py -v
```

The suite runs the **real pipeline** — upload, storage, PDF parsing, analysis,
persistence — with no network, no credentials, and no mocked database. It uses a
throwaway SQLite file and a temp storage directory configured before the app is
imported, so the tests exercise the same wiring production uses.

| File | Covers |
|---|---|
| `test_auth.py` | Registration, login, rotation, reuse detection, logout, sessions |
| `test_users.py` | Profile, password change, RBAC, admin operations, pagination |
| `test_documents.py` | Full upload→extract→analyse→query flow, ownership, validation |
| `test_pipeline.py` | State machine, atomic claim, crash recovery, fallback, concurrency |
| `test_ai_heuristic.py` | Rule engine + local PDF/text extraction |
| `test_ai_openai.py` | OpenAI adapter against a stub client: schema, coercion, errors |
| `test_ai_textract.py` | Textract block parsing and AWS error mapping |
| `test_models.py` | Enum storage, UTC datetimes, lazy-load guards |
| `test_storage.py` | Round-tripping and path-traversal safety |
| `test_config_and_registry.py` | Settings parsing, safety rails, provider selection |
| `test_health.py` | Probes, error envelope, OpenAPI, request correlation |

PDFs used in tests are **generated**, not committed as binary fixtures — see
[`tests/pdf_builder.py`](tests/pdf_builder.py), which emits genuine PDFs with a
correct object table and xref offsets. The content is visible in the test that
uses it.

The OpenAI and Textract paths need live credentials, so they are covered against
stub clients rather than the real services. Everything else is covered
end-to-end.

---

## Docker

```bash
docker compose up --build     # API + MySQL
```

Then open <http://localhost:8000/docs>. The API waits for MySQL to report
healthy and runs `alembic upgrade head` before serving.

The image is a two-stage build (compilers never reach the runtime layer), runs as
a **non-root** user, and its `HEALTHCHECK` hits `/health/ready` so an unreachable
database marks the container unhealthy rather than merely "running".

To use OpenAI in compose, export the key first: `export OPENAI_API_KEY=sk-...`

---

## Production checklist

- [ ] `ENVIRONMENT=production` and a strong `SECRET_KEY`
      (`python -c "import secrets; print(secrets.token_urlsafe(48))"`)
- [ ] `CORS_ORIGINS` restricted to your real frontend domains
- [ ] MySQL or PostgreSQL, **not** SQLite
- [ ] `alembic upgrade head` in the deploy step; `AUTO_CREATE_TABLES` off
- [ ] `STORAGE_BACKEND=s3` if running more than one replica — local disk is not
      shared, so a file uploaded to one instance is invisible to a background
      task on another
- [ ] `LOG_JSON=true` and ship logs somewhere queryable by `request_id`
- [ ] TLS terminated upstream; run uvicorn with `--proxy-headers`
- [ ] One uvicorn worker per container; scale with replicas
- [ ] A real broker (Celery/RQ/SQS) if uploads outgrow one process — see below

---

## Design decisions

Short answers to "why is it built this way".

**Why async?** Every request here is I/O bound — a database round-trip, an S3
call, an OpenAI call. Async keeps thousands in flight on one worker instead of
parking a thread per request.

**Why a provider abstraction instead of calling OpenAI directly?** Three
concrete wins: the feature works with zero credentials (so it is demonstrable and
CI-testable), providers are swappable per environment, and an outage degrades
quality rather than taking the endpoint down.

**Why is the background runner in-process rather than Celery?** It needs no
broker, which is the right trade-off up to a single modest deployment. Its limits
are real and the design accounts for them rather than hiding them: work does not
survive a restart — mitigated by a startup sweep that returns abandoned
documents to a retryable state — and it does not distribute across replicas.
Because the pipeline's entry point is just `process_document(document_id)`
reading state from the database, moving to Celery, RQ, or an SQS consumer means
changing *how it is invoked*, not the pipeline itself.

**Why store enum values instead of names?** SQLAlchemy persists an enum's member
name (`"ADMIN"`) by default, which disagrees with the JSON API, the column's
`server_default`, and anything a human types in a SQL console — so a row inserted
by raw SQL reads back as `LookupError`. Storing values keeps all of them
identical.

**Why a custom `UtcDateTime` type?** SQLite and MySQL `DATETIME` do not persist a
timezone offset, so a value written as aware comes back *naive*. Comparing that
against `datetime.now(UTC)` raises `TypeError`, and arithmetic on it silently
treats a UTC instant as local time. Normalising on the way in and re-attaching
UTC on the way out means the rest of the codebase only ever sees aware UTC.

**Why `lazy="raise"` on every relationship?** It turns an accidental lazy load —
a hidden N+1 in production, or a `MissingGreenlet` in async code — into a loud
failure in the test that introduced it.

**Why is the extraction text in a separate table?** It can be megabytes. Keeping
it out of `documents` means listing 50 documents never drags 50 MB of OCR text
through the database.

**Why 404 instead of 403 for someone else's document?** A 403 confirms the id
exists. Ownership is enforced inside the query, so no endpoint can forget it.

**Why deduplicate by checksum?** Re-uploading the same invoice should not pay for
the same AI call twice. The unique constraint is `(owner_id, checksum)`, so
dedup is per user and never leaks one user's document to another.

**Why does the error envelope include a request id?** It is the bridge between
what a user can quote and what we logged — including the background AI stages,
because the id propagates into the task via a contextvar.
