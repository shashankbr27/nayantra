import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Conflict, Fleet, Lane, MapDetail, Point2, Robot, RobotState, TrafficState, Waypoint } from "../api/types";
import type { LayerKey } from "../store/world";
import { RobotsLayer } from "./RobotsLayer";
import { SX, SY, WP_COLOR, ZONE_STYLE, centroid, fitBox, type ViewBox } from "./geometry";

export type EditTool = "select" | "waypoint" | "lane" | "zone" | "delete";
export type EditSel = { kind: "waypoint" | "lane" | "zone"; id: string } | null;

export interface EditProps {
  tool: EditTool;
  selected: EditSel;
  laneStart: string | null;
  zoneDraft: Point2[];
  onSelect: (sel: EditSel) => void;
  onCanvasClick: (x: number, y: number) => void;
  onWaypointClick: (id: string) => void;
  onLaneClick: (id: string) => void;
  onZoneClick: (id: string) => void;
  onDragWaypoint: (id: string, x: number, y: number, done: boolean) => void;
  onZoneClose: () => void;
}

interface Props {
  map: MapDetail;
  mode: "ops" | "edit";
  layers: Record<LayerKey, boolean>;
  robots: Record<string, Robot>;
  fleets: Record<string, Fleet>;
  states: Record<string, RobotState>;
  traffic?: TrafficState;
  trails?: Record<string, Point2[]>;
  flashes?: { conflict: Conflict; at: number }[];
  selectedRobots?: string[];
  selectedWaypoint?: string | null;
  crosshair?: boolean;
  fitSignal?: number;
  edit?: EditProps;
  onWaypointClick?: (id: string, e: React.MouseEvent) => void;
  onWaypointDoubleClick?: (id: string) => void;
  onRobotClick?: (id: string, e: React.MouseEvent) => void;
  onBackground?: (x: number, y: number) => void;
  onCursor?: (p: Point2 | null) => void;
  onScale?: (pxPerMetre: number) => void;
  control?: React.MutableRefObject<MapControl | null>;
}

export interface MapControl {
  zoom: (factor: number) => void;
  fit: () => void;
  centerOn: (x: number, y: number) => void;
}

const snap = (v: number, step = 0.25) => Math.round(v / step) * step;

const LABEL_PX = 9;
const LABEL_GAP_PX = 10;
const ZONE_LABEL_PX = 10;
const LABEL_PRIORITY: Record<string, number> = {
  pickup: 0, dropoff: 0, charger: 1, dock: 1, landing_pad: 1, parking: 2, location: 2, inspection: 3, intersection: 5,
};

/** Greedy label placement: zones first, then waypoints by importance; skip anything that would overlap. */
function placeLabels(map: MapDetail, waypoints: Waypoint[], unit: number, editing: boolean, air: boolean) {
  const boxes: [number, number, number, number][] = [];
  const hit = (b: [number, number, number, number]) =>
    boxes.some(([x0, y0, x1, y1]) => b[0] < x1 && b[2] > x0 && b[1] < y1 && b[3] > y0);
  const zones = new Set<string>();
  const wps = new Set<string>();
  const pxPerM = 1 / unit;
  if (pxPerM > 7) {
    for (const z of map.zones) {
      if (z.type === "no_fly") continue;
      const [cx, cy] = centroid(z.polygon);
      const text = z.type === "restricted" ? `xx ${z.name}` : z.name;
      const w = text.length * ZONE_LABEL_PX * 0.68 * unit;
      const h = ZONE_LABEL_PX * 1.3 * unit;
      const b: [number, number, number, number] = [SX(cx) - w / 2, SY(cy) - h / 2, SX(cx) + w / 2, SY(cy) + h / 2];
      if (!hit(b)) {
        boxes.push(b);
        zones.add(z.id);
      }
    }
  }
  const minPx = editing ? 9 : 13;
  const order = [...waypoints].sort((a, b) => (LABEL_PRIORITY[a.type] ?? 4) - (LABEL_PRIORITY[b.type] ?? 4));
  for (const w of order) {
    const junction = w.type === "intersection";
    if (junction && (!editing || pxPerM < 22)) continue;
    if (w.layer === "air" && (!air || pxPerM < (editing ? 14 : 26))) continue;
    if (pxPerM < minPx) continue;
    const s = Math.max(0.24, 6 * unit);
    const tw = w.name.length * LABEL_PX * 0.56 * unit;
    const y = SY(w.y) + s + LABEL_GAP_PX * unit;
    const b: [number, number, number, number] = [SX(w.x) - tw / 2 - 2 * unit, y - LABEL_PX * unit, SX(w.x) + tw / 2 + 2 * unit, y + 3 * unit];
    if (!hit(b)) {
      boxes.push(b);
      wps.add(w.id);
    }
  }
  return { zones, waypoints: wps };
}

