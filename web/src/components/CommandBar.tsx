import { useEffect, useRef, useState } from "react";
import { CheckCircle2, CircleAlert, Loader2, Sparkles, X } from "lucide-react";
import { api, streamAgent } from "../api/client";
import { errMsg } from "../lib/format";

interface Step { index: number; tool: string; status?: string; error?: string | null; summary?: string }
interface AgentStatus { available: boolean; provider?: string; llm_configured?: boolean; detail?: string }

const EXAMPLES = [
  "Deliver three packages from receiving to storage",
  "Send a robot to the loading zone",
  "Which robots are currently charging?",
  "Why is UGV-03 waiting?",
  "Patrol the north aisle twice with a quadruped",
];

function summarise(tool: string, result: unknown): string {
  if (!result || typeof result !== "object") return "";
  const r = result as Record<string, unknown>;
  if (r.confirmation_required) return "needs your confirmation";
  if (Array.isArray(r.tasks)) return `${r.tasks.length} task(s) created`;
  if (typeof r.error === "string") return r.error;
  if (tool.startsWith("list_") && Array.isArray(r.items)) return `${r.items.length} results`;
  return "";
}

/** Natural-language operator input → agent (LLM) → MCP tools → core. */
export function CommandBar() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [steps, setSteps] = useState<Step[]>([]);
  const [summary, setSummary] = useState<{ text: string; ok: boolean } | null>(null);
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    api.get<AgentStatus>("/agent/status").then(setStatus).catch(() => setStatus({ available: false, detail: "core unreachable" }));
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const run = async (command: string) => {
    if (!command.trim() || busy) return;
    setBusy(true);
    setOpen(true);
    setSteps([]);
    setSummary(null);
    abort.current = new AbortController();
    try {
      await streamAgent(
        command,
        (event, data) => {
          if (event === "step_start") setSteps((s) => [...s, { index: Number(data.index), tool: String(data.tool) }]);
          else if (event === "step_done") {
            setSteps((s) =>
              s.map((st) =>
                st.index === Number(data.step_index)
                  ? { ...st, status: String(data.status), error: data.error as string | null, summary: summarise(st.tool, data.result) }
                  : st,
              ),
            );
          } else if (event === "done") setSummary({ text: String(data.summary ?? ""), ok: Boolean(data.success) });
        },
        abort.current.signal,
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") setSummary({ text: errMsg(e), ok: false });
    } finally {
      setBusy(false);
    }
  };

  const unavailable = status && !status.available;
  return (
    <div className="cmdbar">
      <form
        className="box glass"
        onSubmit={(e) => {
          e.preventDefault();
          void run(text);
        }}
      >
        {busy ? <Loader2 size={15} className="muted spin" /> : <Sparkles size={15} style={{ color: "var(--accent)" }} />}
        <input
          ref={input}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onFocus={() => setOpen(true)}
          placeholder={unavailable ? "Agent offline — structured controls still work" : 'Ask Nayantra… e.g. "Deliver three packages from receiving to storage"'}
          aria-label="Natural-language command"
        />
        <span className="kbd">/</span>
        {open && (
          <button type="button" className="btn ghost sm icon" onClick={() => { setOpen(false); abort.current?.abort(); }} aria-label="Close">
            <X size={14} />
          </button>
        )}
      </form>
      {open && (
        <div className="out glass">
          {!busy && !summary && steps.length === 0 && (
            <div className="col" style={{ gap: 6 }}>
              {unavailable && (
                <div className="reason" style={vars("#fbbf24")}>
                  {status?.detail}. Start the MCP server and the agent API (<span className="mono">scripts/start.sh</span>) to use natural
                  language. The LLM only creates structured tasks; it never drives robots.
                </div>
              )}
              {status?.available && status.llm_configured === false && (
                <div className="reason" style={vars("#fbbf24")}>No API key configured for the {status.provider} provider.</div>
              )}
              <div className="section-title">Try</div>
              {EXAMPLES.map((ex) => (
                <button key={ex} className="btn ghost sm" style={{ justifyContent: "flex-start" }} onClick={() => { setText(ex); void run(ex); }}>
                  {ex}
                </button>
              ))}
            </div>
          )}
          {steps.map((s) => (
            <div key={s.index} className="step-line">
              {s.status === undefined ? <Loader2 size={13} className="muted" /> : s.status === "success" ? <CheckCircle2 size={13} style={{ color: "var(--ok)" }} /> : <CircleAlert size={13} style={{ color: "var(--err)" }} />}
              <div>
                <span className="mono">{s.tool}</span>
                {s.summary && <span className="muted"> — {s.summary}</span>}
                {s.error && <div style={{ color: "var(--err)", fontSize: 12 }}>{s.error}</div>}
              </div>
            </div>
          ))}
          {summary && (
            <div className="reason" style={{ marginTop: steps.length ? 8 : 0, ...vars(summary.ok ? "#34d399" : "#f87171") }}>
              {summary.text}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function vars(c: string) {
  return { "--c": c } as React.CSSProperties;
}
