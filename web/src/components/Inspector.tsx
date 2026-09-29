import { useMemo, useState } from "react";
import {
  BatteryCharging, CircleSlash, Crosshair, Home, Navigation, OctagonX, Pause, Play, PlugZap, RefreshCw, Route, Send, Unlock, X,
} from "lucide-react";
import { api, isConfirmation } from "../api/client";
import type { Robot, Task } from "../api/types";
import { useWorld } from "../store/world";
import { MODE_META, ROBOT_TYPE_LABEL, dur, errMsg, isOpen, pct, titleCase } from "../lib/format";
import { Bar, Battery, ModeBadge, RobotTypeIcon, TaskBadge, vars } from "./ui";

/** Right-hand panel: selected robot → command center; waypoint → details; else overview. */
export function Inspector({ onFocus }: { onFocus?: (x: number, y: number) => void }) {
  const selected = useWorld((s) => s.selectedRobots);
  const selectedWaypoint = useWorld((s) => s.selectedWaypoint);
  if (selected.length === 1) return <RobotPanel robotId={selected[0]} onFocus={onFocus} />;
  if (selected.length > 1) return <MultiPanel ids={selected} />;
  if (selectedWaypoint) return <WaypointPanel id={selectedWaypoint} />;
  return <Overview />;
}

function useAct() {
  const toast = useWorld((s) => s.toast);
  return async (fn: () => Promise<unknown>, ok?: string) => {
    try {
      const r = await fn();
      if (isConfirmation(r)) toast("warning", r.message);
      else if (ok) toast("success", ok);
      return r;
    } catch (e) {
      toast("error", errMsg(e));
      return null;
    }
  };
}

