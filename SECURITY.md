# Security Policy

Nayantra drives physical robots. Security issues here can have **physical
safety consequences** — please treat them seriously and report them
responsibly.

## Supported versions

| Version | Status |
|---|---|
| 1.x (`main`) | ✅ Actively supported |
| pre-1.0      | ❌ No security fixes |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please email the maintainer:

- **Shashank BR** — *(add your contact email here, e.g. `security@example.com`)*

Or use GitHub's private vulnerability reporting:
[github.com/shashankbr27/nayantra/security/advisories/new](https://github.com/shashankbr27/nayantra/security/advisories/new)

### What to include

1. A short description of the issue and its impact.
2. Steps to reproduce (ideally a minimal proof of concept).
3. Affected versions and commit SHA if known.
4. Your suggested fix, if any.

### What to expect

- **Acknowledgement** within 5 business days.
- **Triage decision** (confirmed / not-applicable / needs-more-info) within
  10 business days.
- **Fix timeline** depends on severity. Critical issues (RCE, unauthorised
  robot control, credential exposure) are targeted for a patch within 14 days.
- **Coordinated disclosure**. We'll work with you on a disclosure date —
  usually 30–90 days after a fix is shipped, depending on severity and
  uptake of the fixed version.

We'll credit you in the advisory unless you ask to remain anonymous.

## Scope

In scope:
- Anything in the [`nayantra/`](nayantra/) tree
- Anything in [`docker/`](docker/) and [`scripts/`](scripts/)
- The Open-RMF client and MCP protocol surface
- JWT auth, CORS configuration, and any auth/authz code paths

Out of scope:
- Vulnerabilities in upstream dependencies — please report those to the
  upstream project. We will update pinned versions when they patch.
- Issues in Open-RMF itself — see [open-rmf/rmf](https://github.com/open-rmf).
- Issues in NVIDIA Isaac Sim — see NVIDIA's security policy.
- Social engineering or physical access to a deployed robot.
- DoS via legitimate API usage (rate limiting is a known gap; see
  [docs/architecture.md](docs/architecture.md)).

## Known limitations

These are intentional design trade-offs documented for transparency. They
are **not** vulnerabilities, but operators should be aware:

- **Default auth is off** (`USE_AUTH=false`) for laptop development. The
  agent emits a startup warning. Servers bind to `127.0.0.1` by default —
  override only when auth is on.
- **JWT is HS256 with a shared symmetric secret.** Production deployments
  should consider RS256 with proper key management. Tracked as a roadmap
  item.
- **No LLM token-budget enforcement.** A buggy or adversarial command can
  rack up arbitrary LLM API costs. Set conservative API-key rate limits
  upstream until in-product budgeting lands.
- **CORS allowlist** is conservative by default (`localhost:3000/8080`).
  Configure `CORS_ORIGINS` in `.env` to match your front-end.

## Hardening checklist (for operators)

Before exposing Nayantra beyond a development laptop:

- [ ] Set `USE_AUTH=true` and a strong random `JWT_SECRET`
      (`python -c "import secrets; print(secrets.token_urlsafe(48))"`)
- [ ] Set `CORS_ORIGINS` to your real front-end origins (no `*`)
- [ ] Set `MCP_SERVER_HOST` / `AGENT_API_HOST` only as wide as needed
- [ ] Change the Grafana admin password in `docker/docker-compose.yml`
- [ ] Put the agent API behind a reverse proxy with TLS
- [ ] Rate-limit at the proxy layer
- [ ] Rotate JWTs regularly (issued via `nayantra-token`)
