import type { Stats, Ticket, TicketList, TicketStatus, Trace } from "./types";

/** Error carrying the API's structured envelope, so callers can show the real reason. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    let code: string | undefined;
    try {
      const body = await response.json();
      message = body?.error?.message ?? message;
      code = body?.error?.code;
    } catch {
      // Non-JSON error body (proxy or gateway); keep the status-based message.
    }
    throw new ApiError(message, response.status, code);
  }

  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export interface TicketQuery {
  status?: TicketStatus | "";
  search?: string;
  limit?: number;
}

export const api = {
  listTickets({ status, search, limit = 100 }: TicketQuery = {}): Promise<TicketList> {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (search) params.set("search", search);
    params.set("limit", String(limit));
    return request<TicketList>(`/api/tickets?${params}`);
  },

  getTicket(id: string): Promise<Ticket> {
    return request<Ticket>(`/api/tickets/${id}`);
  },

  getTrace(id: string): Promise<Trace> {
    return request<Trace>(`/api/tickets/${id}/trace`);
  },

  getStats(): Promise<Stats> {
    return request<Stats>("/api/stats");
  },

  createTicket(payload: Record<string, unknown>): Promise<Ticket> {
    return request<Ticket>("/api/tickets", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  approve(id: string, payload: { approver?: string; edited_draft?: string; note?: string }) {
    return request(`/api/tickets/${id}/approve`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  reject(id: string, payload: { approver?: string; note?: string }) {
    return request(`/api/tickets/${id}/reject`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
};
