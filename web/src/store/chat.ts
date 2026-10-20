import { create } from "zustand";
import { streamAgent } from "../api/client";
import { errMsg } from "../lib/format";
import { readPref, writePref } from "./world";

export interface ChatStep { index: number; tool: string; status?: string; error?: string | null; summary?: string }
export interface ChatTurn {
  id: number;
  command: string;
  steps: ChatStep[];
  answer: { text: string; ok: boolean } | null;
  busy: boolean;
  at: number;
}
export interface HistoryMsg { role: "user" | "assistant"; text: string }

const KEEP_TURNS = 30;      // what is remembered across reloads
const CONTEXT_TURNS = 4;    // earlier turns the agent sees with a new request
const INTERRUPTED = "Interrupted: the page was reloaded before an answer arrived.";

function summarise(tool: string, result: unknown): string {
  if (!result || typeof result !== "object") return "";
  const r = result as Record<string, unknown>;
  if (r.confirmation_required) return "needs your confirmation";
  if (Array.isArray(r.tasks)) return `${r.tasks.length} task(s) created`;
  if (typeof r.error === "string") return r.error;
  if (tool.startsWith("list_") && Array.isArray(r.items)) return `${r.items.length} results`;
  return "";
}

/** The agent keeps no memory of its own, so each request carries the last few finished turns. */
function contextFor(turns: ChatTurn[]): HistoryMsg[] {
  const out: HistoryMsg[] = [];
  for (const t of turns.filter((x) => x.answer && x.answer.text !== INTERRUPTED).slice(-CONTEXT_TURNS)) {
    const tools = [...new Set(t.steps.map((s) => s.tool))];
    out.push({ role: "user", text: t.command });
    out.push({ role: "assistant", text: `${t.answer!.text}${tools.length ? ` (tools used: ${tools.join(", ")})` : ""}` });
  }
  return out;
}

function load(): ChatTurn[] {
  const saved = readPref<ChatTurn[]>("chat", []);
  if (!Array.isArray(saved)) return [];
  return saved.map((t) => ({ ...t, busy: false, answer: t.answer ?? { text: INTERRUPTED, ok: false } }));
}

interface ChatState {
  turns: ChatTurn[];
  busy: boolean;
  collapsed: boolean;
  send: (command: string) => Promise<void>;
  stop: () => void;
  clear: () => void;
  setCollapsed: (v: boolean) => void;
}

let abort: AbortController | null = null;

export const useChat = create<ChatState>((set, get) => ({
  turns: load(),
  busy: false,
  collapsed: readPref<boolean>("chatCollapsed", false),

  setCollapsed: (v) => {
    writePref("chatCollapsed", v);
    set({ collapsed: v });
  },

  clear: () => {
    if (get().busy) return;
    writePref("chat", []);
    set({ turns: [] });
  },

  stop: () => abort?.abort(),

  send: async (raw) => {
    const command = raw.trim();
    if (!command || get().busy) return;
    const history = contextFor(get().turns);
    const id = Math.max(0, ...get().turns.map((t) => t.id)) + 1;
    const patch = (f: (t: ChatTurn) => ChatTurn) => set((s) => ({ turns: s.turns.map((t) => (t.id === id ? f(t) : t)) }));
    set((s) => ({ busy: true, collapsed: false, turns: [...s.turns, { id, command, steps: [], answer: null, busy: true, at: Date.now() }] }));
    abort = new AbortController();
    try {
      await streamAgent(
        command,
        (event, data) => {
          if (event === "step_start") patch((t) => ({ ...t, steps: [...t.steps, { index: Number(data.index), tool: String(data.tool) }] }));
          else if (event === "step_done") {
            patch((t) => ({
              ...t,
              steps: t.steps.map((st) =>
                st.index === Number(data.step_index)
                  ? { ...st, status: String(data.status), error: data.error as string | null, summary: summarise(st.tool, data.result) }
                  : st,
              ),
            }));
          } else if (event === "done") patch((t) => ({ ...t, answer: { text: String(data.summary ?? ""), ok: Boolean(data.success) } }));
        },
        abort.signal,
        history,
      );
    } catch (e) {
      const stopped = (e as Error).name === "AbortError";
      patch((t) => ({ ...t, answer: { text: stopped ? "Stopped." : errMsg(e), ok: false } }));
    } finally {
      patch((t) => ({ ...t, busy: false, answer: t.answer ?? { text: "The agent closed the connection without an answer.", ok: false } }));
      abort = null;
      set({ busy: false });
      writePref("chat", get().turns.filter((t) => !t.busy).slice(-KEEP_TURNS));
    }
  },
}));
