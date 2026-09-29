import { useEffect, useMemo, useState } from "react";
import { Eye, Plus, Send, X } from "lucide-react";
import { api, isConfirmation } from "../api/client";
import type { TaskStep } from "../api/types";
import { useWorld } from "../store/world";
import { useMeta } from "../lib/meta";
import { errMsg, titleCase } from "../lib/format";
import { Field, Modal, vars } from "./ui";

interface Preview {
  map_id: string;
  steps: TaskStep[];
  allocation: { selected: string | null; explanation: string };
  restricted: string[];
}

/** Structured task creation — the same schema the LLM's create_task tool uses. */
export function TaskComposer() {
  const composer = useWorld((s) => s.composer);
  const close = useWorld((s) => s.closeComposer);
  const maps = useWorld((s) => s.maps);
  const activeMapId = useWorld((s) => s.activeMapId);
  const fleets = useWorld((s) => s.fleets);
  const robots = useWorld((s) => s.robots);
  const toast = useWorld((s) => s.toast);
  const selectTask = useWorld((s) => s.selectTask);
  const meta = useMeta();
  const preset = composer.preset ?? {};

  const [type, setType] = useState(preset.type ?? (preset.destination ? "navigate" : "delivery"));
  const [params, setParams] = useState<Record<string, unknown>>(preset.destination ? { destination: preset.destination } : {});
  const [priority, setPriority] = useState(5);
  const [target, setTarget] = useState<"auto" | "fleet" | "robots">(preset.robotIds?.length ? "robots" : "auto");
  const [fleetId, setFleetId] = useState<string>("");
  const [robotIds, setRobotIds] = useState<string[]>(preset.robotIds ?? []);
  const [count, setCount] = useState(1);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const map = activeMapId ? maps[activeMapId] : undefined;
  const places = useMemo(() => {
    if (!map) return [];
    const zones = map.zones.filter((z) => !["restricted", "no_fly", "slow", "fleet_boundary"].includes(z.type)).map((z) => ({ id: z.name, label: `${z.name} (zone — nearest free spot)` }));
    const wps = map.waypoints.filter((w) => w.type !== "intersection").map((w) => ({ id: w.id, label: `${w.name}${w.layer === "air" ? " (air)" : ""}` }));
    return [...zones, ...wps];
  }, [map]);

  const spec = meta?.task_types[type];
  useEffect(() => setPreview(null), [type, params, target, fleetId, robotIds, priority]);

  const body = (robotId?: string) => ({
    type,
    params: Object.fromEntries(Object.entries(params).filter(([, v]) => v !== "" && v !== undefined && !(Array.isArray(v) && v.length === 0))),
    priority,
    map_id: map?.id,
    requirements: {
      fleet_id: target === "fleet" && fleetId ? fleetId : null,
      robot_id: robotId ?? null,
    },
    created_by: "operator",
    trace: { source: "operator" },
  });

  const doPreview = async () => {
    setError("");
    try {
      setPreview(await api.post<Preview>("/tasks/validate", body(target === "robots" ? robotIds[0] : undefined)));
    } catch (e) {
      setError(errMsg(e));
    }
  };

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const created: string[] = [];
      if (target === "robots") {
        if (!robotIds.length) throw new Error("Pick at least one robot");
        for (const rid of robotIds) {
          const r = await api.post<{ tasks: { id: string }[] }>("/tasks", body(rid));
          if (isConfirmation(r)) toast("warning", (r as unknown as { message: string }).message);
          else created.push(...r.tasks.map((t) => t.id));
        }
      } else {
        const r = await api.post<{ tasks: { id: string }[] }>("/tasks", body(), { count });
        if (isConfirmation(r)) toast("warning", (r as unknown as { message: string }).message);
        else created.push(...r.tasks.map((t) => t.id));
      }
      if (created.length) {
        toast("success", `Created ${created.join(", ")}`);
        if (created.length === 1) selectTask(created[0]);
      }
      close();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  const placeInput = (key: string, required: boolean) => (
    <Field key={key} label={`${titleCase(key)}${required ? "" : " (optional)"}`}>
      <input className="input" list="nayantra-places" value={String(params[key] ?? "")} placeholder="waypoint or zone name" onChange={(e) => setParams({ ...params, [key]: e.target.value })} />
    </Field>
  );

  return (
    <Modal
      title="New task"
      onClose={close}
      footer={
        <>
          <button className="btn" onClick={doPreview}><Eye size={13} /> Preview allocation</button>
          <div className="row">
            <button className="btn" onClick={close}>Cancel</button>
            <button className="btn primary" disabled={busy} onClick={submit}><Send size={13} /> Create {target !== "robots" && count > 1 ? `${count} tasks` : "task"}</button>
          </div>
        </>
      }
    >
      <datalist id="nayantra-places">
        {places.map((p) => (
          <option key={p.id} value={p.id}>{p.label}</option>
        ))}
      </datalist>
      <div className="col" style={{ gap: 14 }}>
        <div className="choice">
          {Object.entries(meta?.task_types ?? {}).map(([k, v]) => (
            <button key={k} className={type === k ? "on" : ""} onClick={() => { setType(k); setParams(k === "navigate" && preset.destination ? { destination: preset.destination } : {}); }}>
              <span className="ct">{v.label}</span>
              <span className="cd">{Object.keys(v.params).join(", ") || "no parameters"}</span>
            </button>
          ))}
        </div>
        <div className="grid2">
          {spec &&
            Object.entries(spec.params).map(([key, p]) => {
              if (p.type === "place") return placeInput(key, p.required);
              if (p.type === "place[]") {
                const list = (params[key] as string[] | undefined) ?? [];
                return (
                  <Field key={key} label="Waypoints (in order)">
                    <div className="chips">
                      {list.map((w, i) => (
                        <span key={i} className="chip on">{w}<X size={11} style={{ cursor: "pointer" }} onClick={() => setParams({ ...params, [key]: list.filter((_, j) => j !== i) })} /></span>
                      ))}
                    </div>
                    <input
                      className="input"
                      list="nayantra-places"
                      placeholder="add a waypoint and press Enter"
                      onKeyDown={(e) => {
                        const v = (e.target as HTMLInputElement).value.trim();
                        if (e.key === "Enter" && v) {
                          e.preventDefault();
                          setParams({ ...params, [key]: [...list, v] });
                          (e.target as HTMLInputElement).value = "";
                        }
                      }}
                    />
                  </Field>
                );
              }
              return (
                <Field key={key} label={titleCase(key)}>
                  <input className="input" type={p.type === "string" ? "text" : "number"} value={String(params[key] ?? p.default ?? "")} onChange={(e) => setParams({ ...params, [key]: p.type === "string" ? e.target.value : Number(e.target.value) })} />
                </Field>
              );
            })}
          <Field label={`Priority — ${priority}`} hint="Higher priority wins traffic negotiations">
            <input type="range" min={0} max={9} value={priority} onChange={(e) => setPriority(Number(e.target.value))} />
          </Field>
        </div>
        <Field label="Who does it?">
          <div className="chips">
            <button className={`chip${target === "auto" ? " on" : ""}`} onClick={() => setTarget("auto")}>Let Nayantra choose</button>
            <button className={`chip${target === "fleet" ? " on" : ""}`} onClick={() => setTarget("fleet")}>A specific fleet</button>
            <button className={`chip${target === "robots" ? " on" : ""}`} onClick={() => setTarget("robots")}>Specific robots</button>
          </div>
        </Field>
        {target === "auto" && (
          <Field label="How many" hint="Each copy is allocated separately (e.g. three packages → three robots)">
            <input className="input" type="number" min={1} max={20} value={count} onChange={(e) => setCount(Math.max(1, Math.min(20, Number(e.target.value))))} style={{ width: 90 }} />
          </Field>
        )}
        {target === "fleet" && (
          <Field label="Fleet">
            <select className="input" value={fleetId} onChange={(e) => setFleetId(e.target.value)}>
              <option value="">Choose…</option>
              {Object.values(fleets).map((f) => (
                <option key={f.id} value={f.id}>{f.name}</option>
              ))}
            </select>
          </Field>
        )}
        {target === "robots" && (
          <Field label="Robots">
            <div className="chips">
              {Object.values(robots).map((r) => (
                <button key={r.id} className={`chip${robotIds.includes(r.id) ? " on" : ""}`} onClick={() => setRobotIds(robotIds.includes(r.id) ? robotIds.filter((x) => x !== r.id) : [...robotIds, r.id])}>
                  <span className="fleet-swatch" style={vars({ "--c": fleets[r.fleet_id]?.color ?? "#888" })} /> {r.name}
                </button>
              ))}
            </div>
          </Field>
        )}
        {error && <div className="reason" style={vars({ "--c": "#f87171" })}>{error}</div>}
        {preview && (
          <div className="reason" style={vars({ "--c": preview.allocation.selected ? "#34d399" : "#fbbf24" })}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>{preview.steps.map((s) => s.label).join(" → ")}</div>
            <div>{preview.allocation.explanation}</div>
            {preview.restricted.length > 0 && <div style={{ marginTop: 4, color: "var(--err)" }}>Restricted destination: {preview.restricted.join(", ")} — you'll be asked to confirm.</div>}
          </div>
        )}
        {!map && <div className="faint"><Plus size={12} /> No active map.</div>}
      </div>
    </Modal>
  );
}
