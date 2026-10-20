import { useEffect, useState } from "react";
import type { ChatStep } from "../store/chat";
import { Logo } from "./Logo";

/** The Nayantra logo in motion while the assistant works: a glint runs along its orbit and the star
 *  twinkles (see .logo.live in styles.css). It settles into the static logo when the answer arrives. */
export function ThinkingIcon({ size = 30 }: { size?: number }) {
  return <Logo size={size} live title="Working" />;
}

/** What the agent is doing right now, from the events we actually receive (never invented). */
export function phaseOf(steps: ChatStep[]): string {
  const running = [...steps].reverse().find((s) => s.status === undefined);
  if (running) return `Running ${running.tool}`;
  return steps.length ? "Writing the answer" : "Thinking";
}

/** "Thinking · 4s" line shown next to the animated mark until the answer arrives. */
export function ThinkingLine({ since, steps }: { since: number; steps: ChatStep[] }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(t);
  }, []);
  const secs = Math.max(0, Math.floor((now - since) / 1000));
  return (
    <div className="thinking-line" role="status" aria-live="polite">
      <span className="shimmer">{phaseOf(steps)}</span>
      <span className="dots" aria-hidden><i /><i /><i /></span>
      {secs >= 3 && <span className="elapsed num">{secs}s</span>}
    </div>
  );
}