export function MapView(props: Props) {
  const { map, mode, layers, fitSignal = 0 } = props;
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [measured, setMeasured] = useState(false);
  const [vb, setVb] = useState<ViewBox>(() => fitBox(map.bounds, 800 / 600));
  const drag = useRef<{ kind: "pan" | "wp"; id?: string; sx: number; sy: number; vb: ViewBox; moved: boolean } | null>(null);
  const [panning, setPanning] = useState(false);
  const [dragPos, setDragPos] = useState<{ id: string; x: number; y: number } | null>(null);

  useLayoutEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        setSize({ w: r.width, h: r.height });
        setMeasured(true);
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Fit on map change, on explicit request, and once the real size is known.
  const fittedFor = useRef<string>("");
  useEffect(() => {
    if (!measured) return;
    const key = `${map.id}:${fitSignal}`;
    if (fittedFor.current !== key) {
      fittedFor.current = key;
      setVb(fitBox(map.bounds, size.w / size.h));
    }
  }, [map.id, fitSignal, measured, size.w, size.h, map.bounds]);

  // Keep aspect on resize.
  useEffect(() => {
    setVb((v) => {
      const h = v.w / (size.w / size.h);
      return { ...v, y: v.y + (v.h - h) / 2, h };
    });
  }, [size.w, size.h]);

  const toWorld = useCallback(
    (clientX: number, clientY: number): Point2 => {
      const el = svgRef.current!;
      const r = el.getBoundingClientRect();
      const ux = vb.x + ((clientX - r.left) / r.width) * vb.w;
      const uy = vb.y + ((clientY - r.top) / r.height) * vb.h;
      return [ux, -uy];
    },
    [vb],
  );

  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      setVb((v) => {
        const f = Math.pow(1.0016, e.deltaY);
        const w = Math.max(2.5, Math.min(600, v.w * f));
        const k = w / v.w;
        const px = v.x + ((e.clientX - r.left) / r.width) * v.w;
        const py = v.y + ((e.clientY - r.top) / r.height) * v.h;
        return { x: px - (px - v.x) * k, y: py - (py - v.y) * k, w, h: v.h * k };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const control = props.control;
  useEffect(() => {
    if (!control) return;
    control.current = {
      zoom: (f) =>
        setVb((v) => {
          const w = Math.max(2.5, Math.min(600, v.w * f));
          const k = w / v.w;
          return { x: v.x + (v.w - w) / 2, y: v.y + (v.h - v.h * k) / 2, w, h: v.h * k };
        }),
      fit: () => setVb(fitBox(map.bounds, size.w / size.h)),
      centerOn: (x, y) => setVb((v) => ({ ...v, x: SX(x) - v.w / 2, y: SY(y) - v.h / 2 })),
    };
  }, [control, map.bounds, size.w, size.h]);

  const unit = vb.w / Math.max(1, size.w);
  const pxPerM = 1 / unit;
  const onScale = props.onScale;
  useEffect(() => onScale?.(pxPerM), [pxPerM, onScale]);

  const onMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) return;
    drag.current = { kind: "pan", sx: e.clientX, sy: e.clientY, vb, moved: false };
  };
  const editTool = props.edit?.tool;
  const onWpMouseDown = useCallback(
    (id: string, e: React.MouseEvent) => {
      e.stopPropagation();
      if (mode === "edit" && editTool === "select") {
        drag.current = { kind: "wp", id, sx: e.clientX, sy: e.clientY, vb, moved: false };
      }
    },
    [mode, editTool, vb],
  );
  const onMouseMove = (e: React.MouseEvent) => {
    const d = drag.current;
    if (props.onCursor) props.onCursor(toWorld(e.clientX, e.clientY));
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (!d.moved && Math.hypot(dx, dy) < 4) return;
    d.moved = true;
    if (d.kind === "pan") {
      setPanning(true);
      const k = d.vb.w / size.w;
      setVb({ ...d.vb, x: d.vb.x - dx * k, y: d.vb.y - dy * k });
    } else if (d.id) {
      const [x, y] = toWorld(e.clientX, e.clientY);
      const p = e.altKey ? { x, y } : { x: snap(x), y: snap(y) };
      setDragPos({ id: d.id, ...p });
      props.edit?.onDragWaypoint(d.id, p.x, p.y, false);
    }
  };
  const onMouseUp = (e: React.MouseEvent) => {
    const d = drag.current;
    drag.current = null;
    setPanning(false);
    if (!d) return;
    if (d.kind === "wp" && d.id) {
      if (d.moved && dragPos) props.edit?.onDragWaypoint(d.id, dragPos.x, dragPos.y, true);
      else props.edit?.onWaypointClick(d.id);
      setDragPos(null);
      return;
    }
    if (!d.moved) {
      const [x, y] = toWorld(e.clientX, e.clientY);
      if (mode === "edit") props.edit?.onCanvasClick(e.altKey ? x : snap(x), e.altKey ? y : snap(y));
      else props.onBackground?.(x, y);
    }
  };

  const waypoints = useMemo(() => {
    if (!dragPos) return map.waypoints;
    return map.waypoints.map((w) => (w.id === dragPos.id ? { ...w, x: dragPos.x, y: dragPos.y } : w));
  }, [map.waypoints, dragPos]);
  const wpIndex = useMemo(() => Object.fromEntries(waypoints.map((w) => [w.id, w])), [waypoints]);

  const cursorClass = panning ? "panning" : props.crosshair || (mode === "edit" && props.edit?.tool !== "select") ? "crosshair" : "";

  return (
    <svg
      ref={svgRef}
      className={`map-svg ${cursorClass}`}
      viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
      preserveAspectRatio="xMidYMid meet"
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={() => {
        drag.current = null;
        setPanning(false);
        props.onCursor?.(null);
      }}
      onDoubleClick={() => mode === "edit" && props.edit?.tool === "zone" && props.edit.onZoneClose()}
    >
      <Defs unit={unit} />
      <StaticLayers
        map={map}
        waypoints={waypoints}
        wpIndex={wpIndex}
        layers={layers}
        unit={unit}
        mode={mode}
        edit={props.edit}
        selectedWaypoint={props.selectedWaypoint ?? null}
        onWpMouseDown={onWpMouseDown}
        onWaypointClick={props.onWaypointClick}
        onWaypointDoubleClick={props.onWaypointDoubleClick}
      />
      {mode === "ops" && (
        <LiveLayers
          map={map}
          wpIndex={wpIndex}
          layers={layers}
          unit={unit}
          robots={props.robots}
          fleets={props.fleets}
          states={props.states}
          traffic={props.traffic}
          trails={props.trails ?? {}}
          flashes={props.flashes ?? []}
        />
      )}
      {mode === "edit" && props.edit && <EditOverlay edit={props.edit} wpIndex={wpIndex} unit={unit} />}
      {layers.robots && (
        <RobotsLayer
          mapId={map.id}
          robots={props.robots}
          fleets={props.fleets}
          states={props.states}
          selected={props.selectedRobots ?? []}
          unit={unit}
          labels={layers.labels}
          onRobotClick={props.onRobotClick}
        />
      )}
    </svg>
  );
}

