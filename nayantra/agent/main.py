"""
nayantra/agent/main.py

Command-line interface for Nayantra.

Usage:
    python -m nayantra.agent.main                     # Interactive REPL
    python -m nayantra.agent.main "list all robots"   # Single command
    python -m nayantra.agent.main --stream "..."      # Stream step-by-step events
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from nayantra.agent.agent import RMFAgent
from nayantra.config import settings

logger = logging.getLogger("nayantra.cli")


async def run_single(command: str, stream: bool = False) -> int:
    agent = RMFAgent()
    try:
        if stream:
            async for chunk in agent.stream_run(command):
                sys.stdout.write(chunk)
                sys.stdout.flush()
            return 0

        mission = await agent.run(command)
        print(f"\n{mission.summary}")
        return 0 if mission.success else 1
    finally:
        await agent.close()


async def run_repl() -> int:
    agent = RMFAgent()
    print("Nayantra — type 'exit' to quit")
    try:
        while True:
            try:
                command = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not command or command.lower() in ("exit", "quit"):
                break
            try:
                mission = await agent.run(command)
                print(f"\n{mission.summary}")
            except Exception as exc:
                logger.exception("Mission failed")
                print(f"Error: {exc}")
    finally:
        await agent.close()
    return 0


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOGGING_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    parser = argparse.ArgumentParser(description="Nayantra CLI")
    parser.add_argument("command", nargs="*", help="Command to execute (omit for REPL)")
    parser.add_argument("--stream", action="store_true", help="Stream SSE events to stdout")
    args = parser.parse_args()

    if args.command:
        sys.exit(asyncio.run(run_single(" ".join(args.command), stream=args.stream)))
    else:
        sys.exit(asyncio.run(run_repl()))


if __name__ == "__main__":
    main()
