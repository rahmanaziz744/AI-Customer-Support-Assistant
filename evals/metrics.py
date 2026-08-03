"""Metric helpers.

Written out rather than pulled from scikit-learn: the dataset is small, the
formulas are short, and a reader can check them without a second dependency.
"""

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class ConfusionCounts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class ClassificationReport:
    accuracy: float
    macro_f1: float
    per_class: dict[str, dict[str, float]] = field(default_factory=dict)
    confusions: list[tuple[str, str, str]] = field(default_factory=list)


def classification_report(
    pairs: list[tuple[str, str, str]],
) -> ClassificationReport:
    """Build a report from (item_id, expected, predicted) triples."""
    if not pairs:
        return ClassificationReport(accuracy=0.0, macro_f1=0.0)

    counts: dict[str, ConfusionCounts] = defaultdict(ConfusionCounts)
    correct = 0
    confusions: list[tuple[str, str, str]] = []

    for item_id, expected, predicted in pairs:
        if expected == predicted:
            correct += 1
            counts[expected].tp += 1
        else:
            counts[predicted].fp += 1
            counts[expected].fn += 1
            confusions.append((item_id, expected, predicted))

    per_class = {
        label: {
            "precision": round(c.precision, 4),
            "recall": round(c.recall, 4),
            "f1": round(c.f1, 4),
            "support": c.tp + c.fn,
        }
        for label, c in sorted(counts.items())
    }
    # Macro-averaged over classes that actually appear in the gold labels, so
    # a label the model hallucinated once cannot drag the average down.
    supported = [m["f1"] for m in per_class.values() if m["support"]]
    macro_f1 = sum(supported) / len(supported) if supported else 0.0

    return ClassificationReport(
        accuracy=round(correct / len(pairs), 4),
        macro_f1=round(macro_f1, 4),
        per_class=per_class,
        confusions=confusions,
    )


def binary_report(pairs: list[tuple[str, bool, bool]]) -> dict[str, float]:
    """Precision/recall/F1 for a yes-no decision such as "should escalate"."""
    counts = ConfusionCounts()
    correct = 0
    for _item_id, expected, predicted in pairs:
        if expected and predicted:
            counts.tp += 1
        elif predicted and not expected:
            counts.fp += 1
        elif expected and not predicted:
            counts.fn += 1
        if expected == predicted:
            correct += 1

    return {
        "accuracy": round(correct / len(pairs), 4) if pairs else 0.0,
        "precision": round(counts.precision, 4),
        "recall": round(counts.recall, 4),
        "f1": round(counts.f1, 4),
        "true_positives": counts.tp,
        "false_positives": counts.fp,
        "false_negatives": counts.fn,
    }


def hit_rate_at_k(hits: list[bool]) -> float:
    return round(sum(hits) / len(hits), 4) if hits else 0.0


def format_markdown_table(rows: list[dict[str, object]], columns: list[str]) -> str:
    if not rows:
        return "_no rows_"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(str(row.get(col, "")) for col in columns) + " |" for row in rows
    ]
    return "\n".join([header, sep, *body])
