// Thin REST client. Every call identifies itself as the operator UI so the
// core records "operator" as the origin and honours ?confirm=true.

import type { PendingAction } from "./types";

export class ApiError extends Error {
  status: number;
  details: Record<string, unknown>;
  constructor(status: number, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

export interface ConfirmationNeeded {
  confirmation_required: true;
  message: string;
  confirmation: PendingAction;
}

export function isConfirmation(v: unknown): v is ConfirmationNeeded {
  return typeof v === "object" && v !== null && (v as ConfirmationNeeded).confirmation_required === true;
}

async function request<T>(method: string, path: string, body?: unknown, query?: Record<string, string | number | boolean | undefined>): Promise<T> {
  const url = new URL(path.startsWith("/") ? path : `/${path}`, window.location.origin);
  for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined) url.searchParams.set(k, String(v));
  const resp = await fetch(url.toString(), {
    method,
    headers: { "Content-Type": "application/json", "X-Nayantra-Client": "ui" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!resp.ok) {
    const d = (data ?? {}) as { message?: string; detail?: unknown; details?: Record<string, unknown> };
    const msg = d.message ?? (typeof d.detail === "string" ? d.detail : `HTTP ${resp.status}`);
    throw new ApiError(resp.status, msg, d.details ?? {});
  }
  return data as T;
}

export const api = {
  get: <T>(p: string, q?: Record<string, string | number | boolean | undefined>) => request<T>("GET", `/api/v1${p}`, undefined, q),
  post: <T>(p: string, body?: unknown, q?: Record<string, string | number | boolean | undefined>) => request<T>("POST", `/api/v1${p}`, body ?? {}, q),
  patch: <T>(p: string, body: unknown) => request<T>("PATCH", `/api/v1${p}`, body),
  del: <T>(p: string, q?: Record<string, string | number | boolean | undefined>) => request<T>("DELETE", `/api/v1${p}`, undefined, q),
};

/** Stream the agent's SSE reply to a natural-language command. */
export async function streamAgent(command: string, onEvent: (event: string, data: Record<string, unknown>) => void, signal?: AbortSignal) {
  const resp = await fetch("/api/v1/agent/command", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Nayantra-Client": "ui" },
    body: JSON.stringify({ command }),
    signal,
  });
  if (!resp.ok || !resp.body) throw new ApiError(resp.status, `Agent relay failed (HTTP ${resp.status})`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "message";
      let data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) {
        try {
          onEvent(event, JSON.parse(data));
        } catch {
          onEvent(event, { raw: data });
        }
      }
    }
  }
}
