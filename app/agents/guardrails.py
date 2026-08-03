"""Safety guardrails.

Three layers, in order of how much they are trusted:

1. **Input** — screens the ticket before it reaches a model. Flags prompt
   injection and oversized input. Injection is flagged, not blocked: a real
   customer occasionally writes something that trips a pattern, and a human
   should see the ticket rather than have it silently dropped.
2. **Tool** — enforced in `eligibility.py` and the mock order API, not here.
   A model cannot exceed a refund cap because the number never comes from it.
3. **Output** — checks the finished draft before a human sees it. Catches the
   failure modes that matter: promising money policy does not allow, inventing
   commitments, and leaking data the customer did not supply.

A flag is advisory unless `blocking` is set; blocking flags force escalation.
"""

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_TICKET_CHARS = 12_000

# Phrasings that try to redirect the model rather than describe a problem.
INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction_override", re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
        r"(previous|prior|above|earlier|all)\b[^.\n]{0,20}\b"
        r"(instruction|prompt|rule|polic|direction)", re.I)),
    ("role_reassignment", re.compile(
        r"\b(you are now|act as|pretend to be|from now on you|new instructions?:)", re.I)),
    ("system_prompt_probe", re.compile(
        r"\b(system prompt|your instructions|repeat everything above|"
        r"reveal your|print your (prompt|rules))", re.I)),
    ("authority_claim", re.compile(
        r"\b(as an? (admin|administrator|developer|supervisor|manager)|"
        r"i am (the )?(ceo|admin|developer|owner)|"
        r"(this|it) (has been|was) (pre-?)?approved by)", re.I)),
    ("guardrail_bypass", re.compile(
        r"\b(bypass|skip|without) (the )?(approval|review|verification|guardrail|check)", re.I)),
]

# Topics policy says a human handles, full stop.
ALWAYS_ESCALATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("legal_action", re.compile(
        r"\b(lawyer|attorney|solicitor|sue|suing|lawsuit|legal action|small claims|"
        r"court|litigat)", re.I)),
    ("chargeback", re.compile(r"\b(chargeback|charge back|bank dispute|dispute the charge)", re.I)),
    # "without my authorisation" and "never placed this order" are how customers
    # actually report card fraud; an eval case caught the narrower pattern
    # missing exactly that phrasing.
    ("fraud", re.compile(
        r"\b(fraud|fraudulent|stolen card|unauthoriz|unauthoris|identity theft"
        r"|(did\s?n'?t|never|not)\s+(authoriz|authoris)"
        r"|without\s+(my|his|her|their|the owner'?s)\s+(authoriz|authoris|permission|consent)"
        r"|(never|did\s?n'?t)\s+(placed?|made)\s+(this|that|the)?\s*order"
        r"|order\s+(on|in)\s+my\s+account\s+(i|that\s+i)\s+never)", re.I)),
    ("data_request", re.compile(
        r"\b(gdpr|ccpa|right to be forgotten|erase my data|delete my data|"
        r"data subject|subject access request)", re.I)),
    ("safety_issue", re.compile(
        r"\b(caught fire|catch fire|on fire|smoke|smoking|burn|burnt|electric shock|"
        r"shocked me|explod|swell|overheat|injur|hospital)", re.I)),
    ("media_threat", re.compile(
        r"\b(twitter|reddit|trustpilot|the press|the media|journalist|go public|viral)", re.I)),
    ("self_harm", re.compile(
        r"\b(kill myself|suicide|end my life|self.?harm|want to die)", re.I)),
]

# Commitments a draft must not make on the company's behalf.
OVERPROMISE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("guarantee", re.compile(r"\b(i|we) (can )?(guarantee|promise|assure you)\b", re.I)),
    ("unconditional_exception", re.compile(
        r"\b(mak(e|ing) an exception|waiv(e|ing) the policy|bend(ing)? the rules)\b", re.I)),
    ("unbounded_commitment", re.compile(
        r"\b(whatever it takes|anything you (need|want)|no questions asked)\b", re.I)),
    ("placeholder_left_in", re.compile(
        r"(\[(name|customer|date|amount|order)[^\]]*\]|\{\{)", re.I)),
]

# Data the customer did not give us and we must not echo back.
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("api_key", re.compile(r"\b(sk-[A-Za-z0-9\-_]{16,}|Bearer\s+[A-Za-z0-9\-._~+/]{20,})")),
]

MONEY_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


@dataclass
class GuardrailFlag:
    layer: str  # "input" | "output"
    rule: str
    severity: str  # "info" | "warn" | "block"
    detail: str

    @property
    def blocking(self) -> bool:
        return self.severity == "block"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scan(patterns: list[tuple[str, re.Pattern[str]]], text: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(text)]


