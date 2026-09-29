import { useEffect, type CSSProperties, type ReactNode } from "react";
import { Bot, Dog, Plane, Truck, User, X } from "lucide-react";
import type { RobotMode, RobotType, TaskStatus } from "../api/types";
import { MODE_META, TASK_META, batteryColor } from "../lib/format";

type Vars = CSSProperties & Record<`--${string}`, string | number>;

export function ModeBadge({ mode }: { mode: RobotMode }) {
  const m = MODE_META[mode] ?? MODE_META.offline;
  return (
    <span className="badge" style={{ "--c": m.color } as Vars}>
      <span className={`dot${m.pulse ? " pulse" : ""}`} style={{ "--c": m.color } as Vars} />
      {m.label}
    </span>
  );
}

export function StatusDot({ mode, title }: { mode: RobotMode; title?: string }) {
  const m = MODE_META[mode] ?? MODE_META.offline;
  return <span title={title ?? m.label} className={`dot${m.pulse ? " pulse" : ""}`} style={{ "--c": m.color } as Vars} />;
}

export function TaskBadge({ status }: { status: TaskStatus }) {
  const m = TASK_META[status] ?? TASK_META.queued;
  return (
    <span className="badge" style={{ "--c": m.color } as Vars}>
      {m.label}
    </span>
  );
}

export function Battery({ value, charging }: { value: number | null; charging?: boolean }) {
  if (value === null || value === undefined) return <span className="batt faint" title="battery not reported">—</span>;
  return (
    <span className="batt" title={`${value.toFixed(1)}%${charging ? " · charging" : ""}`}>
      <i style={{ "--p": Math.max(0, Math.min(1, value / 100)), "--c": batteryColor(value) } as Vars} />
      {Math.round(value)}%{charging ? "⚡" : ""}
    </span>
  );
}

export function RobotTypeIcon({ type, size = 14 }: { type: RobotType | string | null | undefined; size?: number }) {
  switch (type) {
    case "uav":
      return <Plane size={size} />;
    case "quadruped":
      return <Dog size={size} />;
    case "humanoid":
      return <User size={size} />;
    case "ugv":
      return <Truck size={size} />;
    default:
      return <Bot size={size} />;
  }
}

export function Bar({ value, color }: { value: number; color?: string }) {
  return (
    <div className="bar">
      <i style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`, "--c": color ?? "var(--accent)" } as Vars} />
    </div>
  );
}

export function Modal({
  title, onClose, children, footer, size,
}: { title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; size?: "sm" }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${size ?? ""}`} role="dialog" aria-modal="true">
        <div className="modal-head">
          <div className="modal-title">{title}</div>
          <button className="btn ghost icon" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function vars(v: Record<string, string | number>): Vars {
  return v as Vars;
}