function Defs({ unit }: { unit: number }) {
  const s = 8 * unit;
  return (
    <defs>
      <pattern id="hatch-red" patternUnits="userSpaceOnUse" width={s} height={s} patternTransform="rotate(45)">
        <line x1={0} y1={0} x2={0} y2={s} stroke="rgba(239,68,68,0.35)" strokeWidth={2 * unit} />
      </pattern>
      <pattern id="hatch-amber" patternUnits="userSpaceOnUse" width={s} height={s} patternTransform="rotate(-45)">
        <line x1={0} y1={0} x2={0} y2={s} stroke="rgba(251,191,36,0.22)" strokeWidth={1.5 * unit} />
      </pattern>
    </defs>
  );
}

// ---------------------------------------------------------------------------
// Static: floor, grid, image, zones, obstacles, walls, lanes, waypoints
// ---------------------------------------------------------------------------

interface StaticProps {
  map: MapDetail;
  waypoints: Waypoint[];
  wpIndex: Record<string, Waypoint>;
  layers: Record<LayerKey, boolean>;
  unit: number;
  mode: "ops" | "edit";
  edit?: EditProps;
  selectedWaypoint: string | null;
  onWpMouseDown: (id: string, e: React.MouseEvent) => void;
  onWaypointClick?: (id: string, e: React.MouseEvent) => void;
  onWaypointDoubleClick?: (id: string) => void;
}

