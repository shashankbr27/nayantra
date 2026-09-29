// Mirrors nayantra/core/models.py — the single source of truth is the backend.

export type RobotType = "ugv" | "uav" | "quadruped" | "humanoid" | "other";
export type Layer = "ground" | "air";
export type RobotMode =
  | "offline" | "idle" | "moving" | "waiting" | "paused" | "charging" | "acting" | "error" | "emergency_stop";
export type TaskStatus =
  | "queued" | "assigned" | "planning" | "executing" | "paused" | "waiting_for_traffic"
  | "waiting_for_robot" | "completed" | "failed" | "cancelled";
export type TaskType =
  | "navigate" | "delivery" | "patrol" | "inspect" | "charge" | "dock" | "takeoff" | "land" | "make_way";
export type Severity = "info" | "warning" | "error" | "critical";
export type Point2 = [number, number];

export interface Pose { x: number; y: number; z: number; yaw: number }
export interface Bounds { min_x: number; min_y: number; max_x: number; max_y: number }

export interface MapImage {
  url: string; resolution: number; origin: number[]; width_px: number; height_px: number; opacity: number;
}
export interface Waypoint {
  id: string; map_id: string; name: string; x: number; y: number; z: number; yaw: number;
  type: string; layer: Layer; aliases: string[]; metadata: Record<string, unknown>;
}
export interface Lane {
  id: string; map_id: string; from_id: string; to_id: string; bidirectional: boolean;
  speed_limit: number | null; width: number | null; layer: Layer; altitude: number | null;
  closed: boolean; name: string; metadata: Record<string, unknown>;
}
export interface Zone {
  id: string; map_id: string; name: string; type: string; polygon: Point2[];
  speed_limit: number | null; allowed_fleets: string[]; layer: Layer | null; metadata: Record<string, unknown>;
}
export interface MapDetail {
  id: string; name: string; description: string; frame: string; bounds: Bounds;
  walls: Point2[][]; obstacles: Point2[][]; image: MapImage | null; metadata: Record<string, unknown>;
  waypoints: Waypoint[]; lanes: Lane[]; zones: Zone[];
}

export interface Footprint { shape: "circle" | "rectangle"; radius: number; length: number | null; width: number | null }
export interface MotionLimits {
  max_speed: number; max_accel: number; max_decel: number; max_yaw_rate: number;
  max_vertical_speed: number | null; cruise_altitude: number | null;
}
export interface BatteryPolicy {
  capacity_wh: number | null; min_pct: number; recharge_pct: number; charged_pct: number;
  charge_rate_pct_per_min: number; drain_pct_per_min_idle: number; drain_pct_per_m: number;
}
export interface Communication { protocol: string; endpoint: string; namespace: string; params: Record<string, unknown> }

export interface FleetStats {
  total: number; online: number; busy: number; idle: number; charging: number; error: number; offline: number; utilization: number;
}
export interface Fleet {
  id: string; name: string; robot_type: RobotType; description: string; color: string; capabilities: string[];
  navigation: { stack: string; planner: string; controller: string; localization: string };
  footprint: Footprint; limits: MotionLimits; battery: BatteryPolicy; charging_required: boolean;
  map_ids: string[]; layer: Layer; communication: Communication; priority: number; config: Record<string, unknown>;
  robots: string[]; stats: FleetStats;
}

export interface RobotHealth {
  cpu_pct: number | null; gpu_pct: number | null; temperature_c: number | null; network: string | null;
  latency_ms: number | null; localization: string | null; navigation: string | null; sensors: Record<string, string>;
}
export interface RobotState {
  robot_id: string; fleet_id: string; map_id: string | null; online: boolean;
  connection: "disconnected" | "connecting" | "connected" | "error"; connection_detail: string;
  mode: RobotMode; pose: Pose; velocity: { linear: number; angular: number; vertical: number };
  battery_pct: number | null; charging: boolean; e_stop: boolean; current_task_id: string | null; queue: string[];
  current_waypoint: string | null; destination: string | null; route: string[]; path: Point2[];
  route_progress: number | null; eta_s: number | null; status_reason: string; health: RobotHealth; adapter: string;
  updated_at: number;
}
export interface Check { name: string; ok: boolean; detail: string; required: boolean }
export interface Robot {
  id: string; name: string; fleet_id: string; manufacturer: string; model: string; robot_type: RobotType | null;
  capabilities: string[] | null; enabled: boolean; metadata: Record<string, unknown>;
  navigation: { stack: string | null; namespace: string; map_id: string | null; frame: string; base_frame: string; odom_source: string; localization: string | null; sensors: string[] };
  physical: Record<string, unknown> & { payload_kg?: number | null; battery_wh?: number | null };
  communication: Communication | null;
  spawn: { waypoint_id: string | null; pose: Pose | null; battery_pct: number };
  effective: {
    robot_type: RobotType; capabilities: string[]; map_id: string | null; layer: Layer; footprint: Footprint;
    max_speed: number; max_accel: number; payload_kg: number | null; communication: Communication;
    nav_stack: string; localization: string; priority: number; battery: BatteryPolicy;
  };
  state: RobotState | null;
  checks: Check[];
}

