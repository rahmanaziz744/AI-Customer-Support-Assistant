"""Evaluation harness.

    python -m evals.runner --mode retrieval,decisions     # no API key needed
    python -m evals.runner --mode all                     # adds classification + judge

Four modes, deliberately separable by cost:

- **retrieval** — does the right policy document come back for each ticket?
  Pure vector search, no model calls, free.
- **decisions** — does the deterministic eligibility engine reach the right
  verdict, and does the escalation gate fire when it should? Also free, and
  the most important suite: these are the calls that move money.
- **classification** — one model call per ticket.
- **draft** — full agent run per ticket plus an LLM-as-judge score. The
  expensive one, off unless asked for.

The two free suites are the default so the whole thing runs in CI.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.eligibility import evaluate_refund, evaluate_replacement  # noqa: E402
from app.agents.escalation import evaluate_escalation  # noqa: E402
from app.agents.guardrails import screen_input  # noqa: E402
from app.agents.nodes import Classification  # noqa: E402
from app.agents.prompts import load_prompt  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import session_scope  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.rag.retriever import retrieve_policy  # noqa: E402
from app.services.order_client import OrderAPIError, OrderClient, OrderNotFound  # noqa: E402
from evals.metrics import (  # noqa: E402
    binary_report,
    classification_report,
    format_markdown_table,
    hit_rate_at_k,
)

logger = get_logger("evals")

DATASET = Path(__file__).parent / "dataset.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"

ALL_MODES = ("retrieval", "decisions", "classification", "draft")
FREE_MODES = ("retrieval", "decisions")

JUDGE_RUBRIC = """You are grading a customer support reply drafted by an AI agent.

Score each criterion 1-5, where 5 is excellent and 1 is unacceptable:

- policy_grounded: every claim about windows, amounts, and timeframes matches
  the policy excerpts. Inventing a number scores 1.
- no_overpromise: makes no commitment the policy does not support — no
  guaranteed dates, no promised exceptions. A single overpromise scores 1.
- tone: warm, direct, appropriately brief; no corporate filler, no repeated
  apologising, no blame.
- actionable: the customer knows what happens next and by when.