def screen_input(subject: str, body: str) -> list[GuardrailFlag]:
    """Screen an inbound ticket. Flags only — nothing here blocks the run."""
    flags: list[GuardrailFlag] = []
    text = f"{subject}\n{body}"

    if len(body) > MAX_TICKET_CHARS:
        flags.append(
            GuardrailFlag(
                layer="input",
                rule="oversized_input",
                severity="warn",
                detail=f"Ticket body is {len(body)} chars (limit {MAX_TICKET_CHARS}); truncated.",
            )
        )

    for rule in _scan(INJECTION_PATTERNS, text):
        # Blocking, not advisory. The prompt already treats the ticket as data,
        # so a well-behaved model would ignore the attempt — but a ticket trying
        # to steer the agent is evidence of an adversarial sender, and that is
        # worth a human's eyes regardless of whether the attempt worked.
        flags.append(
            GuardrailFlag(
                layer="input",
                rule=f"prompt_injection:{rule}",
                severity="block",
                detail=(
                    "Ticket contains text addressed to the agent rather than to "
                    "support, which suggests an attempt to manipulate it. Treated "
                    "as customer data and routed to a human."
                ),
            )
        )

    for rule in _scan(ALWAYS_ESCALATE_PATTERNS, text):
        flags.append(
            GuardrailFlag(
                layer="input",
                rule=f"always_escalate:{rule}",
                severity="block",
                detail=(
                    "Policy requires a human agent for tickets involving "
                    f"{rule.replace('_', ' ')}."
                ),
            )
        )

    return flags


def truncate_body(body: str) -> str:
    if len(body) <= MAX_TICKET_CHARS:
        return body
    return body[:MAX_TICKET_CHARS] + "\n\n[... truncated ...]"


def _extract_amounts(text: str) -> list[Decimal]:
    amounts = []
    for raw in MONEY_RE.findall(text):
        try:
            amounts.append(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    return amounts


def screen_output(
    draft: str,
    *,
    eligibility: dict[str, Any] | None,
    proposed_actions: list[dict[str, Any]] | None = None,
    original_body: str = "",
) -> list[GuardrailFlag]:
    """Validate a finished draft against policy and the eligibility verdict."""
    flags: list[GuardrailFlag] = []
    proposed_actions = proposed_actions or []

    if not draft or not draft.strip():
        return [
            GuardrailFlag(
                layer="output",
                rule="empty_draft",
                severity="block",
                detail="The model produced no reply text.",
            )
        ]

    for rule in _scan(OVERPROMISE_PATTERNS, draft):
        flags.append(
            GuardrailFlag(
                layer="output",
                rule=f"overpromise:{rule}",
                severity="block",
                detail=f"Draft contains an unsupported commitment ({rule.replace('_', ' ')}).",
            )
        )

    # Only flag PII the draft introduced; echoing back what the customer wrote
    # is their own data and not a leak.
    for rule, pattern in PII_PATTERNS:
        for match in pattern.findall(draft):
            value = match if isinstance(match, str) else match[0]
            if value and value not in original_body:
                flags.append(
                    GuardrailFlag(
                        layer="output",
                        rule=f"pii:{rule}",
                        severity="block",
                        detail=f"Draft contains what looks like {rule.replace('_', ' ')} "
                        "not present in the customer's message.",
                    )
                )
                break

    # The decisive check: the draft must not offer money the engine refused.
    if eligibility:
        eligible = bool(eligibility.get("eligible"))
        action = str(eligibility.get("action", ""))
        approved_raw = eligibility.get("approved_amount")

        if not eligible and action == "refund":
            offers_refund = any(a.get("type") == "refund" for a in proposed_actions)
            if offers_refund:
                flags.append(
                    GuardrailFlag(
                        layer="output",
                        rule="action_contradicts_eligibility",
                        severity="block",
                        detail="A refund was proposed although eligibility declined it.",
                    )
                )
            # Deliberately broad. A false positive costs a human glance; a miss
            # sends a customer a refund promise the company will not honour.
            if re.search(
                r"\b("
                r"refund(ing|ed)?\s+(you|your)"
                r"|(issu|process|arrang|approv|authoris|authoriz)\w*\s+"
                r"(a|the|your|this|full|partial)?\s*refund"
                r"|refund\s+(has been|is|will be)\s+(approved|issued|processed|arranged)"
                r"|your\s+(full\s+|partial\s+)?refund"
                r"|money back"
                r")\b",
                draft,
                re.I,
            ):
                flags.append(
                    GuardrailFlag(
                        layer="output",
                        rule="promises_declined_refund",
                        severity="block",
                        detail="Draft appears to promise a refund that eligibility declined.",
                    )
                )

        if eligible and approved_raw:
            try:
                approved = Decimal(str(approved_raw))
            except InvalidOperation:
                approved = None
            if approved is not None:
                for amount in _extract_amounts(draft):
                    if amount > approved:
                        flags.append(
                            GuardrailFlag(
                                layer="output",
                                rule="amount_exceeds_approved",
                                severity="block",
                                detail=(
                                    f"Draft mentions ${amount}, above the approved "
                                    f"${approved}."
                                ),
                            )
                        )
                        break

    for action in proposed_actions:
        if action.get("type") != "refund":
            continue
        if not eligibility:
            flags.append(
                GuardrailFlag(
                    layer="output",
                    rule="refund_without_eligibility",
                    severity="block",
                    detail="A refund was proposed with no eligibility decision on record.",
                )
            )
            break
        try:
            amount = Decimal(str(action.get("amount", "0")))
            approved = Decimal(str(eligibility.get("approved_amount") or "0"))
        except InvalidOperation:
            continue
        if amount > approved:
            flags.append(
                GuardrailFlag(
                    layer="output",
                    rule="proposed_amount_exceeds_approved",
                    severity="block",
                    detail=f"Proposed refund ${amount} exceeds approved ${approved}.",
                )
            )
            break

    return flags


def blocking_flags(flags: list[GuardrailFlag]) -> list[GuardrailFlag]:
    return [f for f in flags if f.blocking]
