import { create } from "zustand";
import { api } from "../api/client";
import type {
  Alert, Conflict, Fleet, Lane, MapDetail, NEvent, PendingAction, Point2, Robot, RobotState,
  SimStatus, Task, TrafficState, Waypoint, World, Zone,
} from "../api/types";

export type Page = "operations" | "fleets" | "robots" | "tasks" | "map" | "system";
export type LayerKey =
  | "image" | "walls" | "zones" | "lanes" | "airLanes" | "waypoints" | "labels" | "robots"
  | "paths" | "trails" | "reservations" | "conflicts" | "grid";

export interface Toast { id: number; kind: "info" | "success" | "warning" | "error"; text: string }

const EMPTY_TRAFFIC: TrafficState = { reservations: [], itineraries: [], conflicts: [], waits: [], precedences: [] };
const TRAIL_POINTS = 90;

function readPref<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(`nayantra.${key}`);
    return v ? (JSON.parse(v) as T) : fallback;
  } catch {
    return fallback;
  }
}
export function writePref(key: string, value: unknown) {
  try {
    localStorage.setItem(`nayantra.${key}`, JSON.stringify(value));
  } catch {
    /* storage unavailable — preference just isn't remembered */
  }
}

interface WorldStore {
  connected: boolean;
  loaded: boolean;
  version: string;
  maps: Record<string, MapDetail>;
  fleets: Record<string, Fleet>;
  robots: Record<string, Robot>;
  states: Record<string, RobotState>;
  tasks: Record<string, Task>;
  traffic: TrafficState;
  events: NEvent[];
  alerts: Record<string, Alert>;
  confirmations: Record<string, PendingAction>;
  sim: SimStatus | null;
  trails: Record<string, Point2[]>;
  flashes: { conflict: Conflict; at: number }[];

  page: Page;
  activeMapId: string | null;
  selectedRobots: string[];
  selectedWaypoint: string | null;
  selectedTask: string | null;
  selectedFleet: string | null;
  goTo: boolean;
  layers: Record<LayerKey, boolean>;
  theme: "dark" | "light";
  toasts: Toast[];
  wizardOpen: boolean;
  composer: { open: boolean; preset?: Partial<{ type: string; robotIds: string[]; destination: string }> };

  applySnapshot: (w: World) => void;
  applyMessage: (type: string, data: unknown) => void;
  setConnected: (v: boolean) => void;
  setPage: (p: Page) => void;
  setActiveMap: (id: string) => void;
  selectRobot: (id: string | null, additive?: boolean) => void;
  setSelectedRobots: (ids: string[]) => void;
  selectWaypoint: (id: string | null) => void;
  selectTask: (id: string | null) => void;
  selectFleet: (id: string | null) => void;
  setGoTo: (v: boolean) => void;
  toggleLayer: (k: LayerKey) => void;
  setTheme: (t: "dark" | "light") => void;
  toast: (kind: Toast["kind"], text: string) => void;
  dismissToast: (id: number) => void;
  openWizard: (v: boolean) => void;
  openComposer: (preset?: WorldStore["composer"]["preset"]) => void;
  closeComposer: () => void;
  refreshRobot: (id: string) => Promise<void>;
  refreshFleet: (id: string) => Promise<void>;
}

let toastSeq = 1;

function byId<T extends { id: string }>(items: T[]): Record<string, T> {
  const out: Record<string, T> = {};
  for (const it of items) out[it.id] = it;
  return out;
}

function upsertIn<T extends { id: string }>(list: T[], item: T): T[] {
  const i = list.findIndex((x) => x.id === item.id);
  if (i < 0) return [...list, item];
  const copy = list.slice();
  copy[i] = item;
  return copy;
}

