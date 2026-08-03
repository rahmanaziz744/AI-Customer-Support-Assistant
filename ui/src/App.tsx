import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { NewTicketModal } from "./components/NewTicketModal";
import { TicketDetail } from "./components/TicketDetail";
import { TicketQueue } from "./components/TicketQueue";
import type { Stats, Ticket, TicketStatus, TicketSummary, Trace } from "./types";

const APPROVER = "reviewer@northwind.test";
const POLL_MS = 2500;

export default function App() {
  const [tickets, setTickets] = useState<TicketSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState<TicketStatus | "">("");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [showNew, setShowNew] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Kept in a ref so the polling effect does not re-subscribe on every keystroke.
  const filters = useRef({ status, search });
  filters.current = { status, search };

  const refreshList = useCallback(async () => {
    try {
      const { status: s, search: q } = filters.current;
      const data = await api.listTickets({ status: s, search: q });
      setTickets(data.items);
      setTotal(data.total);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load tickets");
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshStats = useCallback(async () => {
    try {
      setStats(await api.getStats());
    } catch {
      // Stats are decorative; a failure here should not disrupt the queue.
    }
  }, []);

  const refreshSelected = useCallback(async (id: string) => {
    try {
      const detail = await api.getTicket(id);
      setTicket(detail);
      // A ticket that never ran has no trace; that is expected, not an error.
      setTrace(detail.latest_run ? await api.getTrace(id).catch(() => null) : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load ticket");
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void refreshList();
  }, [status, search, refreshList]);

  useEffect(() => {
    void refreshStats();
  }, [refreshStats]);

  useEffect(() => {
    if (selectedId) void refreshSelected(selectedId);
  }, [selectedId, refreshSelected]);

  // Poll only while work is in flight — a queue of settled tickets does not
  // need to hammer the API.
  const inFlight =
    tickets.some((t) => t.status === "NEW" || t.status === "PROCESSING") ||
    ticket?.status === "NEW" ||
    ticket?.status === "PROCESSING";

  useEffect(() => {
    if (!inFlight) return;
    const timer = setInterval(() => {
      void refreshList();
      if (selectedId) void refreshSelected(selectedId);
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [inFlight, selectedId, refreshList, refreshSelected]);

  async function afterDecision() {
    if (selectedId) await refreshSelected(selectedId);
    await Promise.all([refreshList(), refreshStats()]);
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          Northwind Support Console<span>agent review queue</span>
        </div>
        {stats && (
          <div className="topbar-stats">
            <div className="stat">
              <span className="stat-value">{stats.by_status.AWAITING_APPROVAL ?? 0}</span>
              <span className="stat-label">awaiting approval</span>
            </div>
            <div className="stat">
              <span className="stat-value">{stats.by_status.ESCALATED ?? 0}</span>
              <span className="stat-label">escalated</span>
            </div>
            <div className="stat">
              <span className="stat-value">
                {(stats.escalation_rate * 100).toFixed(0)}%
              </span>
              <span className="stat-label">escalation rate</span>
            </div>
            <div className="stat">
              <span className="stat-value">
                ${Number(stats.total_cost_usd).toFixed(3)}
              </span>
              <span className="stat-label">model spend</span>
            </div>
            <div className="stat">
              <span className="stat-value">
                ${Number(stats.avg_cost_per_ticket_usd).toFixed(4)}
              </span>
              <span className="stat-label">per ticket</span>
            </div>
          </div>
        )}
      </header>

      <div className="main">
        <TicketQueue
          tickets={tickets}
          total={total}
          selectedId={selectedId}
          status={status}
          search={search}
          loading={loading}
          onSelect={setSelectedId}
          onStatusChange={setStatus}
          onSearchChange={setSearch}
          onNewTicket={() => setShowNew(true)}
        />

        <main className="detail">
          {error && <div className="notice notice-danger">{error}</div>}

          {!ticket ? (
            <div className="empty">
              Select a ticket to review the agent's draft, the policy it relied on, and
              the decision it reached.
            </div>
          ) : (
            <TicketDetail
              ticket={ticket}
              trace={trace}
              onApprove={async (editedDraft) => {
                await api.approve(ticket.id, {
                  approver: APPROVER,
                  ...(editedDraft ? { edited_draft: editedDraft } : {}),
                });
                await afterDecision();
              }}
              onReject={async (note) => {
                await api.reject(ticket.id, { approver: APPROVER, note });
                await afterDecision();
              }}
            />
          )}
        </main>
      </div>

      {showNew && (
        <NewTicketModal
          onClose={() => setShowNew(false)}
          onSubmit={async (payload) => {
            const created = await api.createTicket(payload);
            setSelectedId(created.id);
            await refreshList();
          }}
        />
      )}
    </div>
  );
}
