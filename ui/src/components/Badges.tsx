import type { Sentiment, TicketStatus } from "../types";

const STATUS_TONE: Record<TicketStatus, string> = {
  NEW: "badge",
  PROCESSING: "badge badge-accent",
  AWAITING_APPROVAL: "badge badge-warn",
  ESCALATED: "badge badge-danger",
  RESOLVED: "badge badge-ok",
  REJECTED: "badge",
  FAILED: "badge badge-danger",
};

const STATUS_LABEL: Record<TicketStatus, string> = {
  NEW: "New",
  PROCESSING: "Processing",
  AWAITING_APPROVAL: "Needs approval",
  ESCALATED: "Escalated",
  RESOLVED: "Resolved",
  REJECTED: "Rejected",
  FAILED: "Failed",
};

export function StatusBadge({ status }: { status: TicketStatus }) {
  return <span className={STATUS_TONE[status]}>{STATUS_LABEL[status]}</span>;
}

/** Priority 1-5 from the SLA rules; 4 and 5 are visually distinct. */
export function PriorityBadge({ priority }: { priority: number | null }) {
  if (priority == null) return <span className="prio" title="Not yet classified">–</span>;
  return (
    <span className={`prio prio-${priority}`} title={`SLA priority ${priority} of 5`}>
      {priority}
    </span>
  );
}

export function SentimentBadge({ sentiment }: { sentiment: Sentiment | null }) {
  if (!sentiment) return null;
  const tone =
    sentiment === "VERY_NEGATIVE"
      ? "badge badge-danger"
      : sentiment === "NEGATIVE"
        ? "badge badge-warn"
        : sentiment === "POSITIVE"
          ? "badge badge-ok"
          : "badge";
  return <span className={tone}>{sentiment.replace("_", " ").toLowerCase()}</span>;
}

export function CategoryBadge({ category }: { category: string | null }) {
  if (!category) return null;
  return <span className="badge">{category.replace(/_/g, " ").toLowerCase()}</span>;
}

/** Classifier confidence; below the escalation threshold it reads as a warning. */
export function ConfidenceBadge({ confidence }: { confidence: number | null }) {
  if (confidence == null) return null;
  const low = confidence < 0.65;
  return (
    <span className={low ? "badge badge-warn" : "badge"} title="Classifier confidence">
      {low ? "low confidence " : "confidence "}
      {confidence.toFixed(2)}
    </span>
  );
}
