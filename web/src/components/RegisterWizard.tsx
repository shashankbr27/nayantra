import { useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, CheckCircle2, CircleAlert, CircleDashed, MapPin, Plus } from "lucide-react";
import { api } from "../api/client";
import type { Check, Robot, RobotType } from "../api/types";
import { useWorld } from "../store/world";
import { useMeta } from "../lib/meta";
import { ROBOT_TYPE_LABEL, errMsg, slug, titleCase } from "../lib/format";
import { Field, Modal, RobotTypeIcon, vars } from "./ui";

const STEPS = ["Identity", "Fleet", "Capabilities", "Navigation", "Physical", "Communication", "Connect"];
const TYPE_DESC: Record<string, string> = {
  ugv: "Wheeled ground robot, AMR, tugger",
  uav: "Drone; flies air corridors",
  quadruped: "Legged robot (Spot-class)",
  humanoid: "Bipedal humanoid",
  other: "Anything else",
};
const PROTOCOLS: { id: string; label: string; desc: string }[] = [
  { id: "simulation", label: "Simulation", desc: "Nayantra's built-in kinematic simulator" },
  { id: "ros2", label: "ROS 2", desc: "Nav2 over DDS (Isaac Sim, Gazebo, hardware)" },
  { id: "isaac_demo", label: "Isaac demo HTTP", desc: "scripts/isaac_demo.py control API" },
  { id: "mqtt", label: "MQTT", desc: "Not implemented yet" },
  { id: "rest", label: "REST", desc: "Not implemented yet" },
  { id: "websocket", label: "WebSocket", desc: "Not implemented yet" },
  { id: "custom", label: "Custom adapter", desc: "Not implemented yet" },
];
const SENSORS = ["lidar_2d", "lidar_3d", "camera", "stereo_camera", "depth", "imu", "wheel_odometry", "gps", "ultrasonic"];
const COLORS = ["#22d3ee", "#fbbf24", "#4ade80", "#c084fc", "#f472b6", "#fb923c", "#60a5fa", "#a3e635"];

