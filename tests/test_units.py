"""Cost accounting, chunking, policy loading, and tool clamping."""

from decimal import Decimal

import pytest

from app.agents.llm import supports_effort
from app.agents.tools import clamp_to_eligibility, normalise_tool_calls
from app.core.pricing import (
    TokenUsage,
    UnknownModelPricing,
    calculate_cost,
    safe_calculate_cost,
    usage_from_anthropic,
)
from app.rag.chunker import MAX_CHUNK_CHARS, chunk_markdown
from app.rag.loader import PolicyParseError, parse_policy


class TestEffortSupport:
    """`output_config.effort` is a 400 on models that do not accept it."""

    @pytest.mark.parametrize(
        "model", ["claude-sonnet-5", "claude-opus-5", "claude-sonnet-4-6"]
    )
    def test_supported_models_get_effort(self, model):
        assert supports_effort(model)

    @pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-4-5"])
    def test_models_that_reject_effort_do_not_get_it(self, model):
        assert not supports_effort(model)

    def test_dated_snapshot_matches_its_base_model(self):
        assert not supports_effort("claude-haiku-4-5-20251001")

    def test_unknown_model_fails_closed(self):
        # Better to lose the effort hint than to 400 every request.
        assert not supports_effort("claude-something-unreleased")


class TestPricing:
    def test_known_model_costs_are_exact(self):
        # 1M in + 1M out on Opus 5 == $5 + $25.
        cost = calculate_cost(
            "claude-opus-5", TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        )
        assert cost == Decimal("30.000000")

    def test_cache_reads_are_charged_at_a_tenth_of_input(self):
        cost = calculate_cost("claude-opus-5", TokenUsage(cache_read_tokens=1_000_000))
        assert cost == Decimal("0.500000")

    def test_cache_writes_are_charged_at_1_25x_input(self):
        cost = calculate_cost("claude-opus-5", TokenUsage(cache_write_tokens=1_000_000))
        assert cost == Decimal("6.250000")

    def test_dated_snapshot_resolves_to_its_base_model(self):
        assert calculate_cost(
            "claude-haiku-4-5-20251001", TokenUsage(input_tokens=1_000_000)
        ) == Decimal("1.000000")

    def test_unknown_model_raises(self):
        with pytest.raises(UnknownModelPricing):
            calculate_cost("gpt-9", TokenUsage(input_tokens=10))

    def test_unknown_model_is_none_not_zero_on_the_tracing_path(self):
        """A zero would read as 'this call was free', which is a lie."""
        assert safe_calculate_cost("gpt-9", TokenUsage(input_tokens=10)) is None

    def test_usage_totals_add(self):
        combined = TokenUsage(input_tokens=10, output_tokens=5) + TokenUsage(
            input_tokens=1, output_tokens=2
        )
        assert (combined.input_tokens, combined.output_tokens) == (11, 7)

    def test_cached_tokens_are_split_out_of_the_input_total(self):
        """LangChain reports input_tokens inclusive of cache, so billing each
        bucket separately would double-count without this split."""
        usage = usage_from_anthropic(
            {
                "input_tokens": 1000,
                "output_tokens": 50,
                "input_token_details": {"cache_read": 800, "cache_creation": 100},
            }
        )
        assert usage.input_tokens == 100
        assert usage.cache_read_tokens == 800
        assert usage.cache_write_tokens == 100

    def test_missing_usage_is_zero(self):
        assert usage_from_anthropic(None).total_tokens == 0