const StaticLayers = memo(function StaticLayers(p: StaticProps) {
  const { map, waypoints, wpIndex, layers, unit, mode, edit } = p;
  const b = map.bounds;
  const pxPerM = 1 / unit;
  const editing = mode === "edit";
  const sel = edit?.selected;

  const grid = useMemo(() => {
    if (!layers.grid) return null;
    const lines: React.ReactNode[] = [];
    const minor = pxPerM > 14;
    for (let x = Math.ceil(b.min_x); x <= b.max_x; x += 1) {
      const major = x % 5 === 0;
      if (!major && !minor) continue;
      lines.push(<line key={`x${x}`} x1={x} y1={-b.max_y} x2={x} y2={-b.min_y} className={major ? "m-grid-major" : "m-grid"} strokeWidth={unit} />);
    }
    for (let y = Math.ceil(b.min_y); y <= b.max_y; y += 1) {
      const major = y % 5 === 0;
      if (!major && !minor) continue;
      lines.push(<line key={`y${y}`} x1={b.min_x} y1={-y} x2={b.max_x} y2={-y} className={major ? "m-grid-major" : "m-grid"} strokeWidth={unit} />);
    }
    return lines;
  }, [b, pxPerM, layers.grid, unit]);

  const placed = useMemo(() => placeLabels(map, waypoints, unit, editing, layers.airLanes), [map, waypoints, unit, editing, layers.airLanes]);
  const img = map.image;
  return (
    <g>
      <rect className="m-floor" x={b.min_x} y={-b.max_y} width={b.max_x - b.min_x} height={b.max_y - b.min_y} strokeWidth={unit} />
      {layers.image && img && (
        <image
          href={img.url}
          x={img.origin[0]}
          y={-(img.origin[1] + img.height_px * img.resolution)}
          width={img.width_px * img.resolution}
          height={img.height_px * img.resolution}
          opacity={img.opacity}
          preserveAspectRatio="none"
          style={{ imageRendering: "pixelated" }}
        />
      )}
      {grid}
      {layers.zones &&
        map.zones.map((z) => {
          const st = ZONE_STYLE[z.type] ?? ZONE_STYLE.custom;
          const pts = z.polygon.map(([x, y]) => `${SX(x)},${SY(y)}`).join(" ");
          const [cx, cy] = centroid(z.polygon);
          const isSel = sel?.kind === "zone" && sel.id === z.id;
          if (z.type === "no_fly" && !layers.airLanes) return null;
          return (
            <g
              key={z.id}
              onMouseDown={(e) => editing && e.stopPropagation()}
              onClick={(e) => {
                if (!editing) return;
                e.stopPropagation();
                edit?.onZoneClick(z.id);
              }}
              style={{ cursor: editing ? "pointer" : undefined }}
            >
              <polygon points={pts} fill={st.fill} stroke={isSel ? "var(--accent)" : st.stroke} strokeWidth={(isSel ? 2.5 : 1.2) * unit} strokeDasharray={z.type === "no_fly" ? `${6 * unit} ${4 * unit}` : undefined} />
              {st.hatch && <polygon points={pts} fill={`url(#${st.hatch})`} stroke="none" />}
              {layers.labels && placed.zones.has(z.id) && (
                <text className="m-zone-label" x={SX(cx)} y={SY(cy)} textAnchor="middle" dominantBaseline="middle" fontSize={10 * unit} fill={st.stroke}>
                  {z.type === "restricted" ? `⛔ ${z.name}` : z.name}
                </text>
              )}
            </g>
          );
        })}
      {layers.walls &&
        map.obstacles.map((poly, i) => (
          <polygon key={`o${i}`} className="m-obst" points={poly.map(([x, y]) => `${SX(x)},${SY(y)}`).join(" ")} strokeWidth={unit} />
        ))}
      {layers.walls &&
        map.walls.map((line, i) => (
          <polyline key={`w${i}`} className="m-wall" points={line.map(([x, y]) => `${SX(x)},${SY(y)}`).join(" ")} strokeWidth={Math.max(0.12, 2.5 * unit)} />
        ))}
      {layers.lanes && pxPerM > 16 && (
        <g opacity={0.16} style={{ pointerEvents: "none" }}>
          {map.lanes.map((ln) => {
            const a = wpIndex[ln.from_id];
            const b2 = wpIndex[ln.to_id];
            if (ln.layer !== "ground" || !ln.width || !a || !b2) return null;
            return <line key={`band${ln.id}`} x1={SX(a.x)} y1={SY(a.y)} x2={SX(b2.x)} y2={SY(b2.y)} stroke="var(--lane-hi)" strokeWidth={ln.width} strokeLinecap="round" />;
          })}
        </g>
      )}
      {(layers.lanes || layers.airLanes) &&
        map.lanes.map((ln) => <LaneShape key={ln.id} ln={ln} wpIndex={wpIndex} unit={unit} layers={layers} editing={editing} edit={edit} />)}
      {layers.waypoints &&
        waypoints.map((w) => (
          <WaypointShape
            key={w.id}
            w={w}
            unit={unit}
            labels={layers.labels && placed.waypoints.has(w.id)}
            editing={editing}
            airVisible={layers.airLanes}
            selected={(sel?.kind === "waypoint" && sel.id === w.id) || p.selectedWaypoint === w.id || edit?.laneStart === w.id}
            onMouseDown={p.onWpMouseDown}
            onClick={(e) => {
              e.stopPropagation();
              if (editing) {
                if (edit?.tool !== "select") edit?.onWaypointClick(w.id);
              } else p.onWaypointClick?.(w.id, e);
            }}
            onDoubleClick={() => p.onWaypointDoubleClick?.(w.id)}
          />
        ))}
    </g>
  );
});

