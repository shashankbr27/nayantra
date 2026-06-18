#!/usr/bin/env python3
"""
slurm/webrtc_relay.py — TCP+UDP port relay for Isaac Sim WebRTC streaming.

Runs on the jump host (artgarage). The laptop's Isaac Sim WebRTC Streaming
Client connects to artgarage (172.25.60.80); this relay forwards everything
to the GPU node where Isaac Sim actually runs.

    laptop ──TCP 49100 / UDP 47998-48000──> artgarage ──relay──> gpu02

Usage (in tmux on artgarage):
    python3 ~/webrtc_relay.py --target gpu02
No sudo needed — all ports are unprivileged.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s relay %(message)s")
log = logging.getLogger("relay")

TCP_PORTS = [49100]
UDP_PORTS = [47998, 47999, 48000]


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def tcp_relay(listen_port: int, target: str, target_port: int) -> None:
    async def handle(client_r, client_w):
        peer = client_w.get_extra_info("peername")
        try:
            srv_r, srv_w = await asyncio.open_connection(target, target_port)
        except Exception as exc:
            log.warning(f"TCP {listen_port}: cannot reach {target}:{target_port}: {exc}")
            client_w.close()
            return
        log.info(f"TCP {listen_port}: {peer} <-> {target}:{target_port}")
        await asyncio.gather(pipe(client_r, srv_w), pipe(srv_r, client_w))

    server = await asyncio.start_server(handle, "0.0.0.0", listen_port)
    log.info(f"TCP listening on 0.0.0.0:{listen_port} -> {target}:{target_port}")
    async with server:
        await server.serve_forever()


class UdpFront(asyncio.DatagramProtocol):
    """Listens for laptop packets; one back-channel socket per client."""

    def __init__(self, target: str, port: int, loop: asyncio.AbstractEventLoop):
        self.target, self.port, self.loop = target, port, loop
        self.transport: asyncio.DatagramTransport | None = None
        self.backs: dict[tuple, asyncio.DatagramTransport] = {}

    def connection_made(self, transport):
        self.transport = transport
        log.info(f"UDP listening on 0.0.0.0:{self.port} -> {self.target}:{self.port}")

    def datagram_received(self, data, addr):
        back = self.backs.get(addr)
        if back is None or back.is_closing():
            task = self.loop.create_task(self._make_back(addr, data))
            task.add_done_callback(lambda t: t.exception())  # surface errors in log
            return
        back.sendto(data)

    async def _make_back(self, addr, first_packet):
        front = self

        class Back(asyncio.DatagramProtocol):
            def datagram_received(self, data, _src):
                if front.transport and not front.transport.is_closing():
                    front.transport.sendto(data, addr)

            def error_received(self, exc):
                log.warning(f"UDP back-channel error for {addr}: {exc}")

        transport, _ = await self.loop.create_datagram_endpoint(
            Back, remote_addr=(self.target, self.port)
        )
        self.backs[addr] = transport
        transport.sendto(first_packet)
        log.info(f"UDP {self.port}: new client {addr}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="gpu02")
    args = ap.parse_args()

    loop = asyncio.get_running_loop()
    tasks = [asyncio.create_task(tcp_relay(p, args.target, p)) for p in TCP_PORTS]
    for p in UDP_PORTS:
        await loop.create_datagram_endpoint(
            lambda p=p: UdpFront(args.target, p, loop), local_addr=("0.0.0.0", p)
        )
    log.info(f"relay up: TCP {TCP_PORTS} UDP {UDP_PORTS} -> {args.target}")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