class TestChunker:
    def test_splits_on_headings_and_keeps_the_document_title(self):
        chunks = chunk_markdown("## One\n\nAlpha.\n\n## Two\n\nBeta.", "Doc")
        assert len(chunks) == 2
        assert chunks[0].heading == "One"
        assert chunks[0].content.startswith("Doc — One")

    def test_preamble_before_any_heading_is_kept(self):
        chunks = chunk_markdown("Intro text.\n\n## One\n\nAlpha.", "Doc")
        assert chunks[0].heading is None
        assert "Intro text." in chunks[0].content

    def test_long_section_is_split_with_overlap(self):
        body = "## Big\n\n" + "\n\n".join(["word " * 120] * 8)
        chunks = chunk_markdown(body, "Doc")
        assert len(chunks) > 1
        assert all(len(c.content) <= MAX_CHUNK_CHARS + 400 for c in chunks)

    def test_chunk_indices_are_sequential(self):
        chunks = chunk_markdown("## A\n\nx\n\n## B\n\ny\n\n## C\n\nz", "Doc")
        assert [c.index for c in chunks] == [0, 1, 2]

    def test_empty_body_yields_nothing(self):
        assert chunk_markdown("", "Doc") == []


POLICY = """---
slug: test-policy
title: Test Policy
category: REFUND_REQUEST
version: "2"
rules:
  return_window_days: 14
---

## Section

Body text.
"""


class TestPolicyLoader:
    def test_frontmatter_and_body_are_parsed(self):
        policy = parse_policy(POLICY, "test.md")
        assert policy.slug == "test-policy"
        assert policy.rules["return_window_days"] == 14
        assert policy.body.startswith("## Section")

    def test_missing_frontmatter_raises(self):
        with pytest.raises(PolicyParseError, match="frontmatter"):
            parse_policy("# Just markdown", "x.md")

    def test_missing_required_field_raises(self):
        with pytest.raises(PolicyParseError, match="category"):
            parse_policy("---\nslug: a\ntitle: b\n---\n\nBody", "x.md")

    def test_hash_changes_with_content_but_not_with_reparsing(self):
        a = parse_policy(POLICY, "test.md")
        b = parse_policy(POLICY, "test.md")
        c = parse_policy(POLICY.replace("Body text.", "Different."), "test.md")
        assert a.content_hash == b.content_hash
        assert a.content_hash != c.content_hash


class TestToolClamping:
    def test_tool_calls_become_proposed_actions(self):
        actions = normalise_tool_calls(
            [{"name": "ProposeRefund", "args": {"amount": 25.5, "reason": "r"}, "id": "c1"}]
        )
        assert actions == [
            {"type": "refund", "reason": "r", "tool_call_id": "c1", "amount": "25.50"}
        ]

    def test_unknown_tool_is_ignored(self):
        assert normalise_tool_calls([{"name": "DoSomethingElse", "args": {}}]) == []

    def test_refund_without_a_usable_amount_is_marked_invalid(self):
        actions = normalise_tool_calls(
            [{"name": "ProposeRefund", "args": {"amount": "abc", "reason": "r"}}]
        )
        assert actions[0]["invalid"] == "missing_or_invalid_amount"

    def test_refund_is_capped_at_the_approved_amount(self):
        kept, notes = clamp_to_eligibility(
            [{"type": "refund", "amount": "999.00", "reason": "r"}],
            {"action": "refund", "eligible": True, "approved_amount": "50.00"},
        )
        assert kept[0]["amount"] == "50.00"
        assert kept[0]["clamped_from"] == "999.00"
        assert notes

    def test_refund_is_dropped_when_eligibility_declined(self):
        kept, notes = clamp_to_eligibility(
            [{"type": "refund", "amount": "10.00", "reason": "r"}],
            {"action": "refund", "eligible": False, "approved_amount": None},
        )
        assert kept == []
        assert notes

    def test_action_of_the_wrong_kind_is_dropped(self):
        kept, _ = clamp_to_eligibility(
            [{"type": "replacement", "reason": "r"}],
            {"action": "refund", "eligible": True, "approved_amount": "50.00"},
        )
        assert kept == []

    def test_no_action_always_survives(self):
        kept, notes = clamp_to_eligibility([{"type": "none", "reason": "r"}], None)
        assert len(kept) == 1
        assert notes == []

    def test_order_action_without_eligibility_is_dropped(self):
        kept, notes = clamp_to_eligibility(
            [{"type": "refund", "amount": "10.00", "reason": "r"}], None
        )
        assert kept == []
        assert notes