function LaneShape({ ln, wpIndex, unit, layers, editing, edit }: {
  ln: Lane; wpIndex: Record<string, Waypoint>; unit: number; layers: Record<LayerKey, boolean>; editing: boolean; edit?: EditProps;
}) {
  const a = wpIndex[ln.from_id];
  const b = wpIndex[ln.to_id];
  if (!a || !b) return null;
  const air = ln.layer === "air";
  if (air ? !layers.airLanes : !layers.lanes) return null;
  const pxPerM = 1 / unit;
  const isSel = edit?.selected?.kind === "lane" && edit.selected.id === ln.id;
  const x1 = SX(a.x), y1 = SY(a.y), x2 = SX(b.x), y2 = SY(b.y);
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
  const ang = (Math.atan2(y2 - y1, x2 - x1) * 180) / Math.PI;
  const cls = `m-lane${air ? " air" : ""}${ln.closed ? " closed" : ""}`;
  const arrow = 5 * unit;
  return (
    <g
      onMouseDown={(e) => editing && e.stopPropagation()}
      onClick={(e) => {
        if (!editing) return;
        e.stopPropagation();
        edit?.onLaneClick(ln.id);
      }}
      opacity={air && !editing && !isSel ? 0.55 : 1}
      style={{ cursor: editing ? "pointer" : undefined }}
    >
      {editing && <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="transparent" strokeWidth={10 * unit} />}
      <line
        x1={x1} y1={y1} x2={x2} y2={y2}
        className={cls}
        strokeWidth={(isSel ? 3.5 : air ? 1.3 : 2) * unit}
        style={isSel ? { stroke: "var(--accent)" } : undefined}
        strokeDasharray={air ? `${6 * unit} ${4 * unit}` : ln.closed ? `${5 * unit} ${4 * unit}` : undefined}
      />
      {!ln.bidirectional && (
        <path
          d={`M ${-arrow} ${-arrow * 0.7} L ${arrow} 0 L ${-arrow} ${arrow * 0.7} Z`}
          transform={`translate(${mx} ${my}) rotate(${ang})`}
          fill={air ? "var(--lane-air)" : "var(--lane-hi)"}
        />
      )}
      {air && ln.altitude != null && pxPerM > 34 && layers.labels && (
        <text className="m-label" x={mx} y={my - 5 * unit} textAnchor="middle" fontSize={9 * unit} strokeWidth={3 * unit} fill="var(--lane-air)">
          {ln.altitude} m
        </text>
      )}
    </g>
  );
}

