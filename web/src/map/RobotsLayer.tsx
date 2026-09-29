import { memo, useEffect, useRef, useState } from "react";
import type { Fleet, Robot, RobotState } from "../api/types";
import { MODE_META } from "../lib/format";
import { SX, SY, robotRadius, wrapAngle } from "./geometry";

interface Shown { x: number; y: number; z: number; yaw: number }

/** Smoothly interpolates the 5 Hz telemetry into 60 fps motion. */
function useAnimatedPoses(states: Record<string, RobotState>): Record<string, Shown> {
  const target = useRef(states);
  target.current = states;
  const [shown, setShown] = useState<Record<string, Shown>>(() => {
    const init: Record<string, Shown> = {};
    for (const [id, s] of Object.entries(states)) init[id] = { ...s.pose };
    return init;
  });
  const shownRef = useRef(shown);
  shownRef.current = shown;

  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const k = 1 - Math.exp(-dt * 9);
      const cur = shownRef.current;
      const next: Record<string, Shown> = {};
      let changed = false;
      for (const [id, s] of Object.entries(target.current)) {
        const p = s.pose;
        const c = cur[id];
        if (!c || Math.hypot(p.x - c.x, p.y - c.y) > 4) {
          next[id] = { ...p };
          changed = true;
          continue;
        }
        const n = {
          x: c.x + (p.x - c.x) * k,
          y: c.y + (p.y - c.y) * k,
          z: c.z + (p.z - c.z) * k,
          yaw: c.yaw + wrapAngle(p.yaw - c.yaw) * k,
        };
        if (Math.abs(n.x - c.x) + Math.abs(n.y - c.y) + Math.abs(n.yaw - c.yaw) + Math.abs(n.z - c.z) > 1e-4) changed = true;
        next[id] = n;
      }
      if (changed || Object.keys(cur).length !== Object.keys(next).length) setShown(next);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return shown;
}

function Glyph({ type, r, color }: { type: string; r: number; color: string }) {
  const stroke = "rgba(0,0,0,0.55)";
  switch (type) {
    case "uav": {
      const d = r * 0.72;
      return (
        <g>
          {[45, 135, 225, 315].map((a) => {
            const rad = (a * Math.PI) / 180;
            return (
              <g key={a}>
                <line x1={0} y1={0} x2={Math.cos(rad) * d} y2={Math.sin(rad) * d} stroke={color} strokeWidth={r * 0.14} />
                <circle cx={Math.cos(rad) * d} cy={Math.sin(rad) * d} r={r * 0.3} fill="none" stroke={color} strokeWidth={r * 0.1} />
              </g>
            );
          })}
          <circle r={r * 0.34} fill={color} stroke={stroke} strokeWidth={r * 0.06} />
          <path d={`M ${r * 0.2} ${-r * 0.16} L ${r * 0.48} 0 L ${r * 0.2} ${r * 0.16} Z`} fill="#fff" opacity={0.9} />
        </g>
      );
    }
    case "quadruped":
      return (
        <g>
          {[-1, 1].flatMap((sx) => [-1, 1].map((sy) => (
            <circle key={`${sx}${sy}`} cx={sx * r * 0.62} cy={sy * r * 0.6} r={r * 0.16} fill={color} opacity={0.75} />
          )))}
          <path
            d={`M ${-r * 0.9} ${-r * 0.36} L ${r * 0.62} ${-r * 0.36} L ${r * 1.02} 0 L ${r * 0.62} ${r * 0.36} L ${-r * 0.9} ${r * 0.36} Z`}
            fill={color}
            stroke={stroke}
            strokeWidth={r * 0.06}
          />
        </g>
      );
    case "humanoid":
      return (
        <g>
          <ellipse rx={r * 0.55} ry={r * 0.95} fill={color} stroke={stroke} strokeWidth={r * 0.06} />
          <circle cx={r * 0.22} r={r * 0.32} fill="#fff" opacity={0.85} />
        </g>
      );
    case "ugv":
      return (
        <g>
          <rect x={-r * 0.95} y={-r * 0.7} width={r * 1.9} height={r * 1.4} rx={r * 0.28} fill={color} stroke={stroke} strokeWidth={r * 0.06} />
          <path d={`M ${r * 0.28} ${-r * 0.42} L ${r * 0.85} 0 L ${r * 0.28} ${r * 0.42} Z`} fill="#fff" opacity={0.85} />
        </g>
      );
    default:
      return (
        <g>
          <circle r={r * 0.9} fill={color} stroke={stroke} strokeWidth={r * 0.06} />
          <path d={`M ${r * 0.25} ${-r * 0.35} L ${r * 0.8} 0 L ${r * 0.25} ${r * 0.35} Z`} fill="#fff" opacity={0.85} />
        </g>
      );
  }
}