export const useWorld = create<WorldStore>((set, get) => ({
  connected: false,
  loaded: false,
  version: "",
  maps: {},
  fleets: {},
  robots: {},
  states: {},
  tasks: {},
  traffic: EMPTY_TRAFFIC,
  events: [],
  alerts: {},
  confirmations: {},
  sim: null,
  trails: {},
  flashes: [],

  page: (window.location.hash.replace("#/", "") as Page) || "operations",
  activeMapId: readPref<string | null>("map", null),
  selectedRobots: [],
  selectedWaypoint: null,
  selectedTask: null,
  selectedFleet: null,
  goTo: false,
  layers: readPref<Record<LayerKey, boolean>>("layers", {
    image: true, walls: true, zones: true, lanes: true, airLanes: true, waypoints: true, labels: true,
    robots: true, paths: true, trails: true, reservations: true, conflicts: true, grid: true,
  }),
  theme: readPref<"dark" | "light">("theme", "dark"),
  toasts: [],
  wizardOpen: false,
  composer: { open: false },

  applySnapshot: (w) => {
    const states: Record<string, RobotState> = {};
    for (const r of w.robots) if (r.state) states[r.id] = r.state;
    const maps = byId(w.maps);
    let active = get().activeMapId;
    if (!active || !maps[active]) {
      const counts = new Map<string, number>();
      for (const r of w.robots) {
        const m = r.effective.map_id;
        if (m) counts.set(m, (counts.get(m) ?? 0) + 1);
      }
      active = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? w.maps[0]?.id ?? null;
    }
    set({
      loaded: true,
      version: w.version,
      maps,
      fleets: byId(w.fleets),
      robots: byId(w.robots),
      states,
      tasks: byId(w.tasks),
      traffic: w.traffic,
      events: w.events,
      alerts: byId(w.alerts),
      confirmations: byId(w.confirmations),
      sim: w.sim,
      activeMapId: active,
    });
  },

  applyMessage: (type, data) => {
    const s = get();
    switch (type) {
      case "snapshot":
        s.applySnapshot(data as World);
        return;
      case "robots": {
        const list = data as RobotState[];
        const states = { ...s.states };
        const trails = { ...s.trails };
        for (const st of list) {
          states[st.robot_id] = st;
          const t = trails[st.robot_id] ?? [];
          const last = t[t.length - 1];
          const p: Point2 = [st.pose.x, st.pose.y];
          if (!last || Math.hypot(last[0] - p[0], last[1] - p[1]) > 0.25) {
            trails[st.robot_id] = [...t.slice(-(TRAIL_POINTS - 1)), p];
          }
        }
        set({ states, trails });
        return;
      }
      case "traffic":
        set({ traffic: data as TrafficState });
        return;
      case "task": {
        const t = data as Task;
        set({ tasks: { ...s.tasks, [t.id]: t } });
        return;
      }
      case "task_progress": {
        const d = data as { id: string; progress: number; current_step: number };
        const t = s.tasks[d.id];
        if (t) set({ tasks: { ...s.tasks, [d.id]: { ...t, progress: d.progress, current_step: d.current_step } } });
        return;
      }
      case "event": {
        const e = data as NEvent;
        set({ events: [e, ...s.events].slice(0, 400) });
        return;
      }
      case "alert": {
        const a = data as Alert;
        set({ alerts: { ...s.alerts, [a.id]: a } });
        return;
      }
      case "conflict": {
        const c = data as Conflict;
        const now = Date.now();
        set({ flashes: [...s.flashes.filter((f) => now - f.at < 12000), { conflict: c, at: now }] });
        return;
      }
      case "confirmation": {
        const c = data as PendingAction;
        set({ confirmations: { ...s.confirmations, [c.id]: c } });
        return;
      }
      case "sim":
        set({ sim: data as SimStatus });
        return;
      case "robot_removed": {
        const { robot_id } = data as { robot_id: string };
        const robots = { ...s.robots };
        const states = { ...s.states };
        delete robots[robot_id];
        delete states[robot_id];
        set({ robots, states, selectedRobots: s.selectedRobots.filter((r) => r !== robot_id) });
        return;
      }
      case "registry":
        applyRegistry(data as { kind: string; op: string; id: string; item: Record<string, unknown> });
        return;
      default:
        return;
    }
  },

  setConnected: (v) => set({ connected: v }),
  setPage: (p) => {
    window.location.hash = `#/${p}`;
    set({ page: p });
  },
  setActiveMap: (id) => {
    writePref("map", id);
    set({ activeMapId: id, selectedWaypoint: null });
  },
  selectRobot: (id, additive = false) => {
    if (id === null) return set({ selectedRobots: [], goTo: false });
    const cur = get().selectedRobots;
    if (additive) {
      set({ selectedRobots: cur.includes(id) ? cur.filter((r) => r !== id) : [...cur, id] });
    } else {
      set({ selectedRobots: [id], selectedWaypoint: null });
    }
  },
  setSelectedRobots: (ids) => set({ selectedRobots: ids }),
  selectWaypoint: (id) => set({ selectedWaypoint: id }),
  selectTask: (id) => set({ selectedTask: id }),
  selectFleet: (id) => set({ selectedFleet: id }),
  setGoTo: (v) => set({ goTo: v }),
  toggleLayer: (k) => {
    const layers = { ...get().layers, [k]: !get().layers[k] };
    writePref("layers", layers);
    set({ layers });
  },
  setTheme: (t) => {
    writePref("theme", t);
    set({ theme: t });
  },
  toast: (kind, text) => {
    const id = toastSeq++;
    set({ toasts: [...get().toasts, { id, kind, text }].slice(-5) });
    window.setTimeout(() => get().dismissToast(id), kind === "error" ? 8000 : 4500);
  },
  dismissToast: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
  openWizard: (v) => set({ wizardOpen: v }),
  openComposer: (preset) => set({ composer: { open: true, preset } }),
  closeComposer: () => set({ composer: { open: false } }),
  refreshRobot: async (id) => {
    try {
      const r = await api.get<Robot>(`/robots/${id}`);
      set({ robots: { ...get().robots, [id]: r }, states: r.state ? { ...get().states, [id]: r.state } : get().states });
    } catch {
      /* removed meanwhile */
    }
  },
  refreshFleet: async (id) => {
    try {
      const f = await api.get<Fleet>(`/fleets/${id}`);
      set({ fleets: { ...get().fleets, [id]: f } });
    } catch {
      /* removed meanwhile */
    }
  },
}));

