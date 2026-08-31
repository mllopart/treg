# AGENTS.md — guide for AI coding agents

This file orients an AI agent (Claude Code, Codex, Cursor, …) working in this repository.

## Read first

- **Design docs are the source of truth.** `docs/context/` holds one fragment per subsystem, each citing
  the source files it covers in its frontmatter (`sources:`). Before changing code, load the fragment for
  that area; `docs/context/README.md` is the generated index (source file → fragment).
- **The charter:** tools-registry is a registry that turns a team's skills into shareable, callable tools;
  the core mechanic is a proxy that injects credentials server-side so a consumer never holds the secret.
  See `README.md`.

## Architecture

The server under `src/treg/` is a four-layer modular monolith (one process, one package; code
boundaries, not services):

- **`routers/`** — thin HTTP/MCP translation only: no business rules, no query orchestration, no
  money logic.
- **`application/`** — use-case sequencing, transaction boundaries, and compensation (`call/`,
  `signup`, `connect`, `billing`, `onboard/`). The use case opens the DB session and is the **only**
  place that commits; domain functions never commit or roll back.
- **`domain/`** — business rules testable alone: `identity`, `governance`, `connections`, `tools`,
  `catalog`, `capacity`, `money`, `referrals`. Domains do not import each other except three
  enumerated edges (`governance → identity`, `tools → connections`, `capacity → catalog` read-only);
  import-linter contracts in `pyproject.toml` enforce the matrix.
- **`infra/`** — external systems: `db`, `upstream` (faithful relay, credential injection, SSRF
  guard), `stripe`, plus crypto/ratestore/email adapters.

`bootstrap.py` is the composition root: `create_app(role="all" | "dataplane" | "control")` fixes each
role's routes, background tasks, and startup checks (snapshot-tested). `api.py` is a shrinking legacy
route-splicing surface, **not** the brain — new logic goes in application or domain, never in
`api.py`, `cli.py`, or the web layer.

Two hard rules on the call path: a hold is settled or released **exactly once** on every path, and
**zero DB connections** are held while an upstream request is in flight. Balances change only through
`domain/money`'s five entries (grant · topup · reserve · settle · release), in integer micro-USD.
Dataplane writes are limited to an enumerated allowlist — see `docs/context/architecture/`
(`composition.md`, `import-boundaries.md`, `money.md`).

## Working agreement

- Run `uv run --frozen pytest -q` before and after changes; keep it green (add tests for new
  behavior). Always `--frozen`: an older `uv` rewrites `uv.lock` into a huge no-op diff.
- Keep changes minimal and scoped; match the surrounding style.
- When you change a subsystem, update its `docs/context/` fragment in the same change.
- When `/mcp/`, `/mcp/v2/`, or shared MCP code changes, review both MCP surfaces. Preserve the
  documented differences, run the paired MCP contract tests, and update
  `docs/context/architecture/mcp-oauth.md` when the contract changes.
- Commits follow Conventional Commits (`feat(scope): …`, `fix: …`, `docs: …`); one logical change per
  commit. PRs should say what changed and why, and note which fragments were updated.

## Do not touch (without reading the fragment first)

- **The faithful-relay contract** (`src/treg/infra/upstream/relay.py`): the proxy alters only hop-by-hop headers, treg's
  own control headers, and the injected credential — never add upstream-specific modeling or buffering.
- **Security guards that look redundant on purpose**: the `expose_dev_code` double-guard (dev OTP only on
  local sqlite), the call-time SSRF check, the fail-loud missing-Fernet-key startup check, and the
  `treg run` allow-list/rlimits. Read `docs/context/architecture/` before changing any of them.

## Security awareness

- Never commit real secrets. Placeholder/demo values are obviously fake (see `.gitleaks.toml`); CI scans
  every PR. Credentials belong in `.env` (gitignored), never in code, tests, or docs.
- Read **[SECURITY.md](SECURITY.md)** for the security model and the known limitations before touching the
  proxy, the runners, auth, or secret handling.

## Local setup

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. Quick version: `uv sync && uv run pytest -q`; the live dev
stack is `scripts/dev-local.sh up` (server on `:18790`, hot-reload, own sqlite DB, email OTP shown
on-page) with a sandboxed CLI via `scripts/dev-local.sh cli <args>`.

## Things every agent should know before editing

- The server is the only brain — the CLI and the dashboard are thin clients over it. Put logic in
  application or domain modules (see Architecture above), not in `cli.py` or the web layer.
- The dashboard (`src/treg/web/index.html`) is a single-file Vue app with **no build step** — edit the
  HTML directly; there is nothing to compile.
- Schema changes are Alembic revisions in `src/treg/alembic/versions/`, portable across SQLite +
  Postgres. Startup only **verifies** the schema read-only and never writes; migrations run through
  `maintenance.py` / the release pipeline (see `docs/context/ops/deploy.md`).
- One fetch teaches you the product itself: `src/treg/web/llms.txt` (served at `/llms.txt`).
