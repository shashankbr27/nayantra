import { useCallback, useEffect, useRef, useState } from "react";
import { api, isConfirmation } from "../api/client";
import type { Point2 } from "../api/types";
import { BottomDock } from "../components/BottomDock";
import { CommandBar } from "../components/CommandBar";
import { FleetSidebar } from "../components/FleetSidebar";
import { Inspector } from "../components/Inspector";
import { ConflictFlashes, MapChrome } from "../components/MapChrome";
import { Empty } from "../components/ui";
import { type MapControl, MapView } from "../map/MapView";
import { useWorld } from "../store/world";
import { errMsg } from "../lib/format";

export function OperationsPage() {
  const s = useWorld();
  const map = s.activeMapId ? s.maps[s.activeMapId] : undefined;
  const control = useRef<MapControl | null>(null);
  const [cursor, setCursor] = useState<Point2 | null>(null);
  const [pxPerM, setPxPerM] = useState(20);

  const send = useCallback(async (robotIds: string[], waypointId: string) => {
    const { robots, toast, maps, activeMapId } = useWorld.getState();
    const wp = activeMapId ? maps[activeMapId]?.waypoints.find((w) => w.id === waypointId) : undefined;
    for (const rid of robotIds) {
      try {
        const r = await api.post(`/robots/${rid}/navigate`, { waypoint: waypointId });
        if (isConfirmation(r)) toast("warning", r.message);
        else toast("success", `${robots[rid]?.name ?? rid} → ${wp?.name ?? waypointId}`);
      } catch (e) {
        toast("error", `${robots[rid]?.name ?? rid}: ${errMsg(e)}`);
      }
    }
  }, []);

  // Stable handlers keep the memoised static map layers from re-rendering at telemetry rate.
  const onWaypointClick = useCallback(
    (id: string) => {
      const st = useWorld.getState();
      if (st.goTo && st.selectedRobots.length) {
        void send(st.selectedRobots, id);
        st.setGoTo(false);
      } else {
        st.selectRobot(null);
        st.selectWaypoint(id);
      }
    },
    [send],
  );
  const onWaypointDoubleClick = useCallback(
    (id: string) => {
      const st = useWorld.getState();
      if (st.selectedRobots.length) void send(st.selectedRobots, id);
    },
    [send],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
      const st = useWorld.getState();
      if (e.key === "Escape") {
        if (st.goTo) st.setGoTo(false);
        else {
          st.selectRobot(null);
          st.selectWaypoint(null);
        }
      }
      if ((e.key === "g" || e.key === "G") && st.selectedRobots.length) st.setGoTo(!st.goTo);
      if (e.key === "]" || e.key === "[") {
        const ids = Object.keys(st.robots).sort((a, b) => st.robots[a].name.localeCompare(st.robots[b].name));
        if (!ids.length) return;
        const cur = ids.indexOf(st.selectedRobots[0]);
        const next = ids[(cur + (e.key === "]" ? 1 : ids.length - 1)) % ids.length];
        st.selectRobot(next);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="ops">
      <FleetSidebar />
      <div className="stage">
        {map ? (
          <MapView
            map={map}
            mode="ops"
            layers={s.layers}
            robots={s.robots}
            fleets={s.fleets}
            states={s.states}
            traffic={s.traffic}
            trails={s.trails}
            flashes={s.flashes}
            selectedRobots={s.selectedRobots}
            selectedWaypoint={s.selectedWaypoint}
            crosshair={s.goTo}
            control={control}
            onCursor={setCursor}
            onScale={setPxPerM}
            onRobotClick={(id, e) => s.selectRobot(id, e.shiftKey || e.ctrlKey || e.metaKey)}
            onWaypointClick={onWaypointClick}
            onWaypointDoubleClick={onWaypointDoubleClick}
            onBackground={() => {
              if (!s.goTo) {
                s.selectRobot(null);
                s.selectWaypoint(null);
              }
            }}
          />
        ) : (
          <Empty>{s.loaded ? "No maps yet — create one in the Map editor." : "Connecting to Nayantra Core…"}</Empty>
        )}
        {map && <MapChrome control={control} cursor={cursor} pxPerM={pxPerM} />}
        <CommandBar />
        <ConflictFlashes />
      </div>
      <aside className="inspector">
        <Inspector onFocus={(x, y) => control.current?.centerOn(x, y)} />
      </aside>
      <BottomDock />
    </div>
  );
}
