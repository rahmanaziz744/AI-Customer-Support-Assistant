# AI Customer Support Agent

A support agent that triages tickets against company policy: it classifies the
ticket, retrieves the governing policy by vector search, checks refund and
replacement eligibility against an order API, drafts a reply, and routes
everything to a human before anything reaches a customer.

Built with FastAPI, LangGraph, Postgres + pgvector, and Claude.

![Review console](docs/screenshots/02b-approve-draft.png)

---

## The problem

Support automation fails in two expensive ways: it refunds money it should not
have, or it promises a customer something the company will not honour. Both come
from the same mistake — letting the language model be the thing that *decides*.

This project separates the two jobs:

| Concern | Decided by | Why |
| --- | --- | --- |
| What kind of ticket is this? | Model | Language understanding. A wrong answer costs a re-route, not money. |
| Which policy governs it? | Vector search | Retrieval, not judgement. |
| Is a refund allowed, and for how much? | **Rules engine** | Money. Must be reproducible and testable. |
| How should the reply be worded? | Model | Language generation — bounded by the verdict above. |
| Does a human need to see this? | **Deterministic gate** | Safety must not depend on the model grading its own work. |
| Does anything actually happen? | **Human approver** | Nothing is sent or refunded without a person. |

The model writes prose around decisions it did not make. There is no code path
from a model tool call to a refund.

---

## Architecture

```mermaid
flowchart TB
    API["POST /api/tickets"] --> IG["input_guardrails<br/><i>injection · legal · fraud · safety</i>"]
    IG --> CL["classify<br/><i>category · sentiment · priority · confidence</i>"]
    CL --> TG{{triage_gate}}
    TG -->|blocked or unclear| ESC["escalated"]
    TG --> RP["retrieve_policy<br/><i>pgvector top-k</i>"]
    RP --> FO["fetch_order"]
    FO --> CE["check_eligibility<br/><b>deterministic rules</b>"]
    CE --> RG{{review_gate}}
    RG -->|outside policy · above cap| ESC
    RG --> DR["draft_response<br/><i>model + proposal tools</i>"]
    DR --> OG["output_guardrails<br/><i>overpromise · PII · amount</i>"]
    OG --> PG{{post_draft_gate}}
    PG -->|wording failure, once| DR
    PG -->|overstep| ESC
    PG --> AA(["await_approval<br/><b>INTERRUPT</b>"])
    AA -->|approve| EX["execute_actions"] --> SR["send_reply"]
    AA -->|reject| REJ["rejected"]
```

The escalation gate runs at three points on purpose — before any model draft,
after eligibility is known, and after the draft is checked. Full reasoning,
including why the model cannot over-refund, is in
[docs/architecture.md](docs/architecture.md).

---

## Features

**Ticket classification** — category, sentiment, SLA priority 1–5, and a
confidence score via structured output. Deterministic SLA rules can raise
priority but never lower it.

**RAG over policy documents** — eight policy documents, chunked on section
boundaries and embedded locally with `bge-small-en-v1.5` into pgvector. Each
document carries machine-readable rules in its frontmatter alongside the prose,
so the refund window the model reads and the number the engine enforces cannot
drift apart. The always-applicable escalation policy is guaranteed to stay in
the retrieved context regardless of category.

**Deterministic eligibility** — a pure rules engine decides refunds and
replacements: delivery windows, final-sale exclusions, partial refunds, the
remaining balance, the auto-approval cap, and the one-replacement-per-order
limit. Every check is recorded and shown to the reviewer.

**Tool calling** — the drafting model gets `ProposeRefund`, `ProposeReplacement`
and `ProposeNoAction`. They only record intent; nothing calls the order API.

**Human approval** — the graph suspends on LangGraph's `interrupt()` with state
checkpointed to Postgres. Approving resumes the run and is the only path that
issues a refund. The reviewer can edit the draft first; the model's original is
preserved for comparison.

**Escalation logic** — a deterministic gate over guardrail flags, the eligibility
verdict, classifier confidence, retrieval quality, priority/sentiment, order
lookup failures, and repeated draft failures.

**Safety guardrails** — three layers: input screening (prompt injection, legal
threats, fraud, safety, crisis language), tool-layer clamping, and output
validation (overpromises, leaked PII, amounts above the approved ceiling,
drafts that contradict the eligibility verdict).

**Cost tracking** — every node's tokens, cost and latency are written to
`agent_traces` and surfaced per ticket and in aggregate.

**Prompt versioning** — prompts are files, pinned per node, recorded on every run.

