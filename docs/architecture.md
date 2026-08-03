# Architecture

## The problem this shape solves

A support agent that can issue refunds has two failure modes that matter:
it refunds money it should not have, or it sends a customer a promise the
company will not honour. Both come from the same root cause — letting a language
model be the thing that decides.

The design answer here is a division of labour:

| Concern | Decided by | Why |
| --- | --- | --- |
| What kind of ticket is this? | Model | Language understanding; a wrong answer costs a re-route, not money. |
| Which policy governs it? | Vector search | Retrieval, not judgement. |
| Is a refund allowed, and for how much? | **Deterministic rules engine** | Money. Must be reproducible, testable, and impossible to argue with. |
| How do we word the reply? | Model | Language generation, bounded by the verdict above. |
| Does a human need to see this? | **Deterministic gate** | Safety. Must not depend on the model's self-assessment. |
| Does anything actually happen? | **Human approver** | Nothing reaches a customer or a payment system without a person. |

The model writes prose around decisions it did not make.

## Request flow

```mermaid
flowchart TB
    subgraph client["Client"]
        UI["React review console"]
        API["POST /api/tickets"]
    end

    subgraph graph["LangGraph agent"]
        direction TB
        IG["input_guardrails<br/><i>injection, legal, fraud, safety</i>"]
        CL["classify<br/><i>category, sentiment, priority, confidence</i>"]
        TG{{"triage_gate"}}
        RP["retrieve_policy<br/><i>pgvector top-k</i>"]
        FO["fetch_order<br/><i>HTTP to order API</i>"]
        CE["check_eligibility<br/><b>deterministic rules</b>"]
        RG{{"review_gate"}}
        DR["draft_response<br/><i>model + proposal tools</i>"]
        OG["output_guardrails<br/><i>overpromise, PII, amount</i>"]
        PG{{"post_draft_gate"}}
        AA(["await_approval<br/><b>INTERRUPT</b>"])
        EX["execute_actions<br/><i>refund / replacement</i>"]
        SR["send_reply"]
        ESC["escalated"]
        REJ["rejected"]
    end

    subgraph stores["State"]
        PG_DB[("Postgres<br/>tickets · runs · traces<br/>policy vectors · orders")]
        CP[("LangGraph<br/>checkpoints")]
    end

    API --> IG --> CL --> TG
    TG -->|"blocked or unclear"| ESC
    TG --> RP --> FO --> CE --> RG
    RG -->|"outside policy, above cap,<br/>weak retrieval"| ESC
    RG --> DR --> OG --> PG
    PG -->|"wording failure<br/>(once)"| DR
    PG -->|"overstep or<br/>repeat failure"| ESC
    PG --> AA
    AA -->|approve| EX --> SR
    AA -->|reject| REJ

    UI <--> API
    AA -.->|"suspend / resume"| CP
    graph -.->|"every node"| PG_DB
```

## Why the gate appears three times

`escalation_gate` runs at three points, deliberately:

- **triage_gate**, before retrieval. Catches what must never reach a model-written
  draft: legal threats, fraud reports, safety incidents, prompt injection. Also
  stops spending tokens on a ticket a human will handle anyway.
- **review_gate**, after eligibility. Catches what only becomes knowable once the
  order and policy are in hand: outside the refund window, above the approval
  cap, no policy actually matches.
- **post_draft_gate**, after the draft is checked. Splits *who* fixes the
  problem — a wording failure the model can retry once, versus an overstep that
  a human must see.

## The approval interrupt

`await_approval` calls LangGraph's `interrupt()`. The graph state is checkpointed
to Postgres and the call stack unwinds; the run is suspended, not blocked. A
`POST /api/tickets/{id}/approve` resumes that thread with the human's decision,
and the graph continues into `execute_actions` — the only place in the codebase
that issues a refund.

This means the approval survives a process restart, and the resume can happen in
a different worker than the one that produced the draft.

## Why the model cannot over-refund

Four independent barriers, any one of which is sufficient:

1. **The eligibility engine** computes the maximum before the model is called,
   from the order record and the policy's own frontmatter rules.
2. **The tools only propose.** `ProposeRefund` writes to state. No tool touches
   the order API.
3. **Clamping** drops or caps any proposal that exceeds the verdict, and records
   the attempt as a blocking guardrail flag — so an overstep escalates rather
   than being quietly sanitised.
4. **The order API** rejects a refund above the remaining balance regardless of
   what asked for it.

## Data model

| Table | Holds |
| --- | --- |
| `tickets` | The customer message plus flattened classification, so the queue sorts and filters without unpacking JSON. |
| `agent_runs` | One row per graph invocation: draft, proposals, citations, flags, prompt versions, cost. |
| `agent_traces` | One row per node execution: tokens, cost, latency, summaries. Written even on failure. |
| `policy_documents` / `policy_chunks` | The corpus and its 384-dim embeddings, with an HNSW cosine index. |
| `orders` / `order_actions` | Mock commerce state and the audit trail of executed refunds. |
| `checkpoints*` | LangGraph's own tables, created by the checkpointer. |

## Deliberate limits

- The mock order API enforces financial invariants only — never company policy.
  Policy lives in one place so a bug there cannot be masked by the API happening
  to do the right thing.
- Policy prose and machine-readable rules live in the same file, so a change to
  the refund window updates the text the model reads and the number the engine
  enforces together.
- Traces are written to Postgres unconditionally. LangSmith, if configured, is
  additive — the project is fully observable without a third-party account.
