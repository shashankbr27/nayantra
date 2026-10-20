import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUp, Check, CheckCircle2, ChevronDown, ChevronUp, CircleAlert, Copy, Loader2, Sparkles, Square, SquarePen, Wrench } from "lucide-react";
import { api } from "../api/client";
import { RichText } from "../lib/richtext";
import { type ChatTurn, useChat } from "../store/chat";
import { Logo } from "./Logo";
import { ThinkingIcon, ThinkingLine } from "./Thinking";
import { vars } from "./ui";

interface AgentStatus { available: boolean; provider?: string; llm_configured?: boolean; detail?: string }

const EXAMPLES = [
  "Deliver three packages from receiving to storage",
  "Send a robot to the loading zone",
  "Which robots are currently charging?",
  "Why is UGV-03 waiting?",
];

const MAX_INPUT_PX = 140;

/** Operator ↔ agent conversation, docked in the right-hand column. The agent turns sentences into
 *  structured tasks through MCP tools; it never drives robots itself. */
export function ChatPanel() {
  const turns = useChat((s) => s.turns);
  const busy = useChat((s) => s.busy);
  const collapsed = useChat((s) => s.collapsed);
  const send = useChat((s) => s.send);
  const stop = useChat((s) => s.stop);
  const clear = useChat((s) => s.clear);
  const setCollapsed = useChat((s) => s.setCollapsed);
  const [text, setText] = useState("");
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [focusTick, setFocusTick] = useState(0);
  const input = useRef<HTMLTextAreaElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);   // follow new output unless the operator scrolled up to re-read

  useEffect(() => {
    api.get<AgentStatus>("/agent/status").then(setStatus).catch(() => setStatus({ available: false, detail: "core unreachable" }));
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) {
        e.preventDefault();
        useChat.getState().setCollapsed(false);
        setFocusTick((n) => n + 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (focusTick) input.current?.focus();
  }, [focusTick, collapsed]);

  // Grow the box with its text (up to a few lines), shrink it back after sending.
  useLayoutEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_INPUT_PX)}px`;
  }, [text, collapsed]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current && turns.length) el.scrollTop = el.scrollHeight;   // an empty chat stays at its top
  }, [turns, collapsed]);

  const submit = () => {
    if (!text.trim() || busy) return;
    stick.current = true;
    const v = text;
    setText("");
    void send(v);
  };
  const ask = (q: string) => {
    stick.current = true;
    void send(q);
  };

  if (collapsed) {
    return (
      <button type="button" className="chat-bar" onClick={() => { setCollapsed(false); setFocusTick((n) => n + 1); }}>
        {busy ? <ThinkingIcon size={26} /> : <Sparkles size={16} />}
        <span className="grow">{busy ? "Working…" : "Command anything…"}</span>
        {!busy && <span className="kbd">/</span>}
        <ChevronUp size={15} />
      </button>
    );
  }

  const unavailable = status && !status.available;
  const noKey = status?.available && status.llm_configured === false;
  return (
    <section className="chat" aria-label="Assistant">
      <header className="chat-head">
        <span className="chat-title"><Sparkles size={15} style={{ color: "var(--accent-fg)" }} /> Assistant</span>
        {status?.provider && status.available && <span className="faint chat-model">{status.provider}</span>}
        <span className="grow" />
        <button type="button" className="btn ghost sm icon" onClick={clear} disabled={busy || turns.length === 0} title="New chat" aria-label="New chat">
          <SquarePen size={14} />
        </button>
        <button type="button" className="btn ghost sm icon" onClick={() => setCollapsed(true)} title="Collapse" aria-label="Collapse">
          <ChevronDown size={15} />
        </button>
      </header>

      <div
        className="chat-scroll"
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        }}
      >
        {unavailable && (
          <div className="reason" style={vars({ "--c": "#fbbf24" })}>
            {status?.detail}. Start the stack with <span className="mono">./nayantra.sh start</span> to use natural language. The LLM only
            creates structured tasks; it never drives robots.
          </div>
        )}
        {noKey && <div className="reason" style={vars({ "--c": "#fbbf24" })}>No API key configured for the {status?.provider} provider.</div>}

        {turns.length === 0 ? (
          <div className="chat-empty">
            <span className="hero-mark"><Logo size={40} /></span>
            <h2>Command anything</h2>
            <p>Say what you want done in plain words. Risky actions always ask you to confirm first.</p>
            <div className="suggest">
              {EXAMPLES.map((ex) => (
                <button key={ex} type="button" onClick={() => ask(ex)} disabled={busy}>
                  <Sparkles size={13} /> {ex}
                </button>
              ))}
            </div>
          </div>
        ) : (
          turns.map((t) => <Message key={t.id} turn={t} />)
        )}
      </div>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="composer-box">
          <textarea
            ref={input}
            rows={1}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              } else if (e.key === "Escape") input.current?.blur();
            }}
            placeholder={unavailable ? "Agent offline — structured controls still work" : "Command anything…"}
            aria-label="Natural-language command"
          />
          {busy ? (
            <button type="button" className="send stop" onClick={stop} title="Stop" aria-label="Stop">
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button type="submit" className="send" disabled={!text.trim()} title="Send (Enter)" aria-label="Send">
              <ArrowUp size={17} strokeWidth={2.4} />
            </button>
          )}
        </div>
        <div className="composer-foot">Enter to send · Shift+Enter for a new line</div>
      </form>
    </section>
  );
}

function Message({ turn }: { turn: ChatTurn }) {
  const a = turn.answer;
  return (
    <div className="turn">
      <div className="msg-user">{turn.command}</div>
      <div className="msg-bot">
        <div className={`avatar${turn.busy ? " live" : ""}`}>{turn.busy ? <ThinkingIcon size={30} /> : <Logo size={19} />}</div>
        <div className="bot-body">
          {turn.steps.length > 0 && <Steps turn={turn} />}
          {turn.busy && <ThinkingLine since={turn.at} steps={turn.steps} />}
          {a && (
            <div className="answer-wrap">
              <div className={`answer${a.ok ? "" : " bad"}`}>
                {!a.ok && <CircleAlert size={14} className="bad-ico" />}
                <div className="answer-text">{a.text ? <RichText text={a.text} /> : "Done."}</div>
              </div>
              {a.text && <CopyButton text={a.text} />}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Steps({ turn }: { turn: ChatTurn }) {
  const [open, setOpen] = useState(false);
  const show = turn.busy || open;
  const n = turn.steps.length;
  const failed = turn.steps.filter((s) => s.status && s.status !== "success").length;
  return (
    <div className="steps-box">
      <button type="button" className="steps-toggle" onClick={() => setOpen(!open)} aria-expanded={show} disabled={turn.busy}>
        <Wrench size={12} />
        {turn.busy ? "Working" : `Used ${n} tool${n > 1 ? "s" : ""}`}
        {failed > 0 && <span style={{ color: "var(--err)" }}>· {failed} failed</span>}
        {!turn.busy && <ChevronDown size={12} className={show ? "open" : ""} />}
      </button>
      {show &&
        turn.steps.map((s) => (
          <div key={s.index} className="step-line">
            {s.status === undefined ? (
              <Loader2 size={13} className="muted spin" />
            ) : s.status === "success" ? (
              <CheckCircle2 size={13} style={{ color: "var(--ok)" }} />
            ) : (
              <CircleAlert size={13} style={{ color: "var(--err)" }} />
            )}
            <div>
              <span className="mono">{s.tool}</span>
              {s.summary && <span className="muted"> — {s.summary}</span>}
              {s.error && <div style={{ color: "var(--err)", fontSize: 12 }}>{s.error}</div>}
            </div>
          </div>
        ))}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn ghost sm icon copy"
      title="Copy answer"
      aria-label="Copy answer"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          window.setTimeout(() => setDone(false), 1500);
        });
      }}
    >
      {done ? <Check size={13} /> : <Copy size={13} />}
    </button>
  );
}