**Eval suite** — 40 labelled tickets across four suites; the two that need no API
key are the CI default.

---

## Stack

| | |
| --- | --- |
| API | FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic |
| Agent | LangGraph with a Postgres checkpointer, `langchain-anthropic` |
| Model | `claude-opus-5` (per-node overrides supported) |
| Storage | Postgres 16 + pgvector, HNSW cosine index |
| Embeddings | `fastembed` / `BAAI/bge-small-en-v1.5` — local, no second API key |
| UI | React 18 + TypeScript + Vite, no component framework |
| Ops | Docker Compose, structlog, slowapi, pytest |

---

## Setup

### Docker (everything, one command)

```bash
cp .env.example .env   # then add your ANTHROPIC_API_KEY
docker compose up --build
```

Migrations and seeding run automatically. The console is at
<http://localhost:8080>, the API at <http://localhost:8000/docs>.

### Local development

```bash
docker compose up -d db
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
alembic upgrade head
python scripts/seed_db.py
python -m app
```

```bash
cd ui && npm install && npm run dev
```

> Use `python -m app`, not `uvicorn app.main:app`. The entrypoint selects the
> event loop before uvicorn creates one — psycopg's async driver cannot run on
> Windows' default ProactorEventLoop. On Linux the two are equivalent.

### See it without an API key

```bash
python scripts/run_demo.py --offline
```

Populates a realistic queue using canned model replies. Retrieval, the rules
engine, guardrails and the approval interrupt all run for real; only the two
model calls are stubbed.

---

## API examples

Submit a ticket (the agent runs in the background):

```bash
curl -X POST http://localhost:8000/api/tickets \
  -H 'Content-Type: application/json' \
  -d '{"customer_email":"maria.lopez@example.com","subject":"Kettle leaks","body":"The kettle leaks from the seam. I would like a refund.","order_ref":"ORD-1001"}'
```

Read it back once the agent has finished:

```bash
curl http://localhost:8000/api/tickets/$TICKET_ID
```

```json
{
  "status": "AWAITING_APPROVAL",
  "category": "REFUND_REQUEST",
  "priority": 3,
  "confidence": 0.94,
  "latest_run": {
    "draft_response": "Hi Maria,\n\nI've arranged a refund of $89.99 …",
    "proposed_actions": [{ "type": "refund", "amount": "89.99" }],
    "eligibility": {
      "eligible": true,
      "approved_amount": "89.99",
      "reason": "Delivered 5 days ago, inside the 30-day window, with 89.99 refundable."
    }
  }
}
```

Approve, optionally rewriting the reply. **This is the call that issues the refund:**

```bash
curl -X POST http://localhost:8000/api/tickets/$TICKET_ID/approve \
  -H 'Content-Type: application/json' \
  -d '{"approver":"reviewer@northwind.test","edited_draft":"Hi Maria, your refund is on its way."}'
```

Inspect the run, step by step:

```bash
curl http://localhost:8000/api/tickets/$TICKET_ID/trace
```

| Endpoint | Purpose |
| --- | --- |
| `POST /api/tickets` | Submit a ticket (rate limited) |
| `GET /api/tickets` | Queue, filterable by status/category/search |
| `GET /api/tickets/{id}` | Ticket with its latest run |
| `POST /api/tickets/{id}/process` | Run the agent synchronously |
| `POST /api/tickets/{id}/approve` | Approve — sends the reply, executes actions |
| `POST /api/tickets/{id}/reject` | Reject — nothing is sent |
| `GET /api/tickets/{id}/trace` | Per-node tokens, cost, latency |
| `GET /api/stats` | Queue counts, escalation rate, spend |
| `/mock-api/orders/*` | Stand-in commerce backend |

---

## Screenshots

**Review queue** — awaiting-approval first, then by SLA priority.

![Queue](docs/screenshots/01-queue.png)

**Eligibility, shown as evidence.** Every rule the engine evaluated, with the
policy chunks that grounded the draft.

![Eligibility](docs/screenshots/02-review-draft.png)

**A prompt-injection attempt.** Flagged at input, escalated before any model
draft, and the trace shows it never reached the drafting node.

![Injection blocked](docs/screenshots/05-injection-blocked.png)

**The agent trace** — every node with tokens, cost, latency and prompt versions.

![Trace](docs/screenshots/03-trace.png)

---

## Evaluation results

```bash
python scripts/seed_db.py --reset                   # eval cases assume seed order state
python -m evals.runner --mode retrieval,decisions   # no API key
python -m evals.runner --mode all                   # adds classifier + LLM judge
```

