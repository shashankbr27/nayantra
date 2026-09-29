import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Layers, Maximize, Minus, Plus, TriangleAlert } from "lucide-react";
import type { Point2 } from "../api/types";
import type { MapControl } from "../map/MapView";
import { type LayerKey, useWorld } from "../store/world";

const LAYER_LABEL: Record<LayerKey, string> = {
  image: "Map image", grid: "Grid", walls: "Walls & obstacles", zones: "Zones", lanes: "Ground lanes",
  airLanes: "Air corridors", waypoints: "Waypoints", labels: "Labels", robots: "Robots", paths: "Planned paths",
  trails: "Trajectories", reservations: "Traffic reservations", conflicts: "Conflicts & waits",
};

export function MapChrome({ control, cursor, pxPerM }: { control: React.MutableRefObject<MapControl | null>; cursor: Point2 | null; pxPerM: number }) {
  const maps = useWorld((s) => s.maps);
  const activeMapId = useWorld((s) => s.activeMapId);
  const setActiveMap = useWorld((s) => s.setActiveMap);
  const layers = useWorld((s) => s.layers);
  const toggleLayer = useWorld((s) => s.toggleLayer);
  const goTo = useWorld((s) => s.goTo);
  const [open, setOpen] = useState(false);
  const pop = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !pop.current?.contains(e.target as Node) && setOpen(false);
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
      if (e.key === "f" || e.key === "F") control.current?.fit();
      if (e.key === "+" || e.key === "=") control.current?.zoom(0.8);
      if (e.key === "-") control.current?.zoom(1.25);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [control]);

  // scale bar: pick a round length ≈ 90 px
  const target = 90 / Math.max(pxPerM, 1e-6);
  const nice = [0.5, 1, 2, 5, 10, 20, 50].find((v) => v >= target) ?? 50;
  return (
    <>
      <div className="map-overlay map-tl" ref={pop}>
        <div className="map-chip glass">
          <select className="input" style={{ height: 24, width: 190, border: 0, background: "transparent", padding: 0 }} value={activeMapId ?? ""} onChange={(e) => setActiveMap(e.target.value)} aria-label="Map">
            {Object.values(maps).map((m) => (
              <option key={m.id} value={m.id}>{m.name}</option>
            ))}
          </select>
          <button className="btn ghost sm icon" onClick={() => setOpen(!open)} title="Layers" aria-label="Layers"><Layers size={14} /></button>
        </div>
        {open && (
          <div className="layers-pop glass">
            {(Object.keys(LAYER_LABEL) as LayerKey[]).map((k) => (
              <label key={k}>
                <input type="checkbox" className="cbx" checked={layers[k]} onChange={() => toggleLayer(k)} /> {LAYER_LABEL[k]}
              </label>
            ))}
          </div>
        )}
        {goTo && (
          <div className="map-chip glass" style={{ paddingRight: 10, borderColor: "var(--accent)" }}>
            <span style={{ color: "var(--accent)", fontWeight: 600 }}>Go-to mode</span>
            <span className="muted">click a waypoint · Esc to cancel</span>
          </div>
        )}
      </div>
      <div className="map-overlay map-br">
        <div className="glass col" style={{ gap: 0, padding: 2 }}>
          <button className="btn ghost icon" onClick={() => control.current?.zoom(0.75)} title="Zoom in (+)"><Plus size={15} /></button>
          <button className="btn ghost icon" onClick={() => control.current?.zoom(1.33)} title="Zoom out (−)"><Minus size={15} /></button>
          <button className="btn ghost icon" onClick={() => control.current?.fit()} title="Fit to view (F)"><Maximize size={14} /></button>
        </div>
      </div>
      <div className="map-overlay map-bl">
        <div className="glass scale">
          <span className="row" style={{ gap: 6 }}>
            <span style={{ display: "inline-block", width: nice * pxPerM, height: 6, borderLeft: "1.5px solid var(--text-2)", borderRight: "1.5px solid var(--text-2)", borderBottom: "1.5px solid var(--text-2)" }} />
            {nice} m
          </span>
          <span className="coords">{cursor ? `x ${cursor[0].toFixed(2)}  y ${cursor[1].toFixed(2)}` : " "}</span>
        </div>
      </div>
    </>
  );
}

/** Top-right stack: "Potential conflict detected … ✓ resolved" as it happens. */
export function ConflictFlashes() {
  const flashes = useWorld((s) => s.flashes);
  const robots = useWorld((s) => s.robots);
  const [, tick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, []);
  const now = Date.now();
  const live = flashes.filter((f) => now - f.at < 9000).slice(-3);
  if (!live.length) return null;
  const name = (id: string) => robots[id]?.name ?? id;
  return (
    <div className="map-overlay map-tr">
      {live.map(({ conflict: c, at }) => {
        const resolved = now - at > 900;
        return (
          <div key={c.id} className="flash glass">
            <div className="t"><TriangleAlert size={13} /> Potential {c.kind.replace("_", "-")} conflict</div>
            <div className="l">
              <span>{c.robots.map(name).join("  ⇄  ")}</span>
              <span className="muted">→ {c.location_name}</span>
            </div>
            {!resolved ? (
              <div className="l muted">Resolving…</div>
            ) : (
              <>
                <div className="l ok"><CheckCircle2 size={12} /> {c.resolution?.message ?? "resolved"}</div>
                <div className="l ok"><CheckCircle2 size={12} /> Route reservations updated · no collision</div>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