function RobotPanel({ robotId, onFocus }: { robotId: string; onFocus?: (x: number, y: number) => void }) {
  const robot = useWorld((s) => s.robots[robotId]);
  const st = useWorld((s) => s.states[robotId]);
  const fleet = useWorld((s) => (robot ? s.fleets[robot.fleet_id] : undefined));
  const maps = useWorld((s) => s.maps);
  const task = useWorld((s) => (st?.current_task_id ? s.tasks[st.current_task_id] : undefined));
  const goTo = useWorld((s) => s.goTo);
  const setGoTo = useWorld((s) => s.setGoTo);
  const selectRobot = useWorld((s) => s.selectRobot);
  const selectTask = useWorld((s) => s.selectTask);
  const act = useAct();
  const [confirmEstop, setConfirmEstop] = useState(false);
  if (!robot) return null;
  const map = st?.map_id ? maps[st.map_id] : undefined;
  const wpName = (id: string | null | undefined) => (id ? map?.waypoints.find((w) => w.id === id)?.name ?? id : "—");
  const mode = st?.online ? st.mode : "offline";
  const h = st?.health;
  const id = robot.id;

  return (
    <div>
      <div className="insp-head">
        <div className="row between">
          <div className="insp-title">
            <span style={{ color: fleet?.color, display: "inline-flex" }}>
              <RobotTypeIcon type={robot.effective.robot_type} size={17} />
            </span>
            {robot.name}
          </div>
          <div className="row" style={{ gap: 2 }}>
            {st && (
              <button className="btn ghost icon sm" title="Center on map" onClick={() => onFocus?.(st.pose.x, st.pose.y)}>
                <Crosshair size={14} />
              </button>
            )}
            <button className="btn ghost icon sm" onClick={() => selectRobot(null)} aria-label="Close">
              <X size={14} />
            </button>
          </div>
        </div>
        <div className="row" style={{ marginTop: 8, gap: 6 }}>
          <ModeBadge mode={mode} />
          <Battery value={st?.battery_pct ?? null} charging={st?.charging} />
          <span className="faint mono" style={{ marginLeft: "auto" }}>{robot.id}</span>
        </div>
        {st?.status_reason && (
          <div className="reason" style={{ marginTop: 10, ...vars({ "--c": MODE_META[mode].color }) }}>
            {st.status_reason}
          </div>
        )}
      </div>

      <div className="insp-sec">
        <div className="section-title">Operational controls</div>
        <div className="actions">
          <button className={`btn${goTo ? " primary" : ""}`} onClick={() => setGoTo(!goTo)} disabled={!st?.online} title="Then click a waypoint on the map">
            <Navigation size={13} /> {goTo ? "Pick…" : "Go to"}
          </button>
          {robot.enabled && st?.mode === "paused" ? (
            <button className="btn" onClick={() => act(() => api.post(`/robots/${id}/resume`), `${robot.name} resumed`)}>
              <Play size={13} /> Resume
            </button>
          ) : (
            <button className="btn" onClick={() => act(() => api.post(`/robots/${id}/pause`), `${robot.name} paused`)} disabled={!st?.online}>
              <Pause size={13} /> Pause
            </button>
          )}
          <button className="btn" onClick={() => act(() => api.post(`/robots/${id}/stop`, { reason: "Stopped from the robot panel" }), `${robot.name} stopped`)} disabled={!st?.online}>
            <CircleSlash size={13} /> Stop
          </button>
          <button className="btn" onClick={() => act(() => api.post(`/robots/${id}/cancel-task`), "Task cancelled")} disabled={!st?.current_task_id}>
            <X size={13} /> Cancel task
          </button>
          <button className="btn" onClick={() => act(() => api.post(`/robots/${id}/dock`), "Dock task created")} disabled={!robot.effective.capabilities.includes("dock")}>
            <Home size={13} /> Dock
          </button>
          <button className="btn" onClick={() => act(() => api.post(`/robots/${id}/charge`), "Charge task created")} disabled={!robot.effective.capabilities.includes("charge")}>
            <BatteryCharging size={13} /> Charge
          </button>
          <button className="btn" onClick={() => act(() => api.post(`/robots/${id}/restart-navigation`), "Navigation restart requested")}>
            <RefreshCw size={13} /> Restart nav
          </button>
          <button className="btn" onClick={() => act(() => api.post(`/robots/${id}/connect`), "Connection test done")}>
            <PlugZap size={13} /> Test link
          </button>
        </div>
        <div className="section-title" style={{ marginTop: 6 }}>Safety</div>
        {st?.e_stop ? (
          <button className="btn danger" onClick={() => act(() => api.post(`/robots/${id}/release-estop`), "Emergency stop released")}>
            <Unlock size={13} /> Release emergency stop
          </button>
        ) : confirmEstop ? (
          <div className="row">
            <button className="estop grow" style={{ justifyContent: "center" }} onClick={() => { setConfirmEstop(false); void act(() => api.post(`/robots/${id}/estop`, { reason: "Operator emergency stop" }), "EMERGENCY STOP engaged"); }}>
              <OctagonX size={15} /> CONFIRM E-STOP
            </button>
            <button className="btn" onClick={() => setConfirmEstop(false)}>Cancel</button>
          </div>
        ) : (
          <button className="estop" style={{ justifyContent: "center" }} onClick={() => setConfirmEstop(true)}>
            <OctagonX size={15} /> EMERGENCY STOP
          </button>
        )}
      </div>

      <div className="insp-sec">
        <div className="section-title">Navigation</div>
        <div className="kv">
          <span>Destination</span><span>{wpName(st?.destination)}</span>
          <span>At</span><span>{wpName(st?.current_waypoint)}</span>
          <span>ETA</span><span className="num">{st?.eta_s != null ? dur(st.eta_s) : "—"}</span>
          <span>Route</span>
          <span>{st?.route?.length ? st.route.map((r) => wpName(r)).join(" → ") : "—"}</span>
        </div>
        {st?.route_progress != null && <Bar value={st.route_progress} color={fleet?.color} />}
      </div>

      {task && (
        <div className="insp-sec">
          <div className="row between">
            <div className="section-title">Task</div>
            <button className="btn ghost sm" onClick={() => selectTask(task.id)}>Details</button>
          </div>
          <TaskSummary task={task} />
        </div>
      )}

      <div className="insp-sec">
        <div className="section-title">Live state</div>
        <div className="kv">
          <span>Position</span>
          <span className="num mono">
            {st ? `${st.pose.x.toFixed(2)}, ${st.pose.y.toFixed(2)}${st.pose.z > 0.05 ? `, z ${st.pose.z.toFixed(1)}` : ""} · ${((st.pose.yaw * 180) / Math.PI).toFixed(0)}°` : "—"}
          </span>
          <span>Velocity</span><span className="num">{st ? `${st.velocity.linear.toFixed(2)} m/s` : "—"}</span>
          <span>Map</span><span>{map?.name ?? "—"}</span>
          <span>Queue</span><span>{st?.queue.length ? st.queue.join(", ") : "empty"}</span>
          <span>Link</span><span>{st?.connection ?? "—"}{st?.connection_detail ? ` — ${st.connection_detail}` : ""}</span>
        </div>
      </div>

      <div className="insp-sec">
        <div className="section-title">Health</div>
        <div className="kv">
          <span>Localization</span><span>{h?.localization ?? "not reported"}</span>
          <span>Navigation</span><span>{h?.navigation ?? "not reported"}</span>
          <span>Network</span><span>{h?.network ?? "not reported"}{h?.latency_ms != null ? ` · ${h.latency_ms} ms` : ""}</span>
          <span>CPU / GPU</span><span>{h?.cpu_pct != null ? pct(h.cpu_pct) : "not reported"}{h?.gpu_pct != null ? ` / ${pct(h.gpu_pct)}` : ""}</span>
          <span>Temperature</span><span>{h?.temperature_c != null ? `${h.temperature_c.toFixed(0)} °C` : "not reported"}</span>
          <span>Sensors</span>
          <span>{Object.keys(h?.sensors ?? {}).length ? Object.entries(h!.sensors).map(([k, v]) => `${k}: ${v}`).join(", ") : (robot.navigation.sensors.join(", ") || "not reported")}</span>
        </div>
      </div>

      <div className="insp-sec">
        <div className="section-title">Identity</div>
        <div className="kv">
          <span>Fleet</span><span>{fleet?.name ?? robot.fleet_id}</span>
          <span>Type</span><span>{ROBOT_TYPE_LABEL[robot.effective.robot_type]}</span>
          <span>Model</span><span>{[robot.manufacturer, robot.model].filter(Boolean).join(" ") || "—"}</span>
          <span>Adapter</span><span>{st?.adapter ?? "—"} ({robot.effective.communication.protocol})</span>
          <span>Stack</span><span>{robot.effective.nav_stack} · {robot.effective.localization}</span>
          <span>Capabilities</span><span>{robot.effective.capabilities.map(titleCase).join(", ")}</span>
          <span>Max speed</span><span className="num">{robot.effective.max_speed} m/s</span>
        </div>
      </div>
    </div>
  );
}

