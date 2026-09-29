import { useWorld } from "./world";

// One WebSocket to the core; reconnects with backoff and re-syncs from the
// snapshot the server sends on every (re)connect.
let socket: WebSocket | null = null;
let retry = 0;
let pingTimer: number | undefined;

export function connectWorld() {
  if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) return;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/api/v1/ws`);
  socket = ws;
  const store = useWorld.getState();

  ws.onopen = () => {
    retry = 0;
    store.setConnected(true);
    window.clearInterval(pingTimer);
    pingTimer = window.setInterval(() => ws.readyState === WebSocket.OPEN && ws.send('{"type":"ping"}'), 20000);
  };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data as string) as { type: string; data: unknown };
      useWorld.getState().applyMessage(msg.type, msg.data);
    } catch (err) {
      console.error("bad message", err);
    }
  };
  ws.onclose = () => {
    useWorld.getState().setConnected(false);
    window.clearInterval(pingTimer);
    if (socket !== ws) return;
    const delay = Math.min(10000, 500 * 2 ** retry++);
    window.setTimeout(connectWorld, delay);
  };
  ws.onerror = () => ws.close();
}