40 labelled tickets. Cases reference specific seed orders by reference, so reset
the seed data first — an order left partly refunded by a demo run will change
the eligibility verdict, correctly, and show up as a failure.

The runner exits non-zero if any eligibility or escalation decision is wrong, so
it can gate CI. Latest run ([full report](evals/results/latest.md)):

| Suite | Metric | Result |
| --- | --- | --- |
| Retrieval | hit rate @4 | **1.00** |
| Retrieval | hit rate @1 | 0.85 |
| Retrieval | mean top similarity | 0.66 |
| Decisions | eligibility accuracy | **1.00** |
| Decisions | escalation F1 | **1.00** |
| Decisions | missed escalations | **0** |
| Decisions | over-escalations | 0 |

### The evals earned their keep

The first run missed 5 of 15 escalations. Triage found four real defects, not
bad labels:

- The fraud pattern missed *"used my card without my authorisation"* — the most
  natural way to report card fraud — so a fraud ticket was handled automatically.
- Prior replacements were never counted, so a second replacement on the same
  order was auto-approved despite the one-per-order cap.
- The category filter hid the shipping policy from a shipping question
  classified as a general inquiry, leaving the ticket ungrounded.
- Prompt injection only warned. It now escalates before any model call.

After the fixes: escalation F1 0.80 → 1.00, missed escalations 5 → 0.

`hit_rate@1` stays at 0.85 by design: the always-applicable escalation policy is
forced into the retrieved set, and it sometimes outranks the category-specific
document. `@4` is the metric that matters, since all four chunks reach the model.

---

## Tests

```bash
pytest                    # 125 tests
pytest -m "not db"        # unit only, no database
```

125 tests, none requiring an API key: 16s against Postgres, or 96 unit tests in
4s with nothing running. The eligibility engine is covered exhaustively because
it is the component that decides whether money moves.

Two bugs the tests caught:

- `send_reply` overwrote the state's draft with the human's edit, destroying the
  record of what the model originally wrote.
- The compiled graph cached a psycopg connection bound to one event loop, so a
  second `asyncio.run` in the same process failed with *"connection is closed"*.

---

## Known limitations

- **The order system is a mock.** It enforces financial invariants — the order
  exists, the money is available, an idempotency key is not replayed — but no
  company policy. That separation is deliberate, so a bug in the rules engine
  cannot be hidden by the API happening to do the right thing.
- **No authentication.** The approver identity is a string in the request body.
  Any real deployment needs real auth before this is exposed.
- **Escalation is a status, not a queue.** Escalated tickets are marked and
  surfaced but not routed, assigned, or SLA-tracked.
- **`send_reply` logs instead of sending.** No email or helpdesk integration.
- **Retrieval is single-vector.** No reranking or hybrid search; with a corpus
  this small it is not the bottleneck, but it would be at scale.
- **The eval set is small and self-authored.** 40 cases written alongside the
  policies, so it measures internal consistency, not real-world traffic.
- **Rate limiting is in-process.** slowapi's in-memory store does not survive a
  restart or coordinate across replicas; production needs Redis.
- **Cost tracking prices from a table in code.** Correct at the pinned commit,
  and it records `null` rather than `$0` for an unrecognised model.
- **Windows needs a specific entrypoint.** See the note under Setup.

---

## Future work

- Replace the mock order API with a real commerce backend — the HTTP seam
  already exists, so it is a configuration change.
- Auth and per-approver audit, with approval limits by role.
- A/B prompt comparison: prompt versions are already recorded per run, so the
  eval harness could compare two versions over the same dataset.
- Hybrid retrieval with a reranker, measured against the existing `hit_rate@1`.
- Route escalations to a real queue with ownership and SLA timers.
- Expand the eval set from production traffic and track drift per prompt version.
- Streamed drafting, so reviewers see the reply as it is written.

---

## Repository layout

```
app/
  agents/      graph, nodes, eligibility, guardrails, tools, prompts/
  api/         ticket, approval, trace, health, mock order routers
  core/        config, db, logging, pricing, errors, rate limiting
  rag/         loader, chunker, embeddings, retriever, ingest
  models/      SQLAlchemy models
  services/    ticket service, order client
data/policies/ eight policy documents with machine-readable rules
evals/         dataset.jsonl, runner, metrics, results/
scripts/       seed_db, ingest_policies, run_demo
tests/         125 tests, scripted model fakes
ui/            React review console
docs/          architecture.md, screenshots/
```
