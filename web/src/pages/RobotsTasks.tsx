import { useMemo, useState } from "react";
import { MapPin, Plus, Power, Trash2 } from "lucide-react";
import { api } from "../api/client";
import type { TaskStatus } from "../api/types";
import { useWorld } from "../store/world";
import { ROBOT_TYPE_LABEL, TASK_META, clock, errMsg, isOpen } from "../lib/format";
import { Bar, Battery, Empty, ModeBadge, RobotTypeIcon, TaskBadge, vars } from "../components/ui";

export function RobotsPage() {
  const robots = useWorld((s) => s.robots);
  const fleets = useWorld((s) => s.fleets);
  const states = useWorld((s) => s.states);
  const openWizard = useWorld((s) => s.openWizard);
  const toast = useWorld((s) => s.toast);
  const [q, setQ] = useState("");
  const list = Object.values(robots)
    .filter((r) => !q || `${r.name} ${r.id} ${r.fleet_id} ${r.model}`.toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => a.fleet_id.localeCompare(b.fleet_id) || a.name.localeCompare(b.name));
  const show = (id: string, mapId: string | null) => {
    const s = useWorld.getState();
    if (mapId) s.setActiveMap(mapId);
    s.setPage("operations");
    s.selectRobot(id);
  };
  return (
    <div className="page">
      <div className="row between" style={{ marginBottom: 14 }}>
        <h1>Robots <span className="faint" style={{ fontWeight: 400, fontSize: 14 }}>{list.length}</span></h1>
        <div className="row">
          <input className="input" style={{ width: 240 }} placeholder="Filter…" value={q} onChange={(e) => setQ(e.target.value)} />
          <button className="btn primary" onClick={() => openWizard(true)}><Plus size={14} /> Register robot</button>
        </div>
      </div>
      <div className="panel">
        <table className="tbl">
          <thead>
            <tr><th>Robot</th><th>Fleet</th><th>Model</th><th>Link</th><th>Status</th><th>Battery</th><th>Capabilities</th><th /></tr>
          </thead>
          <tbody>
            {list.map((r) => {
              const st = states[r.id];
              const f = fleets[r.fleet_id];
              const failed = r.checks.find((c) => c.required && !c.ok);
              return (
                <tr key={r.id} className="click" onClick={() => show(r.id, r.effective.map_id)}>
                  <td>
                    <span className="row" style={{ gap: 7 }}>
                      <span style={{ color: f?.color, display: "inline-flex" }}><RobotTypeIcon type={r.effective.robot_type} size={14} /></span>
                      <b>{r.name}</b> <span className="faint mono">{r.id}</span>
                    </span>
                  </td>
                  <td>{f?.name ?? r.fleet_id}</td>
                  <td className="muted">{ROBOT_TYPE_LABEL[r.effective.robot_type]} · {[r.manufacturer, r.model].filter(Boolean).join(" ") || "—"}</td>
                  <td>
                    <span className="badge" style={vars({ "--c": st?.online ? "#34d399" : failed ? "#f87171" : "#7c8ea0" })} title={failed?.detail ?? st?.connection_detail}>
                      {r.effective.communication.protocol}{st?.online ? "" : failed ? " · failed" : " · offline"}
                    </span>
                  </td>
                  <td>{r.enabled ? <ModeBadge mode={st?.online ? st.mode : "offline"} /> : <span className="badge">decommissioned</span>}</td>
                  <td><Battery value={st?.battery_pct ?? null} charging={st?.charging} /></td>
                  <td className="muted ellipsis" style={{ maxWidth: 240 }}>{r.effective.capabilities.join(", ")}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                    <button className="btn ghost sm icon" title="Show on map" onClick={() => show(r.id, r.effective.map_id)}><MapPin size={13} /></button>
                    <button
                      className="btn ghost sm icon"
                      title={r.enabled ? "Decommission (take out of service)" : "Recommission"}
                      onClick={() => api.patch(`/robots/${r.id}`, { enabled: !r.enabled }).catch((e) => toast("error", errMsg(e)))}
                    >
                      <Power size={13} />
                    </button>
                    <button className="btn ghost sm icon" title="Remove robot (asks for confirmation)" onClick={() => api.del(`/robots/${r.id}`).catch((e) => toast("error", errMsg(e)))}>
                      <Trash2 size={13} />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {list.length === 0 && <Empty>No robots registered yet.</Empty>}
      </div>
    </div>
  );
}

const FILTERS: { id: string; label: string; test: (s: TaskStatus) => boolean }[] = [
  { id: "open", label: "Open", test: (s) => isOpen(s) },
  { id: "waiting", label: "Waiting", test: (s) => s === "waiting_for_traffic" || s === "waiting_for_robot" },
  { id: "done", label: "Completed", test: (s) => s === "completed" },
  { id: "problem", label: "Failed / cancelled", test: (s) => s === "failed" || s === "cancelled" },
  { id: "all", label: "All", test: () => true },
];

export function TasksPage() {
  const tasks = useWorld((s) => s.tasks);
  const robots = useWorld((s) => s.robots);
  const fleets = useWorld((s) => s.fleets);
  const selectTask = useWorld((s) => s.selectTask);
  const openComposer = useWorld((s) => s.openComposer);
  const [filter, setFilter] = useState("open");
  const f = FILTERS.find((x) => x.id === filter)!;
  const list = useMemo(() => Object.values(tasks).filter((t) => f.test(t.status)).sort((a, b) => b.created_at - a.created_at), [tasks, f]);
  return (
    <div className="page">
      <div className="row between" style={{ marginBottom: 14 }}>
        <h1>Tasks</h1>
        <div className="row">
          <div className="chips">
            {FILTERS.map((x) => (
              <button key={x.id} className={`chip${filter === x.id ? " on" : ""}`} onClick={() => setFilter(x.id)}>
                {x.label} <span className="faint">{Object.values(tasks).filter((t) => x.test(t.status)).length}</span>
              </button>
            ))}
          </div>
          <button className="btn primary" onClick={() => openComposer()}><Plus size={14} /> New task</button>
        </div>
      </div>
      <div className="panel">
        <table className="tbl">
          <thead>
            <tr><th>ID</th><th>Type</th><th>Created by</th><th>Fleet</th><th>Robot</th><th>Priority</th><th>Status</th><th>Created</th><th>Started</th><th>Completed</th><th>Current step</th><th style={{ width: 120 }}>Progress</th></tr>
          </thead>
          <tbody>
            {list.map((t) => (
              <tr key={t.id} className="click" onClick={() => selectTask(t.id)} title={t.status_reason}>
                <td className="mono">{t.id}</td>
                <td>{t.type}</td>
                <td>{t.created_by}{t.trace.source === "llm" ? " (AI)" : ""}</td>
                <td>{t.assigned_fleet ? fleets[t.assigned_fleet]?.name ?? t.assigned_fleet : "—"}</td>
                <td>{t.assigned_robot ? robots[t.assigned_robot]?.name ?? t.assigned_robot : "—"}</td>
                <td className="num">{t.priority}</td>
                <td><TaskBadge status={t.status} /></td>
                <td className="num faint">{clock(t.created_at)}</td>
                <td className="num faint">{t.started_at ? clock(t.started_at) : "—"}</td>
                <td className="num faint">{t.completed_at ? clock(t.completed_at) : "—"}</td>
                <td className="ellipsis" style={{ maxWidth: 200 }}>{t.steps[t.current_step]?.label ?? "—"}</td>
                <td><Bar value={t.progress} color={TASK_META[t.status].color} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {list.length === 0 && <Empty>No tasks match.</Empty>}
      </div>
    </div>
  );
}