interface Props {
  mapId: string;
  robots: Record<string, Robot>;
  fleets: Record<string, Fleet>;
  states: Record<string, RobotState>;
  selected: string[];
  unit: number; // world units per screen pixel
  labels: boolean;
  onRobotClick?: (id: string, e: React.MouseEvent) => void;
}

export const RobotsLayer = memo(function RobotsLayer({ mapId, robots, fleets, states, selected, unit, labels, onRobotClick }: Props) {
  const onMap: Record<string, RobotState> = {};
  for (const [id, s] of Object.entries(states)) if (s.map_id === mapId && robots[id]) onMap[id] = s;
  const shown = useAnimatedPoses(onMap);
  const ids = Object.keys(onMap).sort((a, b) => Number(selected.includes(a)) - Number(selected.includes(b)));
  // Label placement: above, below, then right of the robot — whichever is free.
  const labelPos: Record<string, { dx: number; dy: number; anchor: "middle" | "start" }> = {};
  if (labels) {
    const boxes: [number, number, number, number][] = [];
    const fs = 11 * unit;
    const order = [...ids].sort((a, b) => Number(selected.includes(b)) - Number(selected.includes(a)));
    for (const id of order) {
      const p = shown[id] ?? onMap[id].pose;
      const r = Math.max(robotRadius(robots[id]), 7 * unit);
      const text = robots[id].name + (robots[id].effective.robot_type === "uav" && p.z > 0.3 ? " · 12 m" : "");
      const w = text.length * fs * 0.6;
      const cx = SX(p.x);
      const cy = SY(p.y);
      const options = [
        { dx: 0, dy: -r * 1.62, anchor: "middle" as const, box: [cx - w / 2, cy - r * 1.62 - fs, cx + w / 2, cy - r * 1.62] },
        { dx: 0, dy: r * 1.62 + fs, anchor: "middle" as const, box: [cx - w / 2, cy + r * 1.62, cx + w / 2, cy + r * 1.62 + fs] },
        { dx: r * 1.5, dy: fs * 0.35, anchor: "start" as const, box: [cx + r * 1.5, cy - fs * 0.6, cx + r * 1.5 + w, cy + fs * 0.4] },
      ];
      const free = options.find((o) => !boxes.some(([x0, y0, x1, y1]) => o.box[0] < x1 && o.box[2] > x0 && o.box[1] < y1 && o.box[3] > y0)) ?? options[0];
      boxes.push(free.box as [number, number, number, number]);
      labelPos[id] = { dx: free.dx, dy: free.dy, anchor: free.anchor };
    }
  }
  return (
    <g>
      {ids.map((id) => {
        const s = onMap[id];
        const p = shown[id] ?? s.pose;
        const robot = robots[id];
        const fleet = fleets[robot.fleet_id];
        const color = fleet?.color ?? "#94a3b8";
        const type = robot.effective.robot_type;
        const r = Math.max(robotRadius(robot), 7 * unit);
        const mode = s.online ? s.mode : "offline";
        const ring = MODE_META[mode]?.color ?? "#64748b";
        const sel = selected.includes(id);
        const airborne = type === "uav" && p.z > 0.3;
        return (
          <g
            key={id}
            className="m-robot"
            transform={`translate(${SX(p.x)} ${SY(p.y)})`}
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              onRobotClick?.(id, e);
            }}
            opacity={s.online ? 1 : 0.45}
          >
            {airborne && <ellipse rx={r * 0.9} ry={r * 0.9} fill="rgba(0,0,0,0.35)" transform={`translate(${r * 0.25} ${r * 0.35})`} />}
            {sel && <circle r={r * 1.9} fill="color-mix(in srgb, var(--accent) 14%, transparent)" stroke="var(--accent)" strokeWidth={2 * unit} />}
            <circle r={r * 1.32} fill="none" stroke={ring} strokeWidth={(mode === "idle" ? 1.2 : 2.2) * unit} opacity={mode === "idle" ? 0.55 : 0.95} className={mode === "emergency_stop" ? "m-estop" : undefined} />
            <g transform={`rotate(${(-p.yaw * 180) / Math.PI})`}>
              <Glyph type={type} r={r} color={color} />
            </g>
            {mode === "emergency_stop" && (
              <text y={r * 0.25} textAnchor="middle" fontSize={r * 0.9} fontWeight={800} fill="#fff" style={{ pointerEvents: "none" }}>
                !
              </text>
            )}
            {labels && (
              <text
                className="m-robot-label"
                x={labelPos[id]?.dx ?? 0}
                y={labelPos[id]?.dy ?? -r * 1.62}
                textAnchor={labelPos[id]?.anchor ?? "middle"}
                fontSize={11 * unit}
                strokeWidth={3 * unit}
                fill={sel ? "var(--text)" : `color-mix(in srgb, ${color} 72%, var(--text))`}
              >
                {robot.name}
                {airborne ? ` · ${p.z.toFixed(0)} m` : ""}
              </text>
            )}
          </g>
        );
      })}
    </g>
  );
});
