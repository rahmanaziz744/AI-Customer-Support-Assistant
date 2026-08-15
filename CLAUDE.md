# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (local dev)
docker compose up -d db                       # Postgres 16 + pgvector, host port 5433
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
alembic upgrade head
python scripts/seed_db.py                     # policy corpus + mock orders
python -m app                                 # API on :8000

cd ui && npm install && npm run dev           # console on :5173

# Tests
pytest                                        # full suite, needs Postgres, no API key
pytest -m "not db"                            # unit only, nothing running
pytest tests/test_eligibility.py::test_name   # single test
pytest -m "not live"                          # skip the tests that call the real API

# Lint / typecheck
ruff check app/ tests/ evals/ scripts/        # line-length 100, py312
cd ui && npm run typecheck && npm run build

# Evals (CI gates on these)
python scripts/seed_db.py --reset             # cases reference seed orders by ref
python -m evals.runner --mode retrieval,decisions   # no API key needed
python -m evals.runner --mode all                   # adds classifier + LLM judge
# --limit N, --no-write also available. The decisions suite needs the API running
# (the mock order endpoints live in it).

# Demo queue without an API key
python scripts/run_demo.py --offline

# Migrations
alembic revision --autogenerate -m "message"
alembic upgrade head
```

## Run it with `python -m app`, not `uvicorn app.main:app`

`app/core/runtime.py` sets the event loop policy and **must be imported before any
loop exists** — psycopg's async driver cannot run on Windows' ProactorEventLoop.
`tests/conftest.py` imports it before anything that touches the DB, for the same
reason. On Linux the two entrypoints are equivalent, but keep the invariant.

## The core invariant

**There is no code path from a model tool call to a refund.** Preserve this when
changing anything under `app/agents/`:

- `app/agents/eligibility.py` is a pure rules engine. It computes the maximum
  refund/replacement *before* the model is called. No I/O, no model, exhaustively
  tested — it is the component that decides whether money moves.
- `app/agents/tools.py` — `ProposeRefund` / `ProposeReplacement` / `ProposeNoAction`
  only write to graph state. They never call the order API.
- Proposals above the verdict are clamped *and* flagged `block`, so an overstep
  escalates rather than being quietly sanitised.
- `execute_actions_node` in `app/agents/nodes.py` is the only place that issues a
  refund, and it is reachable only by resuming the approval interrupt.

## Architecture

Read `docs/architecture.md` for the full reasoning; the shape that matters:

**`app/agents/graph.py`** defines the topology. `nodes.escalation_gate_node` is
registered three times — `triage_gate` (before any model draft), `review_gate`
(after eligibility), `post_draft_gate` (after output checks). Adding a node
usually means touching the topology here, the node in `nodes.py`, and the
`AgentState` TypedDict in `state.py`.

**`app/agents/runner.py`** is the boundary between graph state and the database.
The graph never writes ticket/run/trace rows — this module translates a graph
result into them. If a field should be visible in the API or UI, it needs a line
in `_sync_run_from_state` / `_sync_ticket_from_state`.

**The approval interrupt.** `await_approval_node` calls LangGraph's `interrupt()`;
state is checkpointed to Postgres, so a run can be suspended, the process
restarted, and resumed by a different worker via `POST /api/tickets/{id}/approve`.

**Graph compilation is cached per event loop** (`get_compiled_graph`). psycopg's
async connection is bound to the loop that opened it, so a cached graph reused on
a new loop dies with "the connection is closed" — which is what happens to a test
suite with a loop per test, or any script calling `asyncio.run` twice. Checkpointer
DDL runs in `ensure_checkpointer_schema()` at startup, never lazily inside a
request (`CREATE INDEX CONCURRENTLY` would deadlock against the request's own
transaction).

**Policy documents are the source of truth for rules.** `data/policies/*.md` carry
machine-readable `rules:` frontmatter alongside the prose the model reads, so the
refund window the model sees and the number the engine enforces cannot drift. The
constants in `eligibility.py` are fallbacks only. Changing a policy number means
editing the frontmatter *and* the prose, then re-running `scripts/ingest_policies.py`
(or `seed_db.py`).

**Prompts are versioned files** in `app/agents/prompts/*.md`, pinned per node and
recorded on every run in `agent_runs.prompt_versions`. Change behaviour by adding
`draft_v3.md` and repointing the node, not by editing a version already recorded
in past runs.

## Testing

`app/agents/llm.py::get_chat_model` is the single seam every node goes through;
tests install a scripted fake via `set_model_factory` (`tests/fakes.py`), so the
whole graph — routing, guardrails, tool handling, the interrupt — runs
deterministically with no API key. The autouse `_restore_models` fixture undoes it.

`app/services/order_client.py::set_default_transport` is the matching seam for
the order API. Nodes construct `OrderClient()` with no arguments, so the `client`
fixture points the default transport at the ASGI app; without it the graph would
need a real socket on `ORDER_API_BASE_URL`, and the API tests fail against a
stopped server. The fixture restores it to `None` on teardown.

DB-backed tests skip cleanly when Postgres is down (`db_available` probes with a
2s timeout), which keeps plain `pytest` fast on a laptop with nothing started.

`conftest.py` forces `EMBEDDING_PROVIDER=hash` (deterministic offline embedder, no
ONNX download) and `ESCALATION_RETRIEVAL_THRESHOLD=0.0` — the hash embedder's
similarity scores are lexical and would trip the retrieval-quality gate on every
ticket. That gate is tested directly in `test_guardrails.py`.

`asyncio_mode = "auto"`, so async tests need no decorator.

## Conventions

- Money is `Decimal` in the DB and in the engine, but **serialised as a string** in
  graph state — state is JSON-checkpointed and `Decimal` is not JSON-native.
- `Settings` (`app/core/config.py`) is `lru_cache`d; new config goes there with a
  default that keeps local dev working, plus a documented entry in `.env.example`.
- The Sonnet 5 / Opus 5 generation rejects `temperature`/`top_p`. Reasoning
  depth is set via `output_config.effort` instead — see `_default_factory` in
  `llm.py`. `effort` is itself rejected by cheaper models (Haiku 4.5), so
  `supports_effort` allowlists the families that accept it and omits the
  parameter otherwise. Adding a new model family means adding it there.
- Cost tracking prices from a table in `app/core/pricing.py`; unknown models record
  `null`, not `$0`. Add new model IDs there.