export function RegisterWizard() {
  const close = () => useWorld.getState().openWizard(false);
  const fleets = useWorld((s) => s.fleets);
  const maps = useWorld((s) => s.maps);
  const robots = useWorld((s) => s.robots);
  const activeMapId = useWorld((s) => s.activeMapId);
  const meta = useMeta();
  const [step, setStep] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Robot | null>(null);
  const [advanced, setAdvanced] = useState(false);

  const [name, setName] = useState("");
  const [id, setId] = useState("");
  const [idTouched, setIdTouched] = useState(false);
  const [manufacturer, setManufacturer] = useState("");
  const [model, setModel] = useState("");
  const [type, setType] = useState<RobotType>("ugv");

  const [fleetMode, setFleetMode] = useState<"existing" | "new">("existing");
  const [fleetId, setFleetId] = useState("");
  const [newFleet, setNewFleet] = useState({ name: "", color: COLORS[0], max_speed: 1.0, charging_required: true, priority: 5 });

  const [caps, setCaps] = useState<string[]>([]);
  const [customCap, setCustomCap] = useState("");

  const [nav, setNav] = useState({ stack: "kinematic_sim", namespace: "", map_id: activeMapId ?? "", frame: "map", base_frame: "base_link", odom_source: "odom", localization: "ground_truth", sensors: [] as string[] });
  const [phys, setPhys] = useState({ radius: 0.4, length: 0, width: 0, height: 0, max_speed: 1.0, max_accel: 0.5, turning_radius: 0, payload_kg: 0, battery_wh: 0, charge_time_min: 0, operating_time_min: 0 });
  const [comm, setComm] = useState({ protocol: "simulation", endpoint: "", namespace: "" });
  const [spawn, setSpawn] = useState({ waypoint_id: "", battery_pct: 100 });

  const compatibleFleets = Object.values(fleets).filter((f) => f.robot_type === type || f.robot_type === "other");
  const fleet = fleetMode === "existing" ? fleets[fleetId] : undefined;
  const mapChoices = fleet?.map_ids.length ? fleet.map_ids : Object.keys(maps);
  const map = maps[nav.map_id];
  const layer = type === "uav" ? "air" : "ground";
  const spawnChoices = useMemo(() => {
    if (!map) return [];
    const occupied = new Set(Object.values(robots).map((r) => r.spawn.waypoint_id));
    const restricted = map.zones.filter((z) => z.type === "restricted" || z.type === "no_fly");
    const inside = (x: number, y: number, poly: [number, number][]) => {
      let c = false;
      for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        const [xi, yi] = poly[i];
        const [xj, yj] = poly[j];
        if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
      }
      return c;
    };
    return map.waypoints
      .filter((w) => w.layer === layer && w.type !== "intersection")
      .filter((w) => !restricted.some((z) => (z.layer === null || z.layer === w.layer) && inside(w.x, w.y, z.polygon)))
      .map((w) => ({ ...w, taken: occupied.has(w.id) }));
  }, [map, robots, layer]);

  const pickType = (t: RobotType) => {
    setType(t);
    const d = meta?.robot_type_defaults[t];
    if (d) {
      setCaps(d.capabilities);
      setPhys((p) => ({ ...p, radius: d.radius, max_speed: d.max_speed }));
      setNewFleet((f) => ({ ...f, max_speed: d.max_speed }));
    }
    setFleetId("");
  };

  const validate = (): string => {
    if (step === 0) {
      if (!name.trim()) return "Give the robot a name";
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(id || slug(name))) return "Robot ID may contain letters, digits, _ - .";
      if (robots[id || slug(name)]) return `A robot with ID ${id || slug(name)} already exists`;
    }
    if (step === 1) {
      if (fleetMode === "existing" && !fleetId) return compatibleFleets.length ? "Choose a fleet" : "No compatible fleet — create a new one";
      if (fleetMode === "new" && !newFleet.name.trim()) return "Name the new fleet";
    }
    if (step === 3 && !nav.map_id) return "Choose the map this robot operates on";
    if (step === 4 && phys.radius <= 0) return "Footprint radius must be positive";
    if (step === 5 && comm.protocol === "simulation" && !spawn.waypoint_id) return "Choose where the simulated robot spawns";
    if (step === 5 && comm.protocol === "isaac_demo" && !comm.endpoint) return "Enter the isaac_demo control URL";
    return "";
  };

  const next = () => {
    const v = validate();
    if (v) return setError(v);
    setError("");
    if (step === 1 && fleet) {
      setCaps(fleet.capabilities);
      if (fleet.map_ids[0]) setNav((n) => ({ ...n, map_id: fleet.map_ids.includes(n.map_id) ? n.map_id : fleet.map_ids[0] }));
    }
    if (step === 3 && comm.protocol !== "simulation" && nav.stack === "kinematic_sim") setNav((n) => ({ ...n, stack: type === "uav" ? "px4" : "nav2" }));
    setStep(step + 1);
  };

  const register = async () => {
    setBusy(true);
    setError("");
    try {
      let fid = fleetId;
      if (fleetMode === "new") {
        const f = await api.post<{ id: string }>("/fleets", {
          name: newFleet.name,
          robot_type: type,
          color: newFleet.color,
          capabilities: caps,
          map_ids: nav.map_id ? [nav.map_id] : [],
          layer,
          footprint: { shape: "circle", radius: phys.radius },
          limits: { max_speed: newFleet.max_speed, max_accel: phys.max_accel, max_decel: phys.max_accel * 2, max_yaw_rate: 1.5, ...(type === "uav" ? { max_vertical_speed: 1.5, cruise_altitude: 8 } : {}) },
          charging_required: newFleet.charging_required,
          priority: newFleet.priority,
          navigation: { stack: nav.stack, localization: nav.localization },
          communication: { protocol: comm.protocol },
        });
        fid = f.id;
        setFleetMode("existing");
        setFleetId(fid);
      }
      const opt = (v: number) => (v > 0 ? v : null);
      const robot = await api.post<Robot>("/robots", {
        id: id || slug(name),
        name,
        fleet_id: fid,
        manufacturer,
        model,
        robot_type: type,
        capabilities: caps,
        navigation: { ...nav, stack: nav.stack, localization: nav.localization, namespace: comm.namespace || nav.namespace },
        physical: {
          footprint: { shape: "circle", radius: phys.radius },
          length: opt(phys.length), width: opt(phys.width), height: opt(phys.height),
          max_speed: opt(phys.max_speed), max_accel: opt(phys.max_accel), turning_radius: phys.turning_radius || null,
          payload_kg: phys.payload_kg || null, battery_wh: opt(phys.battery_wh), charge_time_min: opt(phys.charge_time_min),
          operating_time_min: opt(phys.operating_time_min),
        },
        communication: { protocol: comm.protocol, endpoint: comm.endpoint, namespace: comm.namespace || nav.namespace, params: {} },
        spawn: { waypoint_id: spawn.waypoint_id || null, battery_pct: spawn.battery_pct },
      });
      setResult(robot);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const numField = (label: string, key: keyof typeof phys, unit: string, hint?: string) => (
    <Field label={`${label} (${unit})`} hint={hint}>
      <input className="input" type="number" step="0.05" min={0} value={phys[key] || ""} placeholder="—" onChange={(e) => setPhys({ ...phys, [key]: Number(e.target.value) })} />
    </Field>
  );

  const body = () => {
    switch (step) {
      case 0:
        return (
          <div className="col" style={{ gap: 12 }}>
            <div className="grid2">
              <Field label="Robot name"><input className="input" autoFocus value={name} placeholder="UGV-05" onChange={(e) => { setName(e.target.value); if (!idTouched) setId(slug(e.target.value)); }} /></Field>
              <Field label="Robot ID" hint="Used in APIs and logs"><input className="input mono" value={id} onChange={(e) => { setId(e.target.value); setIdTouched(true); }} /></Field>
              <Field label="Manufacturer"><input className="input" value={manufacturer} onChange={(e) => setManufacturer(e.target.value)} /></Field>
              <Field label="Model"><input className="input" value={model} onChange={(e) => setModel(e.target.value)} /></Field>
            </div>
            <Field label="Robot type">
              <div className="choice">
                {(meta?.robot_types ?? ["ugv", "uav", "quadruped", "humanoid", "other"]).map((t) => (
                  <button key={t} className={type === t ? "on" : ""} onClick={() => pickType(t)}>
                    <span className="ct row" style={{ gap: 6 }}><RobotTypeIcon type={t} size={15} /> {ROBOT_TYPE_LABEL[t]}</span>
                    <span className="cd">{TYPE_DESC[t]}</span>
                  </button>
                ))}
              </div>
            </Field>
          </div>
        );
      case 1:
        return (
          <div className="col" style={{ gap: 12 }}>
            <div className="muted">Which fleet should this {ROBOT_TYPE_LABEL[type]} belong to?</div>
            <div className="chips">
              <button className={`chip${fleetMode === "existing" ? " on" : ""}`} onClick={() => setFleetMode("existing")}>Existing fleet</button>
              <button className={`chip${fleetMode === "new" ? " on" : ""}`} onClick={() => setFleetMode("new")}><Plus size={12} /> Create new fleet</button>
            </div>
            {fleetMode === "existing" ? (
              compatibleFleets.length ? (
                <div className="choice">
                  {compatibleFleets.map((f) => (
                    <button key={f.id} className={fleetId === f.id ? "on" : ""} onClick={() => setFleetId(f.id)}>
                      <span className="ct row" style={{ gap: 7 }}><span className="fleet-swatch" style={vars({ "--c": f.color })} />{f.name}</span>
                      <span className="cd">{f.robots.length} robots · {f.capabilities.slice(0, 4).join(", ")}</span>
                    </button>
                  ))}
                </div>
              ) : <div className="faint">No {ROBOT_TYPE_LABEL[type]} fleet yet — create one.</div>
            ) : (
              <div className="grid2">
                <Field label="Fleet name"><input className="input" value={newFleet.name} placeholder="Night-shift UGVs" onChange={(e) => setNewFleet({ ...newFleet, name: e.target.value })} /></Field>
                <Field label="Speed limit (m/s)"><input className="input" type="number" step="0.1" value={newFleet.max_speed} onChange={(e) => setNewFleet({ ...newFleet, max_speed: Number(e.target.value) })} /></Field>
                <Field label="Traffic priority (0–9)"><input className="input" type="number" min={0} max={9} value={newFleet.priority} onChange={(e) => setNewFleet({ ...newFleet, priority: Number(e.target.value) })} /></Field>
                <Field label="Colour">
                  <div className="chips">
                    {COLORS.map((c) => (
                      <button key={c} className={`chip${newFleet.color === c ? " on" : ""}`} style={{ width: 26, padding: 0, justifyContent: "center" }} onClick={() => setNewFleet({ ...newFleet, color: c })}>
                        <span className="fleet-swatch" style={vars({ "--c": c })} />
                      </button>
                    ))}
                  </div>
                </Field>
                <label className="row" style={{ gap: 8 }}><input type="checkbox" className="cbx" checked={newFleet.charging_required} onChange={(e) => setNewFleet({ ...newFleet, charging_required: e.target.checked })} /> Auto-charge when battery runs low</label>
              </div>
            )}
          </div>
        );
      case 2:
        return (
          <div className="col" style={{ gap: 12 }}>
            <div className="muted">What can this robot do? Tasks are allocated by capability, never by robot type.</div>
            <div className="chips">
              {Object.entries(meta?.capabilities ?? {}).map(([k, v]) => (
                <button key={k} className={`chip${caps.includes(k) ? " on" : ""}`} onClick={() => setCaps(caps.includes(k) ? caps.filter((c) => c !== k) : [...caps, k])}>{v}</button>
              ))}
              {caps.filter((c) => !meta?.capabilities[c]).map((c) => (
                <button key={c} className="chip on" onClick={() => setCaps(caps.filter((x) => x !== c))}>{titleCase(c)}</button>
              ))}
            </div>
            <div className="row">
              <input className="input" style={{ width: 220 }} placeholder="Other capability…" value={customCap} onChange={(e) => setCustomCap(e.target.value)} />
              <button className="btn" onClick={() => { if (customCap.trim()) { setCaps([...caps, slug(customCap)]); setCustomCap(""); } }}>Add</button>
            </div>
          </div>
        );
      case 3:
        return (
          <div className="col" style={{ gap: 12 }}>
            <div className="grid2">
              <Field label="Map">
                <select className="input" value={nav.map_id} onChange={(e) => setNav({ ...nav, map_id: e.target.value })}>
                  <option value="">Choose…</option>
                  {mapChoices.map((m) => <option key={m} value={m}>{maps[m]?.name ?? m}</option>)}
                </select>
              </Field>
              <Field label="Navigation stack">
                <select className="input" value={nav.stack} onChange={(e) => setNav({ ...nav, stack: e.target.value })}>
                  {(meta?.nav_stacks ?? []).map((s) => <option key={s} value={s}>{s === "kinematic_sim" ? "Kinematic simulator" : s}</option>)}
                </select>
              </Field>
              <Field label="Localization">
                <select className="input" value={nav.localization} onChange={(e) => setNav({ ...nav, localization: e.target.value })}>
                  {(meta?.localization ?? []).map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </Field>
              <Field label="ROS 2 namespace" hint="Topics become <ns>/odom, <ns>/navigate_to_pose">
                <input className="input mono" placeholder={`/${id || slug(name) || "robot_01"}`} value={nav.namespace} onChange={(e) => setNav({ ...nav, namespace: e.target.value })} />
              </Field>
            </div>
            <button className="btn ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setAdvanced(!advanced)}>{advanced ? "Hide" : "Show"} frames &amp; sensors</button>
            {advanced && (
              <div className="grid3">
                <Field label="Map frame"><input className="input mono" value={nav.frame} onChange={(e) => setNav({ ...nav, frame: e.target.value })} /></Field>
                <Field label="Base frame"><input className="input mono" value={nav.base_frame} onChange={(e) => setNav({ ...nav, base_frame: e.target.value })} /></Field>
                <Field label="Odometry source"><input className="input mono" value={nav.odom_source} onChange={(e) => setNav({ ...nav, odom_source: e.target.value })} /></Field>
                <div style={{ gridColumn: "1 / -1" }}>
                  <Field label="Sensors">
                    <div className="chips">
                      {SENSORS.map((s) => <button key={s} className={`chip${nav.sensors.includes(s) ? " on" : ""}`} onClick={() => setNav({ ...nav, sensors: nav.sensors.includes(s) ? nav.sensors.filter((x) => x !== s) : [...nav.sensors, s] })}>{s}</button>)}
                    </div>
                  </Field>
                </div>
              </div>
            )}
          </div>
        );
      case 4:
        return (
          <div className="col" style={{ gap: 10 }}>
            <div className="muted">These limits drive traffic planning: footprint decides which lanes fit, speed decides reservations.</div>
            <div className="grid3">
              {numField("Footprint radius", "radius", "m", "Circle enclosing the robot")}
              {numField("Max speed", "max_speed", "m/s", "Capped by the fleet limit")}
              {numField("Max acceleration", "max_accel", "m/s²")}
              {numField("Length", "length", "m")}
              {numField("Width", "width", "m")}
              {numField("Height", "height", "m")}
              {numField("Turning radius", "turning_radius", "m", "0 = turns in place")}
              {numField("Payload capacity", "payload_kg", "kg")}
              {numField("Battery capacity", "battery_wh", "Wh")}
              {numField("Charging time", "charge_time_min", "min")}
              {numField("Operating time", "operating_time_min", "min")}
            </div>
          </div>
        );
      case 5:
        return (
          <div className="col" style={{ gap: 12 }}>
            <div className="muted">How does Nayantra talk to this robot?</div>
            <div className="choice">
              {PROTOCOLS.map((p) => (
                <button key={p.id} className={comm.protocol === p.id ? "on" : ""} onClick={() => { setComm({ ...comm, protocol: p.id }); if (p.id === "simulation") setNav((n) => ({ ...n, stack: "kinematic_sim", localization: "ground_truth" })); }}>
                  <span className="ct">{p.label}</span>
                  <span className="cd" style={{ color: meta && !meta.implemented_protocols.includes(p.id) ? "var(--warn)" : undefined }}>{p.desc}</span>
                </button>
              ))}
            </div>
            {comm.protocol === "ros2" && (
              <div className="grid2">
                <Field label="Namespace" hint="Leave empty for the global namespace (single robot)"><input className="input mono" value={comm.namespace || nav.namespace} onChange={(e) => setComm({ ...comm, namespace: e.target.value })} /></Field>
                <Field label="ROS domain" hint="The core uses ROS_DOMAIN_ID from its environment"><input className="input" disabled value="from core environment" /></Field>
              </div>
            )}
            {comm.protocol === "isaac_demo" && (
              <Field label="Control API URL"><input className="input mono" placeholder="http://172.25.61.209:8900" value={comm.endpoint} onChange={(e) => setComm({ ...comm, endpoint: e.target.value })} /></Field>
            )}
            {meta && !meta.implemented_protocols.includes(comm.protocol) && (
              <div className="reason" style={vars({ "--c": "#fbbf24" })}>The {comm.protocol.toUpperCase()} adapter is not implemented yet. The robot will be registered, but its connection test will fail and it won't receive work.</div>
            )}
            {comm.protocol === "simulation" && (
              <div className="grid2">
                <Field label="Spawn at">
                  <select className="input" value={spawn.waypoint_id} onChange={(e) => setSpawn({ ...spawn, waypoint_id: e.target.value })}>
                    <option value="">Choose a waypoint…</option>
                    {spawnChoices.map((w) => <option key={w.id} value={w.id} disabled={w.taken}>{w.name}{w.taken ? " (occupied)" : ""}</option>)}
                  </select>
                </Field>
                <Field label={`Initial battery — ${spawn.battery_pct}%`}><input type="range" min={5} max={100} value={spawn.battery_pct} onChange={(e) => setSpawn({ ...spawn, battery_pct: Number(e.target.value) })} /></Field>
              </div>
            )}
          </div>
        );
      default:
        return result ? <ConnectResult robot={result} /> : (
          <div className="col" style={{ gap: 10 }}>
            <div className="kv">
              <span>Robot</span><span>{name} <span className="faint mono">({id || slug(name)})</span></span>
              <span>Type</span><span>{ROBOT_TYPE_LABEL[type]} · {[manufacturer, model].filter(Boolean).join(" ") || "—"}</span>
              <span>Fleet</span><span>{fleetMode === "new" ? `${newFleet.name} (new)` : fleet?.name}</span>
              <span>Capabilities</span><span>{caps.map(titleCase).join(", ") || "—"}</span>
              <span>Navigation</span><span>{nav.stack} · {nav.localization} · map {maps[nav.map_id]?.name ?? nav.map_id}</span>
              <span>Physical</span><span>r {phys.radius} m · {phys.max_speed} m/s{phys.payload_kg ? ` · ${phys.payload_kg} kg` : ""}</span>
              <span>Link</span><span>{comm.protocol}{comm.endpoint ? ` · ${comm.endpoint}` : ""}{spawn.waypoint_id ? ` · spawn ${map?.waypoints.find((w) => w.id === spawn.waypoint_id)?.name}` : ""}</span>
            </div>
            <div className="faint">Registering runs a connection test right away.</div>
          </div>
        );
    }
  };

  return (
    <Modal
      title="Register a robot"
      onClose={close}
      footer={
        <>
          <button className="btn" onClick={() => (step === 0 || result ? close() : setStep(step - 1))}>{step === 0 || result ? "Close" : <><ArrowLeft size={13} /> Back</>}</button>
          <div className="row">
            {error && <span style={{ color: "var(--err)", fontSize: 12.5 }}>{error}</span>}
            {step < STEPS.length - 1 && <button className="btn primary" onClick={next}>Next <ArrowRight size={13} /></button>}
            {step === STEPS.length - 1 && !result && <button className="btn primary" disabled={busy} onClick={register}>{busy ? "Connecting…" : "Register & test connection"}</button>}
            {result && (
              <button className="btn primary" onClick={() => { const s = useWorld.getState(); s.setPage("operations"); s.selectRobot(result.id); if (result.effective.map_id) s.setActiveMap(result.effective.map_id); close(); }}>
                <MapPin size={13} /> Show on map
              </button>
            )}
          </div>
        </>
      }
    >
      <div className="steps">
        {STEPS.map((s, i) => (
          <div key={s} className={`s${i < step ? " done" : ""}${i === step ? " cur" : ""}`}><div className="b" />{s}</div>
        ))}
      </div>
      {body()}
    </Modal>
  );
}

function ConnectResult({ robot }: { robot: Robot }) {
  const live = useWorld((s) => s.robots[robot.id]) ?? robot;
  const checks: Check[] = live.checks.length ? live.checks : robot.checks;
  const ok = checks.length > 0 && checks.every((c) => c.ok || !c.required);
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="row" style={{ fontWeight: 600 }}>
        {ok ? <CheckCircle2 size={16} style={{ color: "var(--ok)" }} /> : <CircleAlert size={16} style={{ color: "var(--err)" }} />}
        {ok ? `${robot.name} is connected and ready for work` : `${robot.name} was registered, but the connection test failed`}
      </div>
      <div>
        {checks.map((c, i) => (
          <div key={c.name} className="check-line" style={{ animationDelay: `${i * 120}ms` }}>
            {c.ok ? <CheckCircle2 size={16} style={{ color: "var(--ok)" }} /> : c.required ? <CircleAlert size={16} style={{ color: "var(--err)" }} /> : <CircleDashed size={16} style={{ color: "var(--warn)" }} />}
            <div>
              <div className="n">{c.name}</div>
              <div className="d">{c.detail}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
