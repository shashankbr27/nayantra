import type { RobotMode, Severity, TaskStatus } from "../api/types";

export const MODE_META: Record<RobotMode, { label: string; color: string; pulse?: boolean }> = {
  moving: { label: "Moving", color: "#38bdf8" },
  idle: { label: "Idle", color: "#7c8ea0" },
  waiting: { label: "Waiting · traffic", color: "#fbbf24", pulse: true },
  paused: { label: "Paused", color: "#a78bfa" },
  charging: { label: "Charging", color: "#34d399", pulse: true },
  acting: { label: "Working", color: "#2dd4bf" },
  error: { label: "Error", color: "#f87171" },
  emergency_stop: { label: "E-STOP", color: "#ef4444", pulse: true },
  offline: { label: "Offline", color: "#4b5b6b" },
};

export const TASK_META: Record<TaskStatus, { label: string; color: string }> = {
  queued: { label: "Queued", color: "#7c8ea0" },
  assigned: { label: "Assigned", color: "#60a5fa" },
  planning: { label: "Planning", color: "#60a5fa" },
  executing: { label: "Executing", color: "#38bdf8" },
  paused: { label: "Paused", color: "#a78bfa" },
  waiting_for_traffic: { label: "Waiting · traffic", color: "#fbbf24" },
  waiting_for_robot: { label: "Waiting · robot", color: "#f59e0b" },
  completed: { label: "Completed", color: "#34d399" },
  failed: { label: "Failed", color: "#f87171" },
  cancelled: { label: "Cancelled", color: "#64748b" },
};

export const SEVERITY_COLOR: Record<Severity, string> = {
  info: "#60a5fa",
  warning: "#fbbf24",
  error: "#f87171",
  critical: "#ef4444",
};

export const TERMINAL: TaskStatus[] = ["completed", "failed", "cancelled"];
export const isOpen = (s: TaskStatus) => !TERMINAL.includes(s);

export const ROBOT_TYPE_LABEL: Record<string, string> = {
  ugv: "UGV",
  uav: "UAV",
  quadruped: "Quadruped",
  humanoid: "Humanoid",
  other: "Other",
};

export function clock(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function ago(ts: number | null | undefined): string {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

export function dur(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "—";
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${Math.round(s % 60)} s`;
}

export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${Math.round(v)}%`;
}

export function batteryColor(v: number | null | undefined): string {
  if (v === null || v === undefined) return "#4b5b6b";
  if (v < 20) return "#f87171";
  if (v < 40) return "#fbbf24";
  return "#34d399";
}

export function titleCase(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function slug(s: string): string {
  return s.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64);
}

export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}
