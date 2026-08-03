# Evaluation Results

Generated 2026-08-03 14:30 UTC

| setting | value |
| --- | --- |
| model | claude-opus-5 |
| effort | medium |
| embedding_model | BAAI/bge-small-en-v1.5 |
| confidence_threshold | 0.65 |
| retrieval_threshold | 0.35 |
| cases | 40 |

## retrieval

_ran in 1.47s_

| metric | value |
| --- | --- |
| cases | 40 |
| hit_rate@1 | 0.85 |
| hit_rate@4 | 1.0 |
| mean_top_score | 0.664 |
| min_top_score | 0.4754 |

No failures.

## decisions

_ran in 5.32s_

| metric | value |
| --- | --- |
| cases | 40 |
| eligibility_accuracy | 1.0 |
| eligibility_macro_f1 | 1.0 |
| escalation_cases_scored | 39 |
| escalation_skipped_needs_classifier | 1 |
| escalation_accuracy | 1.0 |
| escalation_precision | 1.0 |
| escalation_recall | 1.0 |
| escalation_f1 | 1.0 |
| missed_escalations | 0 |
| over_escalations | 0 |

No failures.
