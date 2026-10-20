import { useId } from "react";
import { LOGO } from "./logoData";

const [VX, VY, VW, VH] = LOGO.viewBox;

function Gradient({ id, g }: { id: string; g: typeof LOGO.swooshGradient }) {
  return (
    <linearGradient id={id} gradientUnits="userSpaceOnUse" x1={g.x1} y1={g.y1} x2={g.x2} y2={g.y2}>
      {g.stops.map(([o, c]) => <stop key={o} offset={o} stopColor={c} />)}
    </linearGradient>
  );
}

/** The Nayantra mark: an N with its orbit swoosh and a star. The N follows the theme (white on dark, ink on
 *  light) through --logo-ink; the blue-to-violet swoosh and star deepen slightly on light (.logo-color).
 *  `live` animates it while the assistant works: a glint runs along the orbit and the star twinkles.
 *  `size` is the rendered width in px. */
export function Logo({ size = 28, live = false, title = "Nayantra" }: { size?: number; live?: boolean; title?: string }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  return (
    <svg
      className={`logo${live ? " live" : ""}`}
      viewBox={`${VX} ${VY} ${VW} ${VH}`}
      width={size}
      height={(size * VH) / VW}
      role="img"
      aria-label={title}
    >
      <defs>
        <Gradient id={`${uid}s`} g={LOGO.swooshGradient} />
        <Gradient id={`${uid}t`} g={LOGO.starGradient} />
        {live && (
          <>
            <clipPath id={`${uid}c`}><path d={LOGO.swoosh} /></clipPath>
            <linearGradient id={`${uid}g`} x1="0" x2="1" y1="0" y2="0">
              <stop offset="0" stopColor="#fff" stopOpacity="0" />
              <stop offset="0.5" stopColor="#fff" stopOpacity="0.95" />
              <stop offset="1" stopColor="#fff" stopOpacity="0" />
            </linearGradient>
          </>
        )}
      </defs>
      <g className="logo-color">
        <path className="logo-swoosh" d={LOGO.swoosh} fill={`url(#${uid}s)`} />
        {live && (
          <g clipPath={`url(#${uid}c)`}>
            <rect className="logo-glint" x={VX - 80} y={VY} width="80" height={VH} fill={`url(#${uid}g)`} />
          </g>
        )}
        <path className="logo-star" d={LOGO.star} fill={`url(#${uid}t)`} />
      </g>
      <path className="logo-n" d={LOGO.n} />
    </svg>
  );
}
