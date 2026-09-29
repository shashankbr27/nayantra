import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Pause, Play, Plus, RotateCcw, Square } from "lucide-react";
import { api } from "../api/client";
import type { RobotState } from "../api/types";
import { useWorld } from "../store/world";
import { MODE_META, errMsg } from "../lib/format";
import { Battery, RobotTypeIcon, StatusDot, vars } from "./ui";

export function FleetSidebar() {
  const fleets = useWorld((s) => s.fleets);
  const robots = useWorld((s) => s.robots);
  const states = useWorld((s) => s.states);
  const selected = useWorld((s) => s.selectedRobots);
  const selectRobot = useWorld((s) => s.selectRobot);
  const setSelected = useWorld((s) => s.setSelectedRobots);
  const openWizard = useWorld((s) => s.openWizard);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const grouped = useMemo(() => {
    const byFleet: Record<string, string[]> = {};
    for (const r of Object.values(robots)) (byFleet[r.fleet_id] ??= []).push(r.id);
    for (const ids of Object.values(byFleet)) ids.sort((a, b) => robots[a].name.localeCompare(robots[b].name));
    return byFleet;
  }, [robots]);

  return (
    <aside className="side">
      <div className="side-head">
        <span className="section-title">Fleets</span>
        <button className="btn sm" onClick={() => openWizard(true)} title="Register a robot">
          <Plus size={13} /> Robot
        </button>
      </div>
      <div className="side-scroll">
        {Object.values(fleets).map((f) => {
          const ids = grouped[f.id] ?? [];
          const online = ids.filter((id) => states[id]?.online).length;
          const busy = ids.filter((id) => states[id]?.current_task_id).length;
          const allSel = ids.length > 0 && ids.every((id) => selected.includes(id));
          const open = !collapsed[f.id];
          return (
            <div key={f.id} className="fleet">
              <div className="fleet-head" onClick={() => setCollapsed({ ...collapsed, [f.id]: open })}>
                {open ? <ChevronDown size={14} className="faint" /> : <ChevronRight size={14} className="faint" />}
                <span className="fleet-swatch" style={vars({ "--c": f.color })} />
                <div className="grow">
                  <div className="fleet-name ellipsis">{f.name}</div>
                  <div className="fleet-meta">
                    {ids.length} robots · {online} online · {busy} busy
                  </div>
                </div>
                <input
                  type="checkbox"
                  className="cbx"
                  title="Select the whole fleet"
                  checked={allSel}
                  onClick={(e) => e.stopPropagation()}
                  onChange={() =>
                    setSelected(allSel ? selected.filter((r) => !ids.includes(r)) : [...new Set([...selected, ...ids])])
                  }
                />
              </div>
              {open &&
                ids.map((id) => {
                  const r = robots[id];
                  const st: RobotState | undefined = states[id];
                  const mode = st?.online ? st.mode : "offline";
                  const sel = selected.includes(id);
                  return (
                    <div
                      key={id}
                      className={`robot-row${sel ? " sel" : ""}`}
                      onClick={(e) => selectRobot(id, e.shiftKey || e.ctrlKey || e.metaKey)}
                      title={st?.status_reason || MODE_META[mode].label}
                    >
                      <StatusDot mode={mode} />
                      <div style={{ minWidth: 0 }}>
                        <div className="rname row" style={{ gap: 6 }}>
                          <span style={{ color: f.color, display: "inline-flex" }}>
                            <RobotTypeIcon type={r.effective.robot_type} size={12} />
                          </span>
                          <span className="ellipsis">{r.name}</span>
                        </div>
                        <div className="rsub ellipsis">
                          {st?.current_task_id ? `${st.current_task_id} · ` : ""}
                          {st?.status_reason || MODE_META[mode].label}
                        </div>
                      </div>
                      <Battery value={st?.battery_pct ?? null} charging={st?.charging} />
                    </div>
                  );
                })}
            </div>
          );
        })}
        {Object.keys(fleets).length === 0 && <div className="empty">No fleets yet. Register a robot to create one.</div>}
      </div>
      <SimControls />
    </aside>
  );
}

function SimControls() {
  const sim = useWorld((s) => s.sim);
  const toast = useWorld((s) => s.toast);
  if (!sim) return null;
  const act = async (path: string, label: string) => {
    try {
      await api.post(path);
      toast("info", label);
    } catch (e) {
      toast("error", errMsg(e));
    }
  };
  return (
    <div className="sim-box">
      <div className="row between">
        <span className="section-title">Simulation</span>
        <span className="badge" style={vars({ "--c": sim.running ? "#34d399" : "#fbbf24" })}>
          <span className={`dot${sim.running ? " pulse" : ""}`} style={vars({ "--c": sim.running ? "#34d399" : "#fbbf24" })} />
          {sim.running ? "Running" : "Paused"}
        </span>
      </div>
      <div className="faint" style={{ fontSize: 11.5 }}>
        {sim.environment} · {sim.robots.length} simulated robots · t={Math.round(sim.sim_time_s)} s
      </div>
      <div className="row">
        {sim.running ? (
          <button className="btn sm" onClick={() => act("/sim/pause", "Simulation paused")} title="Pause">
            <Pause size={13} /> Pause
          </button>
        ) : (
          <button className="btn sm" onClick={() => act("/sim/start", "Simulation running")} title="Start">
            <Play size={13} /> Start
          </button>
        )}
        <button className="btn sm" onClick={() => act("/sim/stop", "Simulation stopped and reset")} title="Stop: cancel sim tasks, return robots to spawn, pause">
          <Square size={12} /> Stop
        </button>
        <button className="btn sm icon" onClick={() => act("/sim/reset", "Robots returned to spawn")} title="Reset robots to spawn and keep running">
          <RotateCcw size={13} />
        </button>
        <select
          className="input"
          style={{ height: 24, width: 64, padding: "0 4px" }}
          value={sim.speed}
          onChange={(e) => api.patch("/sim", { speed: Number(e.target.value) }).catch((err) => toast("error", errMsg(err)))}
          title="Simulation speed"
        >
          {[0.5, 1, 2, 4, 8].map((v) => (
            <option key={v} value={v}>
              {v}×
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
