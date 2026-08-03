export type TicketStatus =
  | "NEW"
  | "PROCESSING"
  | "AWAITING_APPROVAL"
  | "ESCALATED"
  | "RESOLVED"
  | "REJECTED"
  | "FAILED";

export type RunStatus =
  | "RUNNING"
  | "AWAITING_APPROVAL"
  | "COMPLETED"
  | "ESCALATED"
  | "REJECTED"
  | "FAILED";

export type Sentiment = "POSITIVE" | "NEUTRAL" | "NEGATIVE" | "VERY_NEGATIVE";

export interface PolicyCitation {
  chunk_id: string;
  document: string;
  slug: string;
  heading: string | null;
  score: number;
}

export interface GuardrailFlag {
  layer: string;
  rule: string;
  severity: "info" | "warn" | "block";
  detail: string;
}

export interface ProposedAction {
  type: "refund" | "replacement" | "none";
  reason: string;
  amount?: string | null;
  status?: string;
  error?: string;
  clamped_from?: string;
  invalid?: string;
}

export interface EligibilityCheck {
  check: string;
  passed: boolean;
  detail: string;
}

export interface Eligibility {
  action: string;
  eligible: boolean;
  reason: string;
  approved_amount: string | null;
  requires_escalation: boolean;
  escalation_reason: string | null;
  checks: EligibilityCheck[];
}

export interface AgentRun {
  id: string;
  status: RunStatus;
  thread_id: string;
  draft_response: string | null;
  final_response: string | null;
  proposed_actions: ProposedAction[];
  executed_actions: ProposedAction[];
  policy_citations: PolicyCitation[];
  guardrail_flags: GuardrailFlag[];
  eligibility: Eligibility | null;
  escalation_reason: string | null;
  prompt_versions: Record<string, string>;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd: string;
  approved_by: string | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface TicketSummary {
  id: string;
  subject: string;
  customer_email: string;
  customer_name: string | null;
  order_ref: string | null;
  status: TicketStatus;
  category: string | null;
  sentiment: Sentiment | null;
  priority: number | null;
  confidence: number | null;
  created_at: string;
  updated_at: string;
}

export interface Ticket extends TicketSummary {
  body: string;
  channel: string;
  classification_meta: Record<string, unknown>;
  resolved_at: string | null;
  latest_run: AgentRun | null;
}

export interface TicketList {
  items: TicketSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface TraceStep {
  step_index: number;
  node_name: string;
  status: string;
  model: string | null;
  input_summary: string | null;
  output_summary: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: string | null;
  latency_ms: number;
  error: string | null;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface Trace {
  ticket_id: string;
  run_id: string;
  run_status: RunStatus;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd: string;
  total_latency_ms: number;
  prompt_versions: Record<string, string>;
  steps: TraceStep[];
}

export interface Stats {
  tickets_total: number;
  by_status: Record<string, number>;
  by_category: Record<string, number>;
  runs_total: number;
  escalation_rate: number;
  auto_resolution_rate: number;
  total_cost_usd: string;
  avg_cost_per_ticket_usd: string;
  total_input_tokens: number;
  total_output_tokens: number;
}