export function TaskSummary({ task }: { task: Task }) {
  return (
    <div className="col" style={{ gap: 6 }}>
      <div className="row between">
        <span className="mono">{task.id} · {task.type}</span>
        <TaskBadge status={task.status} />
      </div>
      <Bar value={task.progress} />
      <div style={{ fontSize: 12.5 }}>
        {task.steps.map((s, i) => (
          <div key={i} className="row" style={{ gap: 6, opacity: s.status === "done" ? 0.55 : 1, fontWeight: i === task.current_step && isOpen(task.status) ? 600 : 400 }}>
            <span style={{ width: 12, color: s.status === "done" ? "var(--ok)" : i === task.current_step ? "var(--accent)" : "var(--text-3)" }}>
              {s.status === "done" ? "✓" : i === task.current_step && isOpen(task.status) ? "▸" : "·"}
            </span>
            <span className="ellipsis">{s.label}</span>
          </div>
        ))}
      </div>
      {task.status_reason && <div className="faint" style={{ fontSize: 12 }}>{task.status_reason}</div>}
    </div>
  );
}

function MultiPanel({ ids }: { ids: string[] }) {
  const robots = useWorld((s) => s.robots);
  const states = useWorld((s) => s.states);
  const openComposer = useWorld((s) => s.openComposer);
  const selectRobot = useWorld((s) => s.selectRobot);
  const goTo = useWorld((s) => s.goTo);
  const setGoTo = useWorld((s) => s.setGoTo);
  const act = useAct();
  return (
    <div>
      <div className="insp-head">
        <div className="row between">
          <div className="insp-title">{ids.length} robots selected</div>
          <button className="btn ghost icon sm" onClick={() => selectRobot(null)} aria-label="Clear selection"><X size={14} /></button>
        </div>
      </div>
      <div className="insp-sec">
        <div className="actions" style={{ gridTemplateColumns: "1fr 1fr" }}>
          <button className="btn primary" onClick={() => openComposer({ robotIds: ids })}><Send size={13} /> Dispatch selected</button>
          <button className={`btn${goTo ? " primary" : ""}`} onClick={() => setGoTo(!goTo)}><Navigation size={13} /> {goTo ? "Pick waypoint…" : "All go to…"}</button>
          <button className="btn" onClick={() => Promise.all(ids.map((i) => act(() => api.post(`/robots/${i}/pause`))))}><Pause size={13} /> Pause all</button>
          <button className="btn" onClick={() => Promise.all(ids.map((i) => act(() => api.post(`/robots/${i}/stop`))))}><CircleSlash size={13} /> Stop all</button>
        </div>
      </div>
      <div className="insp-sec">
        {ids.map((id) => {
          const st = states[id];
          return (
            <div key={id} className="row between" style={{ padding: "3px 0" }}>
              <span className="row" style={{ gap: 6 }}><ModeBadge mode={st?.online ? st.mode : "offline"} /> {robots[id]?.name}</span>
              <Battery value={st?.battery_pct ?? null} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function WaypointPanel({ id }: { id: string }) {
  const maps = useWorld((s) => s.maps);
  const activeMapId = useWorld((s) => s.activeMapId);
  const selectWaypoint = useWorld((s) => s.selectWaypoint);
  const robots = useWorld((s) => s.robots);
  const states = useWorld((s) => s.states);
  const openComposer = useWorld((s) => s.openComposer);
  const act = useAct();
  const map = activeMapId ? maps[activeMapId] : undefined;
  const w = map?.waypoints.find((x) => x.id === id);
  const candidates = useMemo(
    () =>
      Object.values(robots)
        .filter((r: Robot) => r.effective.map_id === map?.id && r.effective.layer === w?.layer && states[r.id]?.online)
        .map((r) => ({ r, d: states[r.id] ? Math.hypot(states[r.id].pose.x - (w?.x ?? 0), states[r.id].pose.y - (w?.y ?? 0)) : 1e9 }))
        .sort((a, b) => a.d - b.d)
        .slice(0, 5),
    [robots, states, map?.id, w],
  );
  if (!w || !map) return <Overview />;
  const lanes = map.lanes.filter((l) => l.from_id === w.id || l.to_id === w.id);
  return (
    <div>
      <div className="insp-head">
        <div className="row between">
          <div className="insp-title"><Route size={16} style={{ color: "var(--accent)" }} />{w.name}</div>
          <button className="btn ghost icon sm" onClick={() => selectWaypoint(null)} aria-label="Close"><X size={14} /></button>
        </div>
        <div className="row" style={{ marginTop: 6, gap: 6 }}>
          <span className="badge">{titleCase(w.type)}</span>
          <span className="badge">{w.layer}</span>
          <span className="faint mono" style={{ marginLeft: "auto" }}>{w.id}</span>
        </div>
      </div>
      <div className="insp-sec">
        <button className="btn primary" onClick={() => openComposer({ type: "navigate", destination: w.id })}>
          <Send size={13} /> Create task to here…
        </button>
        <div className="section-title" style={{ marginTop: 4 }}>Send a specific robot</div>
        {candidates.length === 0 && <div className="faint">No online robot on this layer.</div>}
        {candidates.map(({ r, d }) => (
          <div key={r.id} className="row between">
            <span className="row" style={{ gap: 6 }}><RobotTypeIcon type={r.effective.robot_type} size={12} /> {r.name} <span className="faint num">{d.toFixed(1)} m</span></span>
            <button className="btn sm" onClick={() => act(() => api.post(`/robots/${r.id}/navigate`, { waypoint: w.id }), `${r.name} → ${w.name}`)}>Send</button>
          </div>
        ))}
      </div>
      <div className="insp-sec">
        <div className="kv">
          <span>Position</span><span className="mono num">{w.x.toFixed(2)}, {w.y.toFixed(2)}{w.z ? `, z ${w.z}` : ""}</span>
          <span>Heading</span><span className="num">{((w.yaw * 180) / Math.PI).toFixed(0)}°</span>
          <span>Aliases</span><span>{w.aliases.join(", ") || "—"}</span>
          <span>Lanes</span><span>{lanes.length ? lanes.map((l) => l.id).join(", ") : "none (not routable)"}</span>
        </div>
      </div>
    </div>
  );
}

function Overview() {
  const fleets = useWorld((s) => s.fleets);
  const robots = useWorld((s) => s.robots);
  const states = useWorld((s) => s.states);
  const tasks = useWorld((s) => s.tasks);
  const traffic = useWorld((s) => s.traffic);
  const selectTask = useWorld((s) => s.selectTask);
  const openComposer = useWorld((s) => s.openComposer);
  const all = Object.values(states);
  const online = all.filter((s) => s.online).length;
  const busy = all.filter((s) => s.current_task_id).length;
  const open = Object.values(tasks).filter((t) => isOpen(t.status)).sort((a, b) => b.created_at - a.created_at);
  return (
    <div>
      <div className="insp-head">
        <div className="insp-title">Operations</div>
        <div className="faint" style={{ marginTop: 2 }}>Select a robot or a waypoint on the map · <span className="kbd">/</span> to command</div>
      </div>
      <div className="insp-sec">
        <div className="stat-row">
          <div><div className="v">{Object.keys(robots).length}</div><div className="k">Robots</div></div>
          <div><div className="v">{online}</div><div className="k">Online</div></div>
          <div><div className="v">{busy}</div><div className="k">Busy</div></div>
          <div><div className="v" style={{ color: traffic.waits.length ? "var(--warn)" : undefined }}>{traffic.waits.length}</div><div className="k">Waiting</div></div>
        </div>
        <button className="btn primary" onClick={() => openComposer()}><Send size={13} /> New task</button>
      </div>
      <div className="insp-sec">
        <div className="section-title">Fleets</div>
        {Object.values(fleets).map((f) => {
          const ids = Object.values(robots).filter((r) => r.fleet_id === f.id).map((r) => r.id);
          const on = ids.filter((i) => states[i]?.online).length;
          const b = ids.filter((i) => states[i]?.current_task_id).length;
          return (
            <div key={f.id} className="col" style={{ gap: 4 }}>
              <div className="row between">
                <span className="row" style={{ gap: 7 }}><span className="fleet-swatch" style={vars({ "--c": f.color })} />{f.name}</span>
                <span className="faint num">{b}/{on} busy</span>
              </div>
              <Bar value={on ? b / on : 0} color={f.color} />
            </div>
          );
        })}
      </div>
      <div className="insp-sec">
        <div className="section-title">Active tasks ({open.length})</div>
        {open.length === 0 && <div className="faint">No open tasks.</div>}
        {open.slice(0, 8).map((t) => (
          <div key={t.id} className="row between" style={{ cursor: "pointer" }} onClick={() => selectTask(t.id)}>
            <span className="ellipsis"><span className="mono">{t.id}</span> {t.type} {t.assigned_robot ? `· ${robots[t.assigned_robot]?.name ?? t.assigned_robot}` : ""}</span>
            <TaskBadge status={t.status} />
          </div>
        ))}
      </div>
    </div>
  );
}
