# Contributing to Nayantra

Thanks for your interest in contributing. Nayantra is an LLM-powered robot
navigation framework built on Open-RMF, MCP, ROS 2, and Isaac Sim — there are
plenty of areas to help with, from adding new MCP tools to expanding the
Isaac Sim integration to documentation.

## Ways to contribute

- **Report a bug**: open an issue with steps to reproduce and your environment.
- **Propose a feature**: open an issue *before* writing the code so we can
  agree on scope.
- **Improve docs**: typos, clearer explanations, new diagrams — all welcome.
- **Add an MCP tool**: see the "Extending the System" section in
  [docs/architecture.md](docs/architecture.md).
- **Add an LLM provider**: see the same doc.

## Development setup

Nayantra is designed so the whole stack runs on a laptop with no GPU, no real
robot, and no Open-RMF server — that's the **stub-everything** mode.

```bash
git clone https://github.com/shashankbr27/nayantra.git
cd nayantra

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -e ".[dev]"

cp config/.env.example config/.env
# Set DEBUG_MODE=true to enable stub backends — no external services needed.
```

Run the test suite:

```bash
pytest tests/ -v --cov=nayantra
```

Linting and types:

```bash
ruff check nayantra/ tests/
ruff format nayantra/ tests/
mypy nayantra/
```

## Branching policy

Nayantra uses a simple two-track branching model:

| Branch | Purpose | Who pushes? |
|---|---|---|
| `main` | Stable, release-ready code. **Protected.** | Nobody pushes directly — changes land **only** via reviewed pull request. |
| `dev` | Active integration branch where new work is staged before promotion to `main`. | Maintainer (`shashankbr27`) pushes here directly; external contributors target it via PR. |
| `<your-name>/<topic>` | One branch per contributor per change. | You — created off `dev`, opened as a PR back into `dev`. |

**Examples of contributor branch names**

- `alice/zenoh-tls-mutual-auth`
- `bob/fix-sse-buffer-flush`
- `carol/docs-isaac-sim-quickstart`

Please prefix the branch with your GitHub username so it's obvious whose
work-in-progress it is. Don't push to anyone else's named branch without
asking.

## Pull request workflow

1. **Open an issue first** for any non-trivial change — saves you from
   building something we'd reject on scope.
2. Fork the repo (or, if you have write access, create a branch
   `<your-name>/<topic>` directly off `dev`).
3. **Target your PR at `dev`, never at `main`.** `main` only receives
   merges from `dev` after a release candidate has been validated.
4. Keep PRs **focused**. One logical change per PR.
5. **Add tests**. New tools need a test in `tests/test_mcp_server.py`. New agent
   behaviour needs a test in `tests/test_agent.py`.
6. Run `ruff check` and `pytest` locally before pushing.
7. PR description should explain **why**, not just what. Link the issue.
8. At least one maintainer review is required for `dev` → `main` and for
   any external PR into `dev`.
9. Be patient on review — robotics safety matters, so we err on the side
   of careful review.

## Coding conventions

- Python 3.11+. Use modern syntax (`match`, `|` unions, `from __future__`
  imports where they help).
- Type-hint everything. `mypy` runs in `--strict=false` for now but new
  code should be strict-clean.
- Async-first. Anything touching I/O should be `async def`.
- Logger names follow the `nayantra.<subsystem>` convention.
- Configuration goes through `nayantra/config.py` and `.env` — no hardcoded
  hosts, ports, or model names.
- Don't add comments that just restate the code. Comment the *why*, not
  the *what*.

## Adding a new MCP tool

```python
# nayantra/mcp/tools.py
@_tool({
    "name": "my_new_tool",
    "description": "What it does, in one line.",
    "parameters": {
        "param_a": {"type": "string", "description": "..."},
    },
})
async def _my_new_tool(client: OpenRMFClient, params: dict) -> Any:
    return await client.my_new_method(params["param_a"])
```

Then:
1. Add the matching method (and a `DEBUG_MODE` stub) in
   `nayantra/rmf_client/client.py`.
2. Add a test in `tests/test_mcp_server.py`.

## Reporting security issues

Do **not** open a public issue for security vulnerabilities. Email the
maintainer or use GitHub private vulnerability reporting — see
[SECURITY.md](SECURITY.md) for the full responsible-disclosure process.

## Licence

By contributing, you agree that your contributions will be licensed under
the [Apache License 2.0](LICENSE).