export interface TaskStep {
  kind: "go_to" | "action"; label: string; target: string | null; candidates: string[]; zone_id: string | null;
  action: string | null; duration_s: number | null; status: "pending" | "active" | "done" | "failed" | "skipped";
  detail: string; started_at: number | null; finished_at: number | null;
}
export interface AllocationCandidate { robot_id: string; eligible: boolean; score: number | null; reasons: string[]; estimates: Record<string, unknown> }
export interface Task {
  id: string; type: TaskType; params: Record<string, unknown>; priority: number;
  requirements: { capabilities: string[]; robot_type: string | null; fleet_id: string | null; robot_id: string | null; payload_kg: number | null };
  map_id: string | null; created_by: string;
  trace: { source: string; command: string | null; mission_id: string | null; tool: string | null; reason: string | null };
  allow_restricted: boolean; status: TaskStatus; status_reason: string; assigned_fleet: string | null; assigned_robot: string | null;
  allocation: { selected: string | null; explanation: string; candidates: AllocationCandidate[]; at: number } | null;
  steps: TaskStep[]; current_step: number; progress: number; created_at: number; assigned_at: number | null;
  started_at: number | null; completed_at: number | null; history: { ts: number; status: TaskStatus | null; message: string }[];
}

export interface Reservation { resource: string; kind: "node" | "lane"; element_id: string; map_id: string; robot_id: string; since: number }
export interface Itinerary { robot_id: string; map_id: string; route: string[]; entries: { resource: string; kind: string; element_id: string; t_enter: number; t_exit: number }[]; eta: number | null }
export interface Conflict {
  id: string; kind: string; map_id: string; robots: string[]; element_id: string; location_name: string; point: Point2;
  predicted_at_s: number | null; status: "predicted" | "active" | "resolved"; message: string;
  resolution: { action: string; robot_id: string | null; delay_s: number | null; message: string } | null;
  detected_at: number; resolved_at: number | null;
}
export interface WaitInfo { robot_id: string; resource: string; element_id: string; holder: string | null; reason: string; since: number }
export interface TrafficState {
  reservations: Reservation[]; itineraries: Itinerary[]; conflicts: Conflict[]; waits: WaitInfo[];
  precedences: { first: string; then: string; resource: string }[];
}

export interface NEvent {
  id: number; ts: number; type: string; severity: Severity; message: string;
  robot_id: string | null; fleet_id: string | null; task_id: string | null; map_id: string | null; data: Record<string, unknown>;
}
export interface Alert {
  id: string; key: string; severity: Severity; title: string; message: string; robot_id: string | null; task_id: string | null;
  status: "active" | "acknowledged" | "resolved"; raised_at: number; updated_at: number;
}
export interface PendingAction {
  id: string; kind: string; summary: string; detail: string; params: Record<string, unknown>; requested_by: string;
  trace: Task["trace"]; status: "pending" | "confirmed" | "rejected" | "expired" | "executed" | "failed";
  result: unknown; created_at: number; expires_at: number; resolved_at: number | null;
}
export interface SimStatus { running: boolean; speed: number; sim_time_s: number; environment: string; engine: string; robots: string[] }

export interface World {
  version: string; maps: MapDetail[]; fleets: Fleet[]; robots: Robot[]; tasks: Task[]; traffic: TrafficState;
  events: NEvent[]; alerts: Alert[]; confirmations: PendingAction[]; sim: SimStatus;
}

export interface Meta {
  robot_types: RobotType[]; capabilities: Record<string, string>; protocols: string[]; implemented_protocols: string[];
  nav_stacks: string[]; localization: string[]; waypoint_types: string[]; zone_types: string[];
  task_types: Record<string, { label: string; params: Record<string, { type: string; required: boolean; default?: unknown }> }>;
  robot_type_defaults: Record<string, { capabilities: string[]; max_speed: number; radius: number; layer: Layer }>;
}
