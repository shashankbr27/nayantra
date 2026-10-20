import { useEffect, useState } from "react";
import { Pause, Play, X } from "lucide-react";
import { api } from "../api/client";
import type { NEvent, Task } from "../api/types";
import { useWorld } from "../store/world";
import { SEVERITY_COLOR, TASK_META, clock, dur, errMsg, isOpen } from "../lib/format";
import { Bar, TaskBadge, vars } from "./ui";

export function TaskDrawer() {
  const id = useWorld((s) => s.selectedTask);
  const task = useWorld((s) => (id ? s.tasks[id] : undefined));
  const robots = useWorld((s) => s.robots);
  const fleets = useWorld((s) => s.fleets);
  const maps = useWorld((s) => s.maps);
  const selectTask = useWorld((s) => s.selectTask);
  const selectRobot = useWorld((s) => s.selectRobot);
  const setPage = useWorld((s) => s.setPage);
  const toast = useWorld((s) => s.toast);
  const [events, setEvents] = useState<NEvent[]>([]);
  const [loaded, setLoaded] = useState<Task | null>(null);

  useEffect(() => {
    if (!id) return;
    let alive = true;
    const load = () =>
      api.get<{ task: Task; events: NEvent[] }>(`/tasks/${id}/trace`).then((r) => {
        if (!alive) return;
        setEvents(r.events);
        setLoaded(r.task);
      }).catch(() => undefined);
    void load();
    const t = window.setInterval(load, 3000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [id]);

  const t = task ?? loaded;
  if (!id || !t) return null;
  const name = (rid: string | null) => (rid ? robots[rid]?.name ?? rid : "—");
  const wpName = (w: string | null) => {
    if (!w) return "—";
    for (const m of Object.values(maps)) {
      const f = m.waypoints.find((x) => x.id === w);
      if (f) return f.name;
    }
    return w;
  };
  const act = (verb: "pause" | "resume" | "cancel") =>
    api.post(`/tasks/${t.id}/${verb}`).then(() => toast("success", `${t.id} ${verb}d`)).catch((e) => toast("error", errMsg(e)));
  const alloc = t.allocation;

  return (
    <div className="drawer">
      <div className="insp-head">
        <div className="row between">
          <div className="insp-title"><span className="mono">{t.id}</span> {t.type}</div>
          <button className="btn ghost icon sm" onClick={() => selectTask(null)} aria-label="Close"><X size={15} /></button>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <TaskBadge status={t.status} />
          <span className="badge">priority {t.priority}</span>
          <span className="faint" style={{ marginLeft: "auto" }}>created {clock(t.created_at)} by {t.created_by}</span>
        </div>
        <div style={{ marginTop: 10 }}><Bar value={t.progress} color={TASK_META[t.status].color} /></div>
        {isOpen(t.status) && (
          <div className="row" style={{ marginTop: 10 }}>
            {t.status === "paused" ? (
              <button className="btn" onClick={() => act("resume")}><Play size={13} /> Resume</button>
            ) : (
              <button className="btn" onClick={() => act("pause")}><Pause size={13} /> Pause</button>
            )}
            <button className="btn danger" onClick={() => act("cancel")}><X size={13} /> Cancel task</button>
          </div>
        )}
      </div>
      <div style={{ overflow: "auto", flex: 1 }}>
        <div className="insp-sec">
          <div className="section-title">Why is it {TASK_META[t.status].label.toLowerCase()}?</div>
          <div className="reason" style={vars({ "--c": TASK_META[t.status].color })}>{t.status_reason || "—"}</div>
        </div>

        <div className="insp-sec">
          <div className="section-title">Steps</div>
          <div className="timeline">
            {t.steps.map((s, i) => {
              const c = s.status === "done" ? "#34d399" : s.status === "failed" ? "#f87171" : i === t.current_step && isOpen(t.status) ? "#38bdf8" : undefined;
              return (
                <div key={i} className="tl-item" style={c ? vars({ "--c": c }) : undefined}>
                  <div style={{ fontWeight: i === t.current_step ? 600 : 400 }}>{s.label}</div>
                  <div className="faint" style={{ fontSize: 12 }}>
                    {s.kind === "go_to" && (s.target ? `→ ${wpName(s.target)}` : `one of ${s.candidates.length} spots, chosen on arrival`)}
                    {s.detail ? ` · route ${s.detail}` : ""}
                    {s.started_at && s.finished_at ? ` · ${dur(s.finished_at - s.started_at)}` : ""}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="insp-sec">
          <div className="section-title">Why this robot?</div>
          {alloc ? (
            <>
              <div className="reason" style={vars({ "--c": alloc.selected ? "#34d399" : "#fbbf24" })}>{alloc.explanation}</div>
              <table className="tbl" style={{ marginTop: 4 }}>
                <thead><tr><th>Robot</th><th>Cost</th><th>Details</th></tr></thead>
                <tbody>
                  {[...alloc.candidates]
                    .sort((a, b) => Number(b.eligible) - Number(a.eligible) || (a.score ?? 1e9) - (b.score ?? 1e9))
                    .map((c) => (
                      <tr key={c.robot_id} className="click" onClick={() => { setPage("operations"); selectRobot(c.robot_id); selectTask(null); }}>
                        <td>
                          <span className="row" style={{ gap: 6 }}>
                            <span className="fleet-swatch" style={vars({ "--c": fleets[robots[c.robot_id]?.fleet_id]?.color ?? "#888" })} />
                            {name(c.robot_id)} {alloc.selected === c.robot_id && <span className="badge" style={vars({ "--c": "#34d399" })}>selected</span>}
                          </span>
                        </td>
                        <td className="num">{c.eligible && c.score != null ? `${c.score.toFixed(0)} s` : "—"}</td>
                        <td className="muted" style={{ fontSize: 12 }}>
                          {c.eligible
                            ? `≈${Number(c.estimates.to_start_s ?? 0).toFixed(0)} s to start · busy ${Number(c.estimates.busy_s ?? 0).toFixed(0)} s · battery ${c.estimates.battery_pct != null ? Math.round(Number(c.estimates.battery_pct)) + "%" : "—"}`
                            : c.reasons.join("; ")}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </>
          ) : (
            <div className="faint">Not allocated yet.</div>
          )}
        </div>

        <div className="insp-sec">
          <div className="section-title">Origin</div>
          <div className="kv">
            <span>Source</span><span>{t.trace.source}{t.trace.source === "llm" ? " (AI agent via MCP)" : ""}</span>
            {t.trace.command && (<><span>Command</span><span>“{t.trace.command}”</span></>)}
            {t.trace.tool && (<><span>Tool</span><span className="mono">{t.trace.tool}</span></>)}
            {t.trace.mission_id && (<><span>Mission</span><span className="mono">{t.trace.mission_id}</span></>)}
            {t.trace.reason && (<><span>Reason</span><span>{t.trace.reason}</span></>)}
            <span>Assigned</span><span>{name(t.assigned_robot)}{t.assigned_fleet ? ` · ${fleets[t.assigned_fleet]?.name ?? t.assigned_fleet}` : ""}</span>
            <span>Timing</span>
            <span className="num">
              {t.started_at ? `started ${clock(t.started_at)}` : "not started"}
              {t.completed_at ? ` · finished ${clock(t.completed_at)} (${dur(t.completed_at - (t.started_at ?? t.created_at))})` : ""}
            </span>
          </div>
        </div>

        <div className="insp-sec">
          <div className="section-title">Trace</div>
          <div className="timeline">
            {events.map((e) => (
              <div key={e.id} className="tl-item" style={vars({ "--c": SEVERITY_COLOR[e.severity] })}>
                <div style={{ fontSize: 12.5 }}>{e.message}</div>
                <div className="faint mono" style={{ fontSize: 11 }}>{clock(e.ts)} · {e.type}</div>
              </div>
            ))}
            {t.history
              .filter((h) => !h.status)
              .map((h, i) => (
                <div key={`h${i}`} className="tl-item">
                  <div style={{ fontSize: 12.5 }}>{h.message}</div>
                  <div className="faint mono" style={{ fontSize: 11 }}>{clock(h.ts)} · note</div>
                </div>
              ))}
          </div>
        </div>
      </div>
    </div>
  );
}
