"""
scripts/generate_token.py

Issue a JWT for the MCP server.

Usage:
    python -m scripts.generate_token                       # 24h admin token
    python -m scripts.generate_token --user alice --hours 8
"""
from __future__ import annotations

import argparse

from nayantra.mcp.auth import create_token


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an MCP JWT token")
    parser.add_argument("--user", default="admin", help="Username embedded in the token")
    parser.add_argument("--subject", default="admin", help="JWT subject claim")
    parser.add_argument("--hours", type=int, default=24, help="Token lifetime in hours")
    args = parser.parse_args()

    token = create_token(subject=args.subject, username=args.user, hours=args.hours)
    print(token)


if __name__ == "__main__":
    main()