function WaypointShape({ w, unit, labels, editing, selected, airVisible, onMouseDown, onClick, onDoubleClick }: {
  w: Waypoint; unit: number; labels: boolean; editing: boolean; selected: boolean; airVisible: boolean;
  onMouseDown: (id: string, e: React.MouseEvent) => void; onClick: (e: React.MouseEvent) => void; onDoubleClick: () => void;
}) {
  const pxPerM = 1 / unit;
  if (w.layer === "air" && !airVisible) return null;
  const junction = w.type === "intersection";
  if (junction && !editing && pxPerM < 22 && !selected) return null;
  const color = WP_COLOR[w.type] ?? WP_COLOR.location;
  const s = junction ? Math.max(0.12, 3.5 * unit) : Math.max(0.24, 6 * unit);
  const x = SX(w.x), y = SY(w.y);
  let shape: React.ReactNode;
  switch (w.type) {
    case "charger":
    case "dock":
      shape = <rect className="wp-core" x={-s} y={-s} width={2 * s} height={2 * s} rx={s * 0.35} fill="var(--map-floor)" stroke={color} strokeWidth={1.8 * unit} />;
      break;
    case "pickup":
      shape = <path className="wp-core" d={`M 0 ${-s * 1.2} L ${s * 1.1} ${s * 0.8} L ${-s * 1.1} ${s * 0.8} Z`} fill="var(--map-floor)" stroke={color} strokeWidth={1.8 * unit} />;
      break;
    case "dropoff":
      shape = <path className="wp-core" d={`M 0 ${s * 1.2} L ${s * 1.1} ${-s * 0.8} L ${-s * 1.1} ${-s * 0.8} Z`} fill="var(--map-floor)" stroke={color} strokeWidth={1.8 * unit} />;
      break;
    case "inspection":
      shape = <path className="wp-core" d={`M 0 ${-s * 1.2} L ${s * 1.2} 0 L 0 ${s * 1.2} L ${-s * 1.2} 0 Z`} fill="var(--map-floor)" stroke={color} strokeWidth={1.6 * unit} />;
      break;
    default:
      shape = <circle className="wp-core" r={s} fill={junction ? color : "var(--map-floor)"} stroke={color} strokeWidth={(junction ? 0 : 1.8) * unit} />;
  }
  const glyph = w.type === "charger" ? "⚡" : w.type === "landing_pad" ? "H" : w.type === "parking" ? "P" : null;
  return (
    <g
      className="m-wp"
      transform={`translate(${x} ${y})`}
      onMouseDown={(e) => onMouseDown(w.id, e)}
      onClick={onClick}
      onDoubleClick={(e) => {
        e.stopPropagation();
        onDoubleClick();
      }}
    >
      {selected && <circle r={s * 2.3} fill="none" stroke="var(--accent)" strokeWidth={2 * unit} />}
      <circle r={Math.max(s * 1.6, 7 * unit)} fill="transparent" />
      {shape}
      {glyph && pxPerM > 9 && (
        <text textAnchor="middle" dominantBaseline="central" fontSize={s * 1.25} fill={color} fontWeight={700} style={{ pointerEvents: "none" }}>
          {glyph}
        </text>
      )}
      {(labels || selected) && (
        <text className="m-label" y={s + LABEL_GAP_PX * unit} textAnchor="middle" fontSize={(junction ? 8.5 : LABEL_PX) * unit} strokeWidth={3 * unit}>
          {w.name}
        </text>
      )}
    </g>
  );
}

