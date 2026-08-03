import { useState } from "react";
import type { Trace } from "../types";

function money(value: string | null): string {
  if (!value) return "–";
  const n = Number(value);
  return n === 0 ? "–" : `$${n.toFixed(4)}`;
}

export function TracePanel({ trace }: { trace: Trace | null }) {
  const [open, setOpen] = useState(false);

  if (!trace) return null;

  return (
    <section className="card">
      <div className="card-head">
        <span className="card-title">Agent trace</span>
        <span className="faint">{trace.steps.length} steps</span>
        <button className="btn spacer" onClick={() => setOpen((v) => !v)}>
          {open ? "Hide" : "Show"}
        </button>
      </div>

      {open && (
        <>
          <div className="card-body" style={{ padding: 0, overflowX: "auto" }}>
            <table className="trace-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Node</th>
                  <th>Outcome</th>
                  <th className="trace-num">In</th>
                  <th className="trace-num">Out</th>
                  <th className="trace-num">Cost</th>
                  <th className="trace-num">Latency</th>
                </tr>
              </thead>
              <tbody>
                {trace.steps.map((step, index) => (
                  <tr
                    key={`${step.node_name}-${index}`}
                    className={step.status === "error" ? "trace-error" : undefined}
                  >
                    <td className="trace-num faint">{index + 1}</td>
                    <td className="trace-node">{step.node_name}</td>
                    <td className="trace-summary">
                      {step.error ?? step.output_summary ?? "–"}
                    </td>
                    <td className="trace-num">{step.input_tokens || "–"}</td>
                    <td className="trace-num">{step.output_tokens || "–"}</td>
                    <td className="trace-num">{money(step.cost_usd)}</td>
                    <td className="trace-num">{step.latency_ms} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="totals">
            <div className="stat">
              <span className="stat-value">
                {trace.total_input_tokens.toLocaleString()} in /{" "}
                {trace.total_output_tokens.toLocaleString()} out
              </span>
              <span className="stat-label">tokens</span>
            </div>
            <div className="stat">
              <span className="stat-value">${Number(trace.total_cost_usd).toFixed(4)}</span>
              <span className="stat-label">run cost</span>
            </div>
            <div className="stat">
              <span className="stat-value">{trace.total_latency_ms} ms</span>
              <span className="stat-label">total latency</span>
            </div>
            <div className="stat">
              <span className="stat-value mono" style={{ fontSize: 12 }}>
                {Object.entries(trace.prompt_versions)
                  .map(([node, version]) => `${node}:${version}`)
                  .join("  ") || "–"}
              </span>
              <span className="stat-label">prompt versions</span>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