Return your scores through the structured output, plus one sentence of
justification naming the weakest criterion."""


@dataclass
class EvalCase:
    id: str
    subject: str
    body: str
    customer_email: str
    order_ref: str | None
    expected_category: str
    expected_action: str
    expected_eligible: bool | None
    should_escalate: bool
    relevant_policy: str
    notes: str = ""
    # Which rule should fire. "confidence" cases depend on the classifier and
    # cannot be judged by the free decisions suite, which does not call a model.
    escalation_trigger: str | None = None

    @property
    def query(self) -> str:
        return f"{self.subject}\n{self.body}"


@dataclass
class SuiteResult:
    name: str
    summary: dict[str, Any]
    failures: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0


def load_cases() -> list[EvalCase]:
    cases = []
    for line in DATASET.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(EvalCase(**json.loads(line)))
    return cases


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
async def run_retrieval(cases: list[EvalCase], top_k: int = 4) -> SuiteResult:
    started = time.perf_counter()
    hits_at_1: list[bool] = []
    hits_at_k: list[bool] = []
    scores: list[float] = []
    failures: list[dict[str, Any]] = []

    async with session_scope() as db:
        for case in cases:
            chunks = await retrieve_policy(
                db, case.query, category=case.expected_category, top_k=top_k
            )
            slugs = [c.document_slug for c in chunks]
            at_1 = bool(slugs) and slugs[0] == case.relevant_policy
            at_k = case.relevant_policy in slugs

            hits_at_1.append(at_1)
            hits_at_k.append(at_k)
            if chunks:
                scores.append(chunks[0].score)

            if not at_k:
                failures.append(
                    {
                        "id": case.id,
                        "expected": case.relevant_policy,
                        "retrieved": ", ".join(dict.fromkeys(slugs)) or "(nothing)",
                        "subject": case.subject[:50],
                    }
                )

    return SuiteResult(
        name="retrieval",
        summary={
            "cases": len(cases),
            "hit_rate@1": hit_rate_at_k(hits_at_1),
            f"hit_rate@{top_k}": hit_rate_at_k(hits_at_k),
            "mean_top_score": round(statistics.mean(scores), 4) if scores else 0.0,
            "min_top_score": round(min(scores), 4) if scores else 0.0,
        },
        failures=failures,
        duration_s=round(time.perf_counter() - started, 2),
    )


# ---------------------------------------------------------------------------
# Decisions: eligibility engine + escalation gate
# ---------------------------------------------------------------------------
async def _order_for(client: OrderClient, order_ref: str | None) -> tuple[dict | None, str | None]:
    if not order_ref:
        return None, None
    try:
        return await client.get_order(order_ref), None
    except OrderNotFound:
        return None, f"Order {order_ref} does not exist"
    except OrderAPIError as exc:
        return None, str(exc)


async def run_decisions(cases: list[EvalCase]) -> SuiteResult:
    started = time.perf_counter()
    client = OrderClient()
    eligibility_pairs: list[tuple[str, str, str]] = []
    escalation_pairs: list[tuple[str, bool, bool]] = []
    skipped_escalation: list[str] = []
    failures: list[dict[str, Any]] = []

    async with session_scope() as db:
        for case in cases:
            order, order_error = await _order_for(client, case.order_ref)
            chunks = await retrieve_policy(db, case.query, category=case.expected_category)
            rules: dict[str, Any] = {}
            for chunk in chunks:
                if chunk.document_slug in ("refund-policy", "replacement-policy"):
                    rules = chunk.rules
                    break

            decision = None
            if order and case.expected_category in (
                "REFUND_REQUEST",
                "REPLACEMENT_REQUEST",
                "SHIPPING_ISSUE",
            ):
                if case.expected_category == "REPLACEMENT_REQUEST":
                    text = case.query.lower()
                    damaged = any(
                        w in text
                        for w in ("damaged", "broken", "cracked", "shattered", "defective")
                    )
                    # Same prior-replacement count the graph node uses, so the
                    # suite exercises the real decision rather than a variant.
                    prior = sum(
                        1
                        for a in await client.list_actions(case.order_ref)
                        if a.get("action_type") == "REPLACEMENT"
                    )
                    decision = evaluate_replacement(
                        order, rules, damaged_on_arrival=damaged, prior_replacements=prior
                    )
                else:
                    decision = evaluate_refund(order, rules)

            # Eligibility, scored as a three-way label so "no decision applies"
            # is distinguishable from "decided not eligible".
            if case.expected_eligible is None:
                expected_label = "n/a"
            else:
                expected_label = "eligible" if case.expected_eligible else "not_eligible"
            if decision is None:
                actual_label = "n/a"
            else:
                actual_label = "eligible" if decision.eligible else "not_eligible"
            eligibility_pairs.append((case.id, expected_label, actual_label))

            # Escalation, evaluated over the state the gate would actually see.
            flags = screen_input(case.subject, case.body)
            state = {
                "guardrail_flags": [f.to_dict() for f in flags],
                "eligibility": decision.to_dict() if decision else None,
                # Confidence comes from the classifier, which this free suite
                # does not run; assume a confident classification so the gate is
                # judged on its policy rules, not on a stand-in number.
                "confidence": 0.9,
                "retrieval_top_score": chunks[0].score if chunks else 0.0,
                "order_error": order_error,
                "revision_count": 0,
            }
            verdict = evaluate_escalation(state)

            # Skip cases whose only expected trigger is classifier confidence:
            # this suite runs no model, so scoring them would measure the
            # harness rather than the gate.
            scores_escalation = case.escalation_trigger != "confidence"
            if scores_escalation:
                escalation_pairs.append((case.id, case.should_escalate, verdict.escalate))
            else:
                skipped_escalation.append(case.id)

            escalation_wrong = scores_escalation and case.should_escalate != verdict.escalate
            if expected_label != actual_label or escalation_wrong:
                failures.append(
                    {
                        "id": case.id,
                        "expected_eligible": expected_label,
                        "got_eligible": actual_label,
                        "expected_escalate": case.should_escalate,
                        "got_escalate": verdict.escalate,
                        "rule": verdict.rule or "-",
                    }
                )

    eligibility = classification_report(eligibility_pairs)
    escalation = binary_report(escalation_pairs)

    return SuiteResult(
        name="decisions",
        summary={
            "cases": len(cases),
            "eligibility_accuracy": eligibility.accuracy,
            "eligibility_macro_f1": eligibility.macro_f1,
            "escalation_cases_scored": len(escalation_pairs),
            "escalation_skipped_needs_classifier": len(skipped_escalation),
            "escalation_accuracy": escalation["accuracy"],
            "escalation_precision": escalation["precision"],
            "escalation_recall": escalation["recall"],
            "escalation_f1": escalation["f1"],
            "missed_escalations": escalation["false_negatives"],
            "over_escalations": escalation["false_positives"],
        },
        failures=failures,
        duration_s=round(time.perf_counter() - started, 2),
    )


# ---------------------------------------------------------------------------
# Classification (one model call per case)
# ---------------------------------------------------------------------------
async def run_classification(cases: list[EvalCase]) -> SuiteResult:
    from app.agents.llm import get_chat_model

    started = time.perf_counter()
    model = get_chat_model("classify", max_tokens=1024).with_structured_output(
        Classification, include_raw=True
    )
    system = load_prompt("classify")

    pairs: list[tuple[str, str, str]] = []
    confidences: list[float] = []
    failures: list[dict[str, Any]] = []

    for case in cases:
        result = await model.ainvoke(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": (
                        f"<customer_ticket>\nSubject: {case.subject}\n"
                        f"From: {case.customer_email}\n\n{case.body}\n</customer_ticket>"
                    ),
                },
            ]
        )
        parsed = result.get("parsed") if isinstance(result, dict) else result
        predicted = (parsed.category if parsed else "GENERAL_INQUIRY").upper()
        confidence = float(parsed.confidence) if parsed else 0.0

        pairs.append((case.id, case.expected_category, predicted))
        confidences.append(confidence)

        if predicted != case.expected_category:
            failures.append(
                {
                    "id": case.id,
                    "expected": case.expected_category,
                    "predicted": predicted,
                    "confidence": round(confidence, 2),
                    "subject": case.subject[:45],
                }
            )

    report = classification_report(pairs)
    return SuiteResult(
        name="classification",
        summary={
            "cases": len(cases),
            "accuracy": report.accuracy,
            "macro_f1": report.macro_f1,
            "mean_confidence": round(statistics.mean(confidences), 4) if confidences else 0.0,
            "per_class": report.per_class,
        },
        failures=failures,
        duration_s=round(time.perf_counter() - started, 2),
    )


# ---------------------------------------------------------------------------
# Draft quality (full agent run + LLM judge)
# ---------------------------------------------------------------------------
class JudgeScores(__import__("pydantic").BaseModel):  # noqa: N801
    policy_grounded: int
    no_overpromise: int
    tone: int
    actionable: int
    justification: str


async def run_draft_quality(cases: list[EvalCase], limit: int | None = None) -> SuiteResult:
    """Run the agent for real, then have a model grade each draft.

    Only cases that should produce a customer-facing draft are graded;
    correctly escalated tickets have no draft, and scoring their absence as a
    failure would punish the right behaviour.
    """
    import uuid as uuid_mod

    from app.agents.llm import get_chat_model
    from app.agents.runner import start_run
    from app.models.enums import Channel
    from app.models.ticket import Ticket

    started = time.perf_counter()
    gradable = [c for c in cases if not c.should_escalate][: limit or None]
    judge = get_chat_model("judge", max_tokens=1024).with_structured_output(JudgeScores)

    scores: dict[str, list[int]] = {
        "policy_grounded": [], "no_overpromise": [], "tone": [], "actionable": []
    }
    failures: list[dict[str, Any]] = []
    drafted = 0

    for case in gradable:
        async with session_scope() as db:
            ticket = Ticket(
                channel=Channel.EMAIL,
                customer_email=case.customer_email,
                subject=case.subject,
                body=case.body,
                order_ref=case.order_ref,
            )
            db.add(ticket)
            await db.flush()
            ticket_id = ticket.id

        async with session_scope() as db:
            run = await start_run(db, ticket_id)
            draft = run.draft_response
            citations = run.policy_citations
            eligibility = run.eligibility

        if not draft:
            failures.append(
                {"id": case.id, "issue": "no draft produced", "status": run.status.value}
            )
            continue

        drafted += 1
        verdict = await judge.ainvoke(
            [
                {"role": "system", "content": JUDGE_RUBRIC},
                {
                    "role": "user",
                    "content": (
                        f"<ticket>{case.subject}\n{case.body}</ticket>\n\n"
                        f"<policy_excerpts>{json.dumps(citations)}</policy_excerpts>\n\n"
                        f"<eligibility>{json.dumps(eligibility)}</eligibility>\n\n"
                        f"<draft_reply>{draft}</draft_reply>"
                    ),
                },
            ]
        )
        for key in scores:
            scores[key].append(getattr(verdict, key))

        weakest = min(scores, key=lambda k: scores[k][-1])
        if min(getattr(verdict, k) for k in scores) <= 3:
            failures.append(
                {
                    "id": case.id,
                    "issue": f"low score on {weakest}",
                    "justification": verdict.justification[:120],
                }
            )

        _ = uuid_mod  # keep the import meaningful for readers of the ids above

    return SuiteResult(
        name="draft",
        summary={
            "cases_graded": drafted,
            "cases_skipped_escalated": len(cases) - len(gradable),
            **{
                f"mean_{key}": round(statistics.mean(vals), 3) if vals else 0.0
                for key, vals in scores.items()
            },
        },
        failures=failures,
        duration_s=round(time.perf_counter() - started, 2),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def render_report(results: list[SuiteResult], settings_summary: dict[str, Any]) -> str:
    lines = [
        "# Evaluation Results",
        "",
        f"Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "| setting | value |",
        "| --- | --- |",
        *[f"| {k} | {v} |" for k, v in settings_summary.items()],
        "",
    ]

    for result in results:
        lines += [f"## {result.name}", "", f"_ran in {result.duration_s}s_", ""]
        flat = {k: v for k, v in result.summary.items() if not isinstance(v, dict)}
        lines += ["| metric | value |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in flat.items()]
        lines.append("")

        per_class = result.summary.get("per_class")
        if isinstance(per_class, dict) and per_class:
            rows = [{"class": k, **v} for k, v in per_class.items()]
            lines += [
                "Per class:",
                "",
                format_markdown_table(rows, ["class", "precision", "recall", "f1", "support"]),
                "",
            ]

        if result.failures:
            columns = list(result.failures[0].keys())
            lines += [
                f"Failures ({len(result.failures)}):",
                "",
                format_markdown_table(result.failures, columns),
                "",
            ]
        else:
            lines += ["No failures.", ""]

    return "\n".join(lines)


async def main(modes: list[str], limit: int | None, write: bool) -> int:
    configure_logging()
    settings = get_settings()
    cases = load_cases()
    if limit:
        cases = cases[:limit]

    print(f"Loaded {len(cases)} eval cases; running: {', '.join(modes)}\n")

    results: list[SuiteResult] = []
    if "retrieval" in modes:
        results.append(await run_retrieval(cases))
    if "decisions" in modes:
        results.append(await run_decisions(cases))
    if "classification" in modes:
        results.append(await run_classification(cases))
    if "draft" in modes:
        results.append(await run_draft_quality(cases))

    for result in results:
        print(f"--- {result.name} ({result.duration_s}s) ---")
        for key, value in result.summary.items():
            if not isinstance(value, dict):
                print(f"  {key:<28} {value}")
        if result.failures:
            print(f"  failures                     {len(result.failures)}")
            for failure in result.failures[:8]:
                print(f"     - {failure}")
        print()

    report = render_report(
        results,
        {
            "model": settings.agent_model,
            "effort": settings.agent_effort,
            "embedding_model": settings.embedding_model,
            "confidence_threshold": settings.escalation_confidence_threshold,
            "retrieval_threshold": settings.escalation_retrieval_threshold,
            "cases": len(cases),
        },
    )

    if write:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = RESULTS_DIR / "latest.md"
        path.write_text(report, encoding="utf-8")
        print(f"Report written to {path}")

    # Non-zero exit if any money-moving decision was wrong, so this can gate CI.
    decisions = next((r for r in results if r.name == "decisions"), None)
    if decisions and decisions.failures:
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        default=",".join(FREE_MODES),
        help=f"comma-separated: {', '.join(ALL_MODES)}, or 'all'",
    )
    parser.add_argument("--limit", type=int, default=None, help="only the first N cases")
    parser.add_argument("--no-write", action="store_true", help="skip writing results/latest.md")
    args = parser.parse_args()

    selected = ALL_MODES if args.mode == "all" else tuple(m.strip() for m in args.mode.split(","))
    unknown = [m for m in selected if m not in ALL_MODES]
    if unknown:
        parser.error(f"unknown mode(s): {', '.join(unknown)}")

    raise SystemExit(asyncio.run(main(list(selected), args.limit, not args.no_write)))
