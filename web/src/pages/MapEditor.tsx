import { useEffect, useMemo, useRef, useState } from "react";
import { Download, Hexagon, ImageUp, MapPinPlus, MousePointer2, Plus, Spline, Trash2 } from "lucide-react";
import { api } from "../api/client";
import type { Lane, MapDetail, Point2, Waypoint, Zone } from "../api/types";
import { useWorld } from "../store/world";
import { useMeta } from "../lib/meta";
import { errMsg, titleCase } from "../lib/format";
import { Empty, Field, Modal, vars } from "../components/ui";
import { type EditSel, type EditTool, type MapControl, MapView } from "../map/MapView";

const TOOLS: { id: EditTool; label: string; icon: React.ReactNode; hint: string }[] = [
  { id: "select", label: "Select / move", icon: <MousePointer2 size={16} />, hint: "Click to select · drag waypoints to move (Alt = no snap)" },
  { id: "waypoint", label: "Add waypoint", icon: <MapPinPlus size={16} />, hint: "Click on the map to place a waypoint (snaps to 0.25 m)" },
  { id: "lane", label: "Draw lane", icon: <Spline size={16} />, hint: "Click a waypoint, then another to connect them" },
  { id: "zone", label: "Draw zone", icon: <Hexagon size={16} />, hint: "Click corners · double-click to close the polygon · Esc cancels" },
  { id: "delete", label: "Delete", icon: <Trash2 size={16} />, hint: "Click a waypoint, lane or zone to delete it" },
];