// ---------------------------------------------------------------------------
// Live: reservations, planned paths, trails, waits, conflicts
// ---------------------------------------------------------------------------

function LiveLayers({ map, wpIndex, layers, unit, robots, fleets, states, traffic, trails, flashes }: {
  map: MapDetail; wpIndex: Record<string, Waypoint>; layers: Record<LayerKey, boolean>; unit: number;
  robots: Record<string, Robot>; fleets: Record<string, Fleet>; states: Record<string, RobotState>;
  traffic?: TrafficState; trails: Record<string, Point2[]>; flashes: { conflict: Conflict; at: number }[];
}) {
  const colorOf = (rid: string) => fleets[robots[rid]?.fleet_id]?.color ?? "#94a3b8";
  const laneIndex = useMemo(() => Object.fromEntries(map.lanes.map((l) => [l.id, l])), [map.lanes]);
  const now = Date.now();
  return (
    <g style={{ pointerEvents: "none" }}>
      {layers.trails &&
        Object.entries(trails).map(([rid, pts]) => {
          const st = states[rid];
          if (!st || st.map_id !== map.id || pts.length < 2) return null;
          return (
            <polyline key={`t${rid}`} points={pts.map(([x, y]) => `${SX(x)},${SY(y)}`).join(" ")} fill="none" stroke={colorOf(rid)} strokeOpacity={0.28} strokeWidth={1.6 * unit} strokeLinejoin="round" />
          );
        })}
      {layers.reservations &&
        traffic?.reservations
          .filter((r) => r.map_id === map.id)
          .map((r, i) => {
            const c = colorOf(r.robot_id);
            if (r.kind === "lane") {
              const ln = laneIndex[r.element_id];
              const a = ln && wpIndex[ln.from_id];
              const b = ln && wpIndex[ln.to_id];
              if (!a || !b) return null;
              return <line key={`rl${i}`} x1={SX(a.x)} y1={SY(a.y)} x2={SX(b.x)} y2={SY(b.y)} stroke={c} strokeOpacity={0.5} strokeWidth={6 * unit} strokeLinecap="round" />;
            }
            const w = wpIndex[r.element_id];
            if (!w) return null;
            return <circle key={`rn${i}`} cx={SX(w.x)} cy={SY(w.y)} r={Math.max(0.5, 9 * unit)} fill={c} fillOpacity={0.12} stroke={c} strokeOpacity={0.55} strokeWidth={1.4 * unit} />;
          })}
      {layers.paths &&
        Object.values(states).map((st) => {
          if (st.map_id !== map.id || st.path.length < 2) return null;
          const c = colorOf(st.robot_id);
          const pts = st.path.map(([x, y]) => `${SX(x)},${SY(y)}`).join(" ");
          const [ex, ey] = st.path[st.path.length - 1];
          return (
            <g key={`p${st.robot_id}`}>
              <polyline points={pts} fill="none" stroke={c} strokeOpacity={0.85} strokeWidth={2 * unit} strokeDasharray={`${7 * unit} ${5 * unit}`} strokeLinejoin="round" />
              <circle cx={SX(ex)} cy={SY(ey)} r={Math.max(0.45, 8 * unit)} fill="none" stroke={c} strokeWidth={2 * unit} />
              <circle cx={SX(ex)} cy={SY(ey)} r={Math.max(0.12, 2.5 * unit)} fill={c} />
            </g>
          );
        })}
      {layers.conflicts &&
        traffic?.waits.map((w) => {
          const a = states[w.robot_id];
          const b = w.holder ? states[w.holder] : undefined;
          if (!a || !b || a.map_id !== map.id) return null;
          return (
            <line key={`w${w.robot_id}`} x1={SX(a.pose.x)} y1={SY(a.pose.y)} x2={SX(b.pose.x)} y2={SY(b.pose.y)} stroke="var(--warn)" strokeOpacity={0.65} strokeWidth={1.4 * unit} strokeDasharray={`${2 * unit} ${4 * unit}`} />
          );
        })}
      {layers.conflicts &&
        flashes
          .filter((f) => f.conflict.map_id === map.id && now - f.at < 12000)
          .map((f) => {
            const [x, y] = f.conflict.point;
            const r = Math.max(0.9, 16 * unit);
            return (
              <g key={f.conflict.id} transform={`translate(${SX(x)} ${SY(y)})`}>
                <circle r={r} fill="rgba(251,191,36,0.10)" stroke="var(--warn)" strokeWidth={2 * unit} className="m-conflict" />
                <text textAnchor="middle" dominantBaseline="central" fontSize={r * 0.95} fontWeight={800} fill="var(--warn)">!</text>
                <text className="m-label" y={-r - 5 * unit} textAnchor="middle" fontSize={10 * unit} strokeWidth={3 * unit} fill="var(--warn)">
                  {f.conflict.kind.replace("_", "-")}
                </text>
              </g>
            );
          })}
    </g>
  );
}

function EditOverlay({ edit, wpIndex, unit }: { edit: EditProps; wpIndex: Record<string, Waypoint>; unit: number }) {
  const draft = edit.zoneDraft;
  const start = edit.laneStart ? wpIndex[edit.laneStart] : undefined;
  return (
    <g style={{ pointerEvents: "none" }}>
      {start && <circle cx={SX(start.x)} cy={SY(start.y)} r={Math.max(0.5, 10 * unit)} fill="none" stroke="var(--accent)" strokeWidth={2 * unit} strokeDasharray={`${3 * unit} ${3 * unit}`} />}
      {draft.length > 0 && (
        <>
          <polyline points={draft.map(([x, y]) => `${SX(x)},${SY(y)}`).join(" ")} fill="var(--accent-soft)" stroke="var(--accent)" strokeWidth={2 * unit} />
          {draft.map(([x, y], i) => (
            <circle key={i} cx={SX(x)} cy={SY(y)} r={4 * unit} fill="var(--accent)" />
          ))}
        </>
      )}
    </g>
  );
}
