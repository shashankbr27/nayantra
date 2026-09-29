import { useState } from "react";
import {
  AlertTriangle, Bot, CheckCircle2, CircleAlert, Info, LayoutDashboard, ListChecks, Map as MapIcon, Moon, OctagonX,
  Server, ShieldAlert, Sun, Users,
} from "lucide-react";
import { api, isConfirmation } from "../api/client";
import { type Page, useWorld } from "../store/world";
import { ago, errMsg } from "../lib/format";
import { Modal, vars } from "./ui";

const PAGES: { id: Page; label: string; icon: React.ReactNode }[] = [
  { id: "operations", label: "Operations", icon: <LayoutDashboard size={14} /> },
  { id: "fleets", label: "Fleets", icon: <Users size={14} /> },
  { id: "robots", label: "Robots", icon: <Bot size={14} /> },
  { id: "tasks", label: "Tasks", icon: <ListChecks size={14} /> },
  { id: "map", label: "Map editor", icon: <MapIcon size={14} /> },
  { id: "system", label: "System", icon: <Server size={14} /> },
];

export function Logo() {
  return (
    <svg width="20" height="20" viewBox="0 0 32 32" aria-hidden>
      <circle cx="16" cy="16" r="10" fill="none" stroke="currentColor" strokeWidth="2.4" />
      <circle cx="16" cy="16" r="3.4" fill="currentColor" />
      <path d="M16 2v6M16 24v6M2 16h6M24 16h6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  );
}

export function TopBar() {
  const page = useWorld((s) => s.page);
  const setPage = useWorld((s) => s.setPage);
  const connected = useWorld((s) => s.connected);
  const theme = useWorld((s) => s.theme);
  const setTheme = useWorld((s) => s.setTheme);
  const alerts = useWorld((s) => s.alerts);
  const toast = useWorld((s) => s.toast);
  const active = Object.values(alerts).filter((a) => a.status === "active");
  const critical = active.some((a) => a.severity === "critical" || a.severity === "error");
  return (
    <header className="topbar">
      <div className="brand"><Logo /> NAYANTRA</div>
      <nav className="nav">
        {PAGES.map((p) => (
          <button key={p.id} className={page === p.id ? "active" : ""} onClick={() => setPage(p.id)}>
            {p.icon} {p.label}
          </button>
        ))}
      </nav>
      <div className="spacer" />
      {active.length > 0 && (
        <span className="badge" style={vars({ "--c": critical ? "#f87171" : "#fbbf24" })} title={active.map((a) => a.title).join("\n")}>
          <AlertTriangle size={12} /> {active.length} alert{active.length > 1 ? "s" : ""}
        </span>
      )}
      <span className="conn">
        <span className={`dot${connected ? "" : " pulse"}`} style={vars({ "--c": connected ? "#34d399" : "#f87171" })} />
        {connected ? "Live" : "Reconnecting…"}
      </span>
      <button className="btn ghost icon" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} title="Toggle theme" aria-label="Toggle theme">
        {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
      </button>
      <button
        className="estop small"
        title="Emergency-stop every robot (asks for confirmation)"
        onClick={async () => {
          try {
            const r = await api.post("/robots/estop-all", { reason: "Global E-STOP from the top bar" });
            if (!isConfirmation(r)) toast("error", "Unexpected response");
          } catch (e) {
            toast("error", errMsg(e));
          }
        }}
      >
        <OctagonX size={14} /> E-STOP ALL
      </button>
    </header>
  );
}

/** Every dangerous action (from the UI or the LLM) lands here for an explicit decision. */
export function ConfirmDialog() {
  const confirmations = useWorld((s) => s.confirmations);
  const toast = useWorld((s) => s.toast);
  const [busy, setBusy] = useState(false);
  const pending = Object.values(confirmations)
    .filter((c) => c.status === "pending" && c.expires_at * 1000 > Date.now())
    .sort((a, b) => a.created_at - b.created_at);
  const c = pending[0];
  if (!c) return null;
  const decide = async (confirm: boolean) => {
    setBusy(true);
    try {
      const r = await api.post<{ status: string; result?: { error?: string } }>(`/confirmations/${c.id}/${confirm ? "confirm" : "reject"}`);
      if (confirm) {
        if (r.status === "executed") toast("success", `Done: ${c.summary}`);
        else toast("error", `Failed: ${r.result?.error ?? r.status}`);
      }
    } catch (e) {
      toast("error", errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const fromLLM = c.requested_by === "mcp" || c.trace?.source === "llm";
  const estop = c.kind === "estop_all";
  return (
    <Modal
      size="sm"
      title={<span className="row"><ShieldAlert size={16} style={{ color: "var(--warn)" }} /> Confirmation required</span>}
      onClose={() => void decide(false)}
      footer={
        <>
          <span className="faint" style={{ fontSize: 12 }}>{pending.length > 1 ? `${pending.length - 1} more waiting` : `expires ${ago(c.expires_at).replace(" ago", "")}`}</span>
          <div className="row">
            <button className="btn" disabled={busy} onClick={() => void decide(false)}>Cancel</button>
            {estop ? (
              <button className="estop" disabled={busy} onClick={() => void decide(true)}><OctagonX size={14} /> Confirm</button>
            ) : (
              <button className="btn primary" disabled={busy} onClick={() => void decide(true)}>Confirm</button>
            )}
          </div>
        </>
      }
    >
      <div className="col" style={{ gap: 10 }}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>{c.summary}</div>
        {c.detail && <div className="muted">{c.detail}</div>}
        <div className="reason" style={vars({ "--c": fromLLM ? "#a78bfa" : "#60a5fa" })}>
          Requested by <b>{fromLLM ? "the AI agent" : c.requested_by}</b>
          {c.trace?.command ? <> for the command “{c.trace.command}”</> : null}
          {c.trace?.tool ? <> via tool <span className="mono">{c.trace.tool}</span></> : null}. Nothing happens unless you confirm.
        </div>
      </div>
    </Modal>
  );
}

export function Toasts() {
  const toasts = useWorld((s) => s.toasts);
  const dismiss = useWorld((s) => s.dismissToast);
  const icon = { info: <Info size={14} />, success: <CheckCircle2 size={14} />, warning: <AlertTriangle size={14} />, error: <CircleAlert size={14} /> };
  const color = { info: "var(--info)", success: "var(--ok)", warning: "var(--warn)", error: "var(--err)" };
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.id} className="toast glass" onClick={() => dismiss(t.id)} style={{ borderLeft: `3px solid ${color[t.kind]}` }}>
          <span style={{ color: color[t.kind], display: "inline-flex" }}>{icon[t.kind]}</span>
          {t.text}
        </div>
      ))}
    </div>
  );
}
