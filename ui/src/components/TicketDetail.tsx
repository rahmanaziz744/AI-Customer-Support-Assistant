import { useEffect, useState } from "react";
import type { Ticket, Trace } from "../types";
import {
  CategoryBadge,
  ConfidenceBadge,
  PriorityBadge,
  SentimentBadge,
  StatusBadge,
} from "./Badges";
import { TracePanel } from "./TracePanel";

interface Props {
  ticket: Ticket;
  trace: Trace | null;
  onApprove: (editedDraft: string | null) => Promise<void>;
  onReject: (note: string) => Promise<void>;
}

const APPROVER = "reviewer@northwind.test";

export function TicketDetail({ ticket, trace, onApprove, onReject }: Props) {
  const run = ticket.latest_run;
  const [draft, setDraft] = useState(run?.final_response ?? run?.draft_response ?? "");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Reset the editor when a different ticket (or a new run) loads, otherwise the
  // previous ticket's draft would linger in the textarea.
  useEffect(() => {
    setDraft(run?.final_response ?? run?.draft_response ?? "");
    setError(null);
  }, [ticket.id, run?.id, run?.draft_response, run?.final_response]);

  const awaiting = ticket.status === "AWAITING_APPROVAL";
  const original = run?.draft_response ?? "";
  const edited = awaiting && draft.trim() !== original.trim();

  async function act(kind: "approve" | "reject") {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "approve") await onApprove(edited ? draft : null);
      else await onReject("Rejected from the console");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <header className="detail-head">
        <h1 className="detail-subject">{ticket.subject}</h1>
        <div className="detail-sub">
          <StatusBadge status={ticket.status} />
          <span>{ticket.customer_name ?? ticket.customer_email}</span>
          {ticket.order_ref && <span className="mono">{ticket.order_ref}</span>}
          <span className="faint">{new Date(ticket.created_at).toLocaleString()}</span>
        </div>
        <div className="chips">
          <PriorityBadge priority={ticket.priority} />
          <CategoryBadge category={ticket.category} />
          <SentimentBadge sentiment={ticket.sentiment} />
          <ConfidenceBadge confidence={ticket.confidence} />
        </div>
      </header>

      {ticket.status === "PROCESSING" && (
        <div className="notice notice-warn">
          <span className="spinner" /> The agent is working on this ticket…
        </div>
      )}

      {run?.escalation_reason && (
        <div className="notice notice-danger">
          <strong>Escalated to a human.</strong> {run.escalation_reason}
        </div>
      )}

      {ticket.status === "RESOLVED" && (
        <div className="notice notice-ok">
          Reply sent{run?.approved_by ? ` — approved by ${run.approved_by}` : ""}.
        </div>
      )}

      <section className="card">
        <div className="card-head">
          <span className="card-title">Customer message</span>
        </div>
        <div className="card-body">
          <div className="ticket-body">{ticket.body}</div>
        </div>
      </section>

      {run?.eligibility && (
        <section className="card">
          <div className="card-head">
            <span className="card-title">Eligibility decision</span>
            <span
              className={run.eligibility.eligible ? "badge badge-ok" : "badge badge-danger"}
            >
              {run.eligibility.eligible ? "eligible" : "not eligible"}
            </span>
            {run.eligibility.approved_amount && (
              <span className="badge badge-accent">
                up to ${run.eligibility.approved_amount}
              </span>
            )}
          </div>
          <div className="card-body">
            <p className="dim" style={{ marginTop: 0 }}>
              {run.eligibility.reason}
            </p>
            {run.eligibility.checks.map((check) => (
              <div className="check" key={check.check}>
                <span
                  className={`check-mark ${check.passed ? "check-pass" : "check-fail"}`}
                >
                  {check.passed ? "✓" : "✕"}
                </span>
                <span className="check-name">{check.check}</span>
                <span className="check-detail">{check.detail}</span>
              </div>
            ))}
            <p className="hint" style={{ marginBottom: 0 }}>
              Computed by a deterministic rules engine, not the model. The draft cannot
              exceed it.
            </p>
          </div>
        </section>
      )}

      {run && run.guardrail_flags.length > 0 && (
        <section className="card">
          <div className="card-head">
            <span className="card-title">Guardrail flags</span>
          </div>
          <div className="card-body">
            {run.guardrail_flags.map((flag, index) => (
              <div key={`${flag.rule}-${index}`} className={`flag flag-${flag.severity}`}>
                <span className="flag-rule">
                  {flag.layer} · {flag.rule}
                </span>
                {flag.detail}
              </div>
            ))}
          </div>
        </section>
      )}

      {run && run.policy_citations.length > 0 && (
        <section className="card">
          <div className="card-head">
            <span className="card-title">Retrieved policy</span>
            <span className="faint">{run.policy_citations.length} chunks</span>
          </div>
          <div className="card-body">
            {run.policy_citations.map((citation) => (
              <div className="citation" key={citation.chunk_id}>
                <span className="citation-doc">{citation.document}</span>
                {citation.heading && (
                  <span className="citation-heading">— {citation.heading}</span>
                )}
                <span className="score">{citation.score.toFixed(3)}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {run?.draft_response && (
        <section className="card">
          <div className="card-head">
            <span className="card-title">
              {awaiting ? "Draft reply — review before sending" : "Reply"}
            </span>
            {edited && <span className="badge badge-warn">edited</span>}
          </div>
          <div className="card-body">
            {awaiting ? (
              <textarea
                className="textarea"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
            ) : (
              <div className="ticket-body">
                {run.final_response ?? run.draft_response}
              </div>
            )}

            {run.proposed_actions.length > 0 && (
              <div style={{ marginTop: 13 }}>
                <div className="stat-label" style={{ marginBottom: 6 }}>
                  Proposed actions
                </div>
                {run.proposed_actions.map((action, index) => (
                  <div key={index} className="check">
                    <span className="check-mark">→</span>
                    <span className="check-name">
                      {action.type}
                      {action.amount ? ` $${action.amount}` : ""}
                    </span>
                    <span className="check-detail">{action.reason}</span>
                  </div>
                ))}
              </div>
            )}

            {run.executed_actions.length > 0 && (
              <div style={{ marginTop: 13 }}>
                <div className="stat-label" style={{ marginBottom: 6 }}>
                  Executed
                </div>
                {run.executed_actions.map((action, index) => (
                  <div key={index} className="check">
                    <span
                      className={`check-mark ${
                        action.status === "executed" ? "check-pass" : "check-fail"
                      }`}
                    >
                      {action.status === "executed" ? "✓" : "✕"}
                    </span>
                    <span className="check-name">
                      {action.type}
                      {action.amount ? ` $${action.amount}` : ""}
                    </span>
                    <span className="check-detail">
                      {action.error ?? action.status}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {error && (
              <div className="notice notice-danger" style={{ marginTop: 13 }}>
                {error}
              </div>
            )}

            {awaiting && (
              <div className="actions">
                <button
                  className="btn btn-primary"
                  disabled={busy !== null || !draft.trim()}
                  onClick={() => act("approve")}
                >
                  {busy === "approve" ? "Sending…" : "Approve and send"}
                </button>
                <button
                  className="btn btn-danger"
                  disabled={busy !== null}
                  onClick={() => act("reject")}
                >
                  {busy === "reject" ? "Rejecting…" : "Reject"}
                </button>
                <span className="hint">
                  {run.proposed_actions.some((a) => a.type !== "none")
                    ? "Approving also executes the proposed order actions."
                    : "No order action will be taken."}{" "}
                  Signed in as {APPROVER}.
                </span>
              </div>
            )}
          </div>
        </section>
      )}

      <TracePanel trace={trace} />
    </div>
  );
}