export function MapEditorPage() {
  const maps = useWorld((s) => s.maps);
  const activeMapId = useWorld((s) => s.activeMapId);
  const setActiveMap = useWorld((s) => s.setActiveMap);
  const layers = useWorld((s) => s.layers);
  const robots = useWorld((s) => s.robots);
  const fleets = useWorld((s) => s.fleets);
  const states = useWorld((s) => s.states);
  const toast = useWorld((s) => s.toast);
  const meta = useMeta();
  const map = activeMapId ? maps[activeMapId] : undefined;
  const [tool, setTool] = useState<EditTool>("select");
  const [sel, setSel] = useState<EditSel>(null);
  const [laneStart, setLaneStart] = useState<string | null>(null);
  const [zoneDraft, setZoneDraft] = useState<Point2[]>([]);
  const [wpType, setWpType] = useState("location");
  const [laneOpts, setLaneOpts] = useState({ bidirectional: true, speed_limit: 0 });
  const [zoneType, setZoneType] = useState("restricted");
  const [newMap, setNewMap] = useState(false);
  const [upload, setUpload] = useState(false);
  const control = useRef<MapControl | null>(null);

  useEffect(() => {
    setLaneStart(null);
    setZoneDraft([]);
  }, [tool, activeMapId]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setLaneStart(null);
        setZoneDraft([]);
        setSel(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const call = async <T,>(p: Promise<T>, ok?: string): Promise<T | null> => {
    try {
      const r = await p;
      if (ok) toast("success", ok);
      return r;
    } catch (e) {
      toast("error", errMsg(e));
      return null;
    }
  };

  const nextName = (prefix: string) => {
    const names = new Set(map?.waypoints.map((w) => w.name.toLowerCase()));
    let n = (map?.waypoints.length ?? 0) + 1;
    while (names.has(`${prefix} ${n}`.toLowerCase())) n++;
    return `${prefix} ${n}`;
  };

  const del = async (kind: "waypoint" | "lane" | "zone", id: string) => {
    const path = kind === "waypoint" ? `/waypoints/${id}` : kind === "lane" ? `/lanes/${id}` : `/zones/${id}`;
    await call(api.del(path), `${titleCase(kind)} deleted`);
    setSel(null);
  };

  const edit = map && {
    tool,
    selected: sel,
    laneStart,
    zoneDraft,
    onSelect: setSel,
    onCanvasClick: async (x: number, y: number) => {
      if (tool === "waypoint") {
        const w = await call(api.post<Waypoint>(`/maps/${map.id}/waypoints`, { name: nextName(wpType === "intersection" ? "Junction" : "Waypoint"), x, y, type: wpType }));
        if (w) {
          setSel({ kind: "waypoint", id: w.id });
          setTool("select");
        }
      } else if (tool === "zone") setZoneDraft([...zoneDraft, [x, y]]);
      else setSel(null);
    },
    onWaypointClick: async (id: string) => {
      if (tool === "select") setSel({ kind: "waypoint", id });
      else if (tool === "delete") await del("waypoint", id);
      else if (tool === "lane") {
        if (!laneStart) setLaneStart(id);
        else if (laneStart !== id) {
          const ln = await call(
            api.post<Lane>(`/maps/${map.id}/lanes`, {
              from_id: laneStart,
              to_id: id,
              bidirectional: laneOpts.bidirectional,
              speed_limit: laneOpts.speed_limit || null,
              layer: map.waypoints.find((w) => w.id === laneStart)?.layer ?? "ground",
            }),
          );
          setLaneStart(ln ? id : laneStart);
          if (ln) setSel({ kind: "lane", id: ln.id });
        }
      }
    },
    onLaneClick: async (id: string) => {
      if (tool === "delete") await del("lane", id);
      else if (tool === "select") setSel({ kind: "lane", id });
    },
    onZoneClick: async (id: string) => {
      if (tool === "delete") await del("zone", id);
      else if (tool === "select") setSel({ kind: "zone", id });
    },
    onDragWaypoint: async (id: string, x: number, y: number, done: boolean) => {
      if (done) await call(api.patch(`/waypoints/${id}`, { x, y }));
    },
    onZoneClose: async () => {
      if (zoneDraft.length < 3) return;
      const count = (map.zones.length ?? 0) + 1;
      const z = await call(api.post<Zone>(`/maps/${map.id}/zones`, { name: `${titleCase(zoneType)} zone ${count}`, type: zoneType, polygon: zoneDraft }));
      setZoneDraft([]);
      if (z) {
        setSel({ kind: "zone", id: z.id });
        setTool("select");
      }
    },
  };

  if (!map) {
    return (
      <div className="page">
        <Empty>
          No map yet. <button className="btn primary" onClick={() => setNewMap(true)}><Plus size={13} /> Create a map</button>
        </Empty>
        {newMap && <NewMapModal onClose={() => setNewMap(false)} />}
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: "54px 1fr 320px", height: "100%", minHeight: 0 }}>
      <div style={{ background: "var(--bg-2)", borderRight: "1px solid var(--line)" }}>
        <div className="editor-tools">
          {TOOLS.map((t) => (
            <button key={t.id} className={`btn icon${tool === t.id ? " on" : ""}`} title={t.label} onClick={() => setTool(t.id)}>{t.icon}</button>
          ))}
        </div>
      </div>
      <div className="stage">
        <MapView
          map={map}
          mode="edit"
          layers={{ ...layers, waypoints: true, lanes: true, labels: true, robots: layers.robots }}
          robots={robots}
          fleets={fleets}
          states={states}
          control={control}
          edit={edit || undefined}
        />
        <div className="map-overlay map-tl">
          <div className="map-chip glass">
            <select className="input" style={{ height: 24, width: 200, border: 0, background: "transparent", padding: 0 }} value={map.id} onChange={(e) => setActiveMap(e.target.value)}>
              {Object.values(maps).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
            <button className="btn ghost sm" onClick={() => setNewMap(true)}><Plus size={13} /> Map</button>
            <button className="btn ghost sm" onClick={() => setUpload(true)}><ImageUp size={13} /> Image</button>
            <a className="btn ghost sm" href={`/api/v1/maps/${map.id}/export/rmf-nav-graph`} download={`${map.id}_nav_graph.yaml`} title="Export in the rmf_fleet_adapter nav-graph layout"><Download size={13} /> RMF graph</a>
          </div>
          <div className="map-chip glass" style={{ paddingRight: 10 }}>
            <span className="muted">{TOOLS.find((t) => t.id === tool)?.hint}</span>
          </div>
        </div>
        <div className="map-overlay" style={{ bottom: 10, left: 10 }}>
          <div className="glass row" style={{ padding: "6px 10px", gap: 10 }}>
            {tool === "waypoint" && (
              <label className="row">Type
                <select className="input" style={{ height: 24, width: 140 }} value={wpType} onChange={(e) => setWpType(e.target.value)}>
                  {(meta?.waypoint_types ?? []).map((t) => <option key={t} value={t}>{titleCase(t)}</option>)}
                </select>
              </label>
            )}
            {tool === "lane" && (
              <>
                <label className="row"><input type="checkbox" className="cbx" checked={laneOpts.bidirectional} onChange={(e) => setLaneOpts({ ...laneOpts, bidirectional: e.target.checked })} /> Two-way</label>
                <label className="row">Speed limit <input className="input" type="number" step="0.1" style={{ height: 24, width: 70 }} value={laneOpts.speed_limit || ""} placeholder="none" onChange={(e) => setLaneOpts({ ...laneOpts, speed_limit: Number(e.target.value) })} /> m/s</label>
                {laneStart && <span style={{ color: "var(--accent)" }}>from {map.waypoints.find((w) => w.id === laneStart)?.name} → click next</span>}
              </>
            )}
            {tool === "zone" && (
              <>
                <label className="row">Type
                  <select className="input" style={{ height: 24, width: 150 }} value={zoneType} onChange={(e) => setZoneType(e.target.value)}>
                    {(meta?.zone_types ?? []).map((t) => <option key={t} value={t}>{titleCase(t)}</option>)}
                  </select>
                </label>
                <span className="muted">{zoneDraft.length} corners</span>
                {zoneDraft.length >= 3 && <button className="btn sm primary" onClick={() => void edit?.onZoneClose()}>Close polygon</button>}
              </>
            )}
            {(tool === "select" || tool === "delete") && (
              <span className="muted">{map.waypoints.length} waypoints · {map.lanes.length} lanes · {map.zones.length} zones</span>
            )}
          </div>
        </div>
      </div>
      <aside className="inspector">
        <Properties map={map} sel={sel} onDelete={del} onSaved={() => undefined} />
      </aside>
      {newMap && <NewMapModal onClose={() => setNewMap(false)} />}
      {upload && <UploadImage map={map} onClose={() => setUpload(false)} />}
    </div>
  );
}

function Properties({ map, sel, onDelete, onSaved }: { map: MapDetail; sel: EditSel; onDelete: (k: "waypoint" | "lane" | "zone", id: string) => void; onSaved: () => void }) {
  const toast = useWorld((s) => s.toast);
  const fleets = useWorld((s) => s.fleets);
  const meta = useMeta();
  const item = useMemo(() => {
    if (!sel) return null;
    if (sel.kind === "waypoint") return map.waypoints.find((w) => w.id === sel.id) ?? null;
    if (sel.kind === "lane") return map.lanes.find((l) => l.id === sel.id) ?? null;
    return map.zones.find((z) => z.id === sel.id) ?? null;
  }, [map, sel]);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  useEffect(() => setDraft(item ? { ...item } : {}), [item]);
  if (!sel || !item) {
    return (
      <div>
        <div className="insp-head"><div className="insp-title">{map.name}</div><div className="faint">{map.description}</div></div>
        <div className="insp-sec">
          <div className="kv">
            <span>ID</span><span className="mono">{map.id}</span>
            <span>Bounds</span><span className="mono">{map.bounds.min_x}, {map.bounds.min_y} → {map.bounds.max_x}, {map.bounds.max_y} m</span>
            <span>Waypoints</span><span>{map.waypoints.length}</span>
            <span>Lanes</span><span>{map.lanes.length} ({map.lanes.filter((l) => !l.bidirectional).length} one-way, {map.lanes.filter((l) => l.layer === "air").length} air)</span>
            <span>Zones</span><span>{map.zones.length}</span>
            <span>Image</span><span>{map.image ? `${map.image.width_px}×${map.image.height_px} px @ ${map.image.resolution} m/px` : "none"}</span>
          </div>
        </div>
        <div className="insp-sec faint">Select an element to edit it. The graph you draw here is what fleet-level route planning and traffic coordination use.</div>
      </div>
    );
  }
  const save = async () => {
    const path = sel.kind === "waypoint" ? `/waypoints/${sel.id}` : sel.kind === "lane" ? `/lanes/${sel.id}` : `/zones/${sel.id}`;
    const allowed: Record<string, string[]> = {
      waypoint: ["name", "x", "y", "z", "yaw", "type", "layer", "aliases"],
      lane: ["bidirectional", "speed_limit", "width", "layer", "altitude", "closed", "name"],
      zone: ["name", "type", "speed_limit", "allowed_fleets", "layer"],
    };
    const body = Object.fromEntries(Object.entries(draft).filter(([k]) => allowed[sel.kind].includes(k)));
    try {
      await api.patch(path, body);
      toast("success", "Saved");
      onSaved();
    } catch (e) {
      toast("error", errMsg(e));
    }
  };
  const num = (k: string, label: string, step = 0.1) => (
    <Field label={label}>
      <input className="input" type="number" step={step} value={draft[k] === null || draft[k] === undefined ? "" : String(draft[k])} onChange={(e) => setDraft({ ...draft, [k]: e.target.value === "" ? null : Number(e.target.value) })} />
    </Field>
  );
  const text = (k: string, label: string) => (
    <Field label={label}><input className="input" value={String(draft[k] ?? "")} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} /></Field>
  );
  return (
    <div>
      <div className="insp-head">
        <div className="insp-title">{titleCase(sel.kind)}</div>
        <div className="faint mono">{sel.id}</div>
      </div>
      <div className="insp-sec">
        {sel.kind === "waypoint" && (
          <>
            {text("name", "Name")}
            <Field label="Type">
              <select className="input" value={String(draft.type)} onChange={(e) => setDraft({ ...draft, type: e.target.value })}>
                {(meta?.waypoint_types ?? []).map((t) => <option key={t} value={t}>{titleCase(t)}</option>)}
              </select>
            </Field>
            <div className="grid2">{num("x", "x (m)")}{num("y", "y (m)")}</div>
            <div className="grid2">
              <Field label="Heading (°)"><input className="input" type="number" value={Math.round(((draft.yaw as number) ?? 0) * 180 / Math.PI)} onChange={(e) => setDraft({ ...draft, yaw: (Number(e.target.value) * Math.PI) / 180 })} /></Field>
              {num("z", "z (m)")}
            </div>
            <Field label="Layer">
              <select className="input" value={String(draft.layer)} onChange={(e) => setDraft({ ...draft, layer: e.target.value })}>
                <option value="ground">ground</option><option value="air">air</option>
              </select>
            </Field>
            <Field label="Aliases" hint="Other names operators or the LLM may use, comma-separated">
              <input className="input" value={((draft.aliases as string[]) ?? []).join(", ")} onChange={(e) => setDraft({ ...draft, aliases: e.target.value.split(",").map((a) => a.trim()).filter(Boolean) })} />
            </Field>
          </>
        )}
        {sel.kind === "lane" && (
          <>
            <div className="kv"><span>Connects</span><span>{(item as Lane).from_id} {(item as Lane).bidirectional ? "↔" : "→"} {(item as Lane).to_id}</span></div>
            {text("name", "Name")}
            <label className="row"><input type="checkbox" className="cbx" checked={Boolean(draft.bidirectional)} onChange={(e) => setDraft({ ...draft, bidirectional: e.target.checked })} /> Two-way lane</label>
            <label className="row"><input type="checkbox" className="cbx" checked={Boolean(draft.closed)} onChange={(e) => setDraft({ ...draft, closed: e.target.checked })} /> Closed (routing avoids it)</label>
            <div className="grid2">{num("speed_limit", "Speed limit (m/s)")}{num("width", "Usable width (m)")}</div>
            <div className="grid2">
              <Field label="Layer">
                <select className="input" value={String(draft.layer)} onChange={(e) => setDraft({ ...draft, layer: e.target.value })}>
                  <option value="ground">ground</option><option value="air">air</option>
                </select>
              </Field>
              {num("altitude", "Altitude (m, air)", 1)}
            </div>
          </>
        )}
        {sel.kind === "zone" && (
          <>
            {text("name", "Name")}
            <Field label="Type">
              <select className="input" value={String(draft.type)} onChange={(e) => setDraft({ ...draft, type: e.target.value })}>
                {(meta?.zone_types ?? []).map((t) => <option key={t} value={t}>{titleCase(t)}</option>)}
              </select>
            </Field>
            {num("speed_limit", "Speed limit (m/s)")}
            <Field label={draft.type === "fleet_boundary" ? "Fleets confined to this zone" : "Fleets allowed inside"}>
              <div className="chips">
                {Object.values(fleets).map((f) => {
                  const list = (draft.allowed_fleets as string[]) ?? [];
                  const on = list.includes(f.id);
                  return (
                    <button key={f.id} className={`chip${on ? " on" : ""}`} onClick={() => setDraft({ ...draft, allowed_fleets: on ? list.filter((x) => x !== f.id) : [...list, f.id] })}>
                      <span className="fleet-swatch" style={vars({ "--c": f.color })} /> {f.name}
                    </button>
                  );
                })}
              </div>
            </Field>
          </>
        )}
        <div className="row" style={{ marginTop: 6 }}>
          <button className="btn primary" onClick={save}>Save</button>
          <button className="btn danger" onClick={() => onDelete(sel.kind, sel.id)}><Trash2 size={13} /> Delete</button>
        </div>
      </div>
    </div>
  );
}

function NewMapModal({ onClose }: { onClose: () => void }) {
  const setActiveMap = useWorld((s) => s.setActiveMap);
  const [f, setF] = useState({ name: "", description: "", min_x: 0, min_y: 0, max_x: 30, max_y: 20 });
  const [err, setErr] = useState("");
  const save = async () => {
    try {
      const m = await api.post<MapDetail>("/maps", { name: f.name, description: f.description, bounds: { min_x: f.min_x, min_y: f.min_y, max_x: f.max_x, max_y: f.max_y } });
      setActiveMap(m.id);
      onClose();
    } catch (e) {
      setErr(errMsg(e));
    }
  };
  const n = (k: keyof typeof f, label: string) => (
    <Field label={label}><input className="input" type="number" value={f[k] as number} onChange={(e) => setF({ ...f, [k]: Number(e.target.value) })} /></Field>
  );
  return (
    <Modal size="sm" title="New map" onClose={onClose} footer={<><span style={{ color: "var(--err)" }}>{err}</span><button className="btn primary" onClick={save}>Create map</button></>}>
      <div className="col" style={{ gap: 10 }}>
        <Field label="Name"><input className="input" autoFocus value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Description"><input className="input" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
        <div className="section-title">Workspace bounds (metres)</div>
        <div className="grid2">{n("min_x", "min x")}{n("min_y", "min y")}{n("max_x", "max x")}{n("max_y", "max y")}</div>
        <div className="faint">Goals outside the bounds are rejected (workspace hard limit).</div>
      </div>
    </Modal>
  );
}

async function pgmToPng(buf: ArrayBuffer): Promise<{ url: string; w: number; h: number }> {
  const bytes = new Uint8Array(buf);
  let pos = 0;
  const token = () => {
    for (;;) {
      while (pos < bytes.length && /\s/.test(String.fromCharCode(bytes[pos]))) pos++;
      if (bytes[pos] === 35) while (pos < bytes.length && bytes[pos] !== 10) pos++;
      else break;
    }
    let s = "";
    while (pos < bytes.length && !/\s/.test(String.fromCharCode(bytes[pos]))) s += String.fromCharCode(bytes[pos++]);
    return s;
  };
  const magic = token();
  const w = Number(token());
  const h = Number(token());
  const max = Number(token());
  pos++;
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d")!;
  const img = ctx.createImageData(w, h);
  for (let i = 0; i < w * h; i++) {
    const v = magic === "P5" ? bytes[pos + i] : Number(token());
    const g = Math.round((v / max) * 255);
    img.data[i * 4] = img.data[i * 4 + 1] = img.data[i * 4 + 2] = g;
    img.data[i * 4 + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
  return { url: canvas.toDataURL("image/png"), w, h };
}

function UploadImage({ map, onClose }: { map: MapDetail; onClose: () => void }) {
  const [file, setFile] = useState<{ url: string; w: number; h: number } | null>(null);
  const [res, setRes] = useState(0.05);
  const [origin, setOrigin] = useState<[number, number]>([map.bounds.min_x, map.bounds.min_y]);
  const [opacity, setOpacity] = useState(0.6);
  const [err, setErr] = useState("");
  const pick = async (f: File) => {
    setErr("");
    try {
      if (f.name.endsWith(".yaml") || f.name.endsWith(".yml")) {
        const t = await f.text();
        const r = /resolution:\s*([\d.]+)/.exec(t);
        const o = /origin:\s*\[\s*([-\d.]+)\s*,\s*([-\d.]+)/.exec(t);
        if (r) setRes(Number(r[1]));
        if (o) setOrigin([Number(o[1]), Number(o[2])]);
        return;
      }
      if (f.name.endsWith(".pgm")) {
        setFile(await pgmToPng(await f.arrayBuffer()));
        return;
      }
      const url = await new Promise<string>((ok, bad) => {
        const rd = new FileReader();
        rd.onload = () => ok(String(rd.result));
        rd.onerror = () => bad(rd.error);
        rd.readAsDataURL(f);
      });
      const dims = await new Promise<{ w: number; h: number }>((ok) => {
        const im = new Image();
        im.onload = () => ok({ w: im.naturalWidth, h: im.naturalHeight });
        im.src = url;
      });
      setFile({ url, ...dims });
    } catch (e) {
      setErr(errMsg(e));
    }
  };
  const save = async () => {
    if (!file) return setErr("Choose an image first");
    try {
      await api.post(`/maps/${map.id}/image`, { data_url: file.url, resolution: res, origin: [origin[0], origin[1], 0], width_px: file.w, height_px: file.h, opacity });
      onClose();
    } catch (e) {
      setErr(errMsg(e));
    }
  };
  return (
    <Modal size="sm" title={`Map image — ${map.name}`} onClose={onClose} footer={<><span style={{ color: "var(--err)" }}>{err}</span><button className="btn primary" onClick={save}>Upload</button></>}>
      <div className="col" style={{ gap: 10 }}>
        <Field label="Image" hint="PNG / JPEG, or a Nav2 map_server .pgm (add its .yaml to fill resolution and origin)">
          <input type="file" accept=".png,.jpg,.jpeg,.webp,.pgm,.yaml,.yml" multiple onChange={(e) => Array.from(e.target.files ?? []).forEach((f) => void pick(f))} />
        </Field>
        {file && <div className="faint">{file.w} × {file.h} px → {(file.w * res).toFixed(1)} × {(file.h * res).toFixed(1)} m</div>}
        <div className="grid3">
          <Field label="Resolution (m/px)"><input className="input" type="number" step="0.005" value={res} onChange={(e) => setRes(Number(e.target.value))} /></Field>
          <Field label="Origin x"><input className="input" type="number" value={origin[0]} onChange={(e) => setOrigin([Number(e.target.value), origin[1]])} /></Field>
          <Field label="Origin y"><input className="input" type="number" value={origin[1]} onChange={(e) => setOrigin([origin[0], Number(e.target.value)])} /></Field>
        </div>
        <Field label={`Opacity — ${Math.round(opacity * 100)}%`}><input type="range" min={0.1} max={1} step={0.05} value={opacity} onChange={(e) => setOpacity(Number(e.target.value))} /></Field>
      </div>
    </Modal>
  );
}