function applyRegistry(msg: { kind: string; op: string; id: string; item: Record<string, unknown> }) {
  const s = useWorld.getState();
  const { kind, op, id, item } = msg;
  const set = useWorld.setState;
  if (kind === "map") {
    if (op === "delete") {
      const maps = { ...s.maps };
      delete maps[id];
      set({ maps });
      return;
    }
    const prev = s.maps[id];
    const merged = { ...prev, ...(item as unknown as Partial<MapDetail>) } as MapDetail;
    set({ maps: { ...s.maps, [id]: { ...merged, waypoints: prev?.waypoints ?? [], lanes: prev?.lanes ?? [], zones: prev?.zones ?? [] } } });
    return;
  }
  if (kind === "waypoint" || kind === "lane" || kind === "zone") {
    const mapId = item.map_id as string;
    const m = s.maps[mapId];
    if (!m) return;
    const key = kind === "waypoint" ? "waypoints" : kind === "lane" ? "lanes" : "zones";
    const list = m[key] as unknown as { id: string }[];
    const next = op === "delete" ? list.filter((x) => x.id !== id) : upsertIn(list, item as unknown as { id: string });
    set({ maps: { ...s.maps, [mapId]: { ...m, [key]: next as unknown as Waypoint[] & Lane[] & Zone[] } } });
    if (op === "delete" && kind === "waypoint" && s.selectedWaypoint === id) set({ selectedWaypoint: null });
    return;
  }
  if (kind === "fleet") {
    if (op === "delete") {
      const fleets = { ...s.fleets };
      delete fleets[id];
      set({ fleets });
    } else void s.refreshFleet(id);
    return;
  }
  if (kind === "robot") {
    if (op === "delete") {
      const robots = { ...s.robots };
      delete robots[id];
      set({ robots });
    } else {
      void s.refreshRobot(id);
      const fleetId = item.fleet_id as string;
      if (fleetId) void s.refreshFleet(fleetId);
    }
  }
}
