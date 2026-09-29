import type { Bounds, Point2, Robot } from "../api/types";

// World (metres, y up) → SVG user units (y down): (x, y) → (x, -y).
export const SX = (x: number) => x;
export const SY = (y: number) => -y;

export interface ViewBox { x: number; y: number; w: number; h: number }

export function fitBox(b: Bounds, aspect: number, pad = 1.5): ViewBox {
  const w0 = b.max_x - b.min_x + pad * 2;
  const h0 = b.max_y - b.min_y + pad * 2;
  let w = w0;
  let h = h0;
  if (w / h > aspect) h = w / aspect;
  else w = h * aspect;
  const cx = (b.min_x + b.max_x) / 2;
  const cy = -(b.min_y + b.max_y) / 2;
  return { x: cx - w / 2, y: cy - h / 2, w, h };
}

export function centroid(poly: Point2[]): Point2 {
  let x = 0;
  let y = 0;
  for (const [px, py] of poly) {
    x += px;
    y += py;
  }
  return [x / poly.length, y / poly.length];
}

export function robotRadius(r: Robot | undefined): number {
  const fp = r?.effective.footprint;
  if (!fp) return 0.4;
  if (fp.shape === "rectangle" && fp.length && fp.width) return 0.5 * Math.hypot(fp.length, fp.width);
  return fp.radius;
}

export function wrapAngle(a: number): number {
  return Math.atan2(Math.sin(a), Math.cos(a));
}

export const ZONE_STYLE: Record<string, { fill: string; stroke: string; hatch?: string }> = {
  restricted: { fill: "rgba(239,68,68,0.10)", stroke: "rgba(239,68,68,0.55)", hatch: "hatch-red" },
  no_fly: { fill: "rgba(251,191,36,0.05)", stroke: "rgba(251,191,36,0.45)", hatch: "hatch-amber" },
  slow: { fill: "rgba(96,165,250,0.06)", stroke: "rgba(96,165,250,0.35)" },
  charging: { fill: "rgba(52,211,153,0.07)", stroke: "rgba(52,211,153,0.4)" },
  receiving: { fill: "rgba(56,189,248,0.07)", stroke: "rgba(56,189,248,0.4)" },
  storage: { fill: "rgba(167,139,250,0.07)", stroke: "rgba(167,139,250,0.4)" },
  loading: { fill: "rgba(45,212,191,0.07)", stroke: "rgba(45,212,191,0.4)" },
  pickup: { fill: "rgba(56,189,248,0.06)", stroke: "rgba(56,189,248,0.35)" },
  dropoff: { fill: "rgba(167,139,250,0.06)", stroke: "rgba(167,139,250,0.35)" },
  fleet_boundary: { fill: "rgba(148,163,184,0.03)", stroke: "rgba(148,163,184,0.45)" },
  custom: { fill: "rgba(148,163,184,0.05)", stroke: "rgba(148,163,184,0.3)" },
};

export const WP_COLOR: Record<string, string> = {
  charger: "#34d399",
  pickup: "#38bdf8",
  dropoff: "#a78bfa",
  dock: "#2dd4bf",
  parking: "#94a3b8",
  landing_pad: "#fbbf24",
  inspection: "#f472b6",
  location: "#cbd5e1",
  intersection: "#64748b",
};
