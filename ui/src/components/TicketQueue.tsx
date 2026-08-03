import type { TicketStatus, TicketSummary } from "../types";
import { PriorityBadge, StatusBadge } from "./Badges";

const STATUS_OPTIONS: Array<{ value: TicketStatus | ""; label: string }> = [
  { value: "", label: "All statuses" },
  { value: "AWAITING_APPROVAL", label: "Needs approval" },
  { value: "ESCALATED", label: "Escalated" },
  { value: "RESOLVED", label: "Resolved" },
  { value: "REJECTED", label: "Rejected" },
  { value: "NEW", label: "New" },
  { value: "PROCESSING", label: "Processing" },
  { value: "FAILED", label: "Failed" },
];

interface Props {
  tickets: TicketSummary[];
  total: number;
  selectedId: string | null;
  status: TicketStatus | "";
  search: string;
  loading: boolean;
  onSelect: (id: string) => void;
  onStatusChange: (status: TicketStatus | "") => void;
  onSearchChange: (search: string) => void;
  onNewTicket: () => void;
}

export function TicketQueue({
  tickets,
  total,
  selectedId,
  status,
  search,
  loading,
  onSelect,
  onStatusChange,
  onSearchChange,
  onNewTicket,
}: Props) {
  return (
    <aside className="queue">
      <div className="queue-controls">
        <div className="queue-controls-row">
          <select
            className="select"
            value={status}
            onChange={(e) => onStatusChange(e.target.value as TicketStatus | "")}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <button className="btn btn-primary" onClick={onNewTicket} title="Submit a ticket">
            New
          </button>
        </div>
        <input
          className="input"
          placeholder="Search subject, email or order…"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
        />
        <div className="hint">
          {loading ? <span className="spinner" /> : `${total} ticket${total === 1 ? "" : "s"}`}
        </div>
      </div>

      <div className="queue-list">
        {tickets.length === 0 && !loading ? (
          <div className="empty">No tickets match this view.</div>
        ) : (
          tickets.map((ticket) => (
            <div
              key={ticket.id}
              className={`queue-item${ticket.id === selectedId ? " selected" : ""}`}
              onClick={() => onSelect(ticket.id)}
            >
              <div className="queue-item-top">
                <PriorityBadge priority={ticket.priority} />
                <span className="queue-subject">{ticket.subject}</span>
              </div>
              <div className="queue-meta">
                <StatusBadge status={ticket.status} />
                {ticket.order_ref && <span className="mono">{ticket.order_ref}</span>}
                <span>{ticket.customer_email}</span>
              </div>
            </div>
          ))
        )}
      </div>
    </aside>
  );
}
