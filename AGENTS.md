# AGENTS.md — guide for AI coding agents

This file orients an AI agent (Claude Code, Codex, Cursor, …) working in this repository.
`CLAUDE.md` defers to this file; do not add project guidance there.

## The product

**The tool catalog for your agent.** Point an agent at one base URL with one token and it can do the
job — without owning the API keys. Two halves answer the same token, through the same `/call/`:

- **The catalog** — 2,896 curated external endpoints across 60 providers (SEO and backlinks, social
  and trends, people and company enrichment, ads, scraping). treg can serve eligible ones **on its own
  key**, metered per call from the team's prepaid balance ($1.00 free per new team). No provider signup.
- **Your own tools** — what a teammate registered: a paid API account, an OAuth connection, a vendor
  CLI, a `SKILL.md`. **A team's own key always wins over treg's, and those calls are never metered.**

The load-bearing mechanic: the caller makes the **real upstream request**, the proxy **injects the
credential server-side**, and relays the answer verbatim. We never model an upstream API, so we
survive its changes and the caller never holds a secret.

### Words to use, and words not to

One concept, one word. This was settled deliberately — mixed vocabulary is how the old framing keeps
coming back.

| Thing | Word |
|---|---|
| what an agent calls | **a tool** |
| the public half | **the catalog** |
| the team's half | **your own tools** (your keys & skills) |
| the server / deployment | **registry** — only here |

**Do not** call either half a *vault*, a *marketplace*, or *the registry*. "Vault" means safe storage,
which was the older security-led pitch; "marketplace" implies buying and sellers; "registry" is
infrastructure language that says nothing about what a user can now do.

Phrase everything as **what the agent can now do**, not what we store. The test: *"your team's shared
vault of skills and secrets"* fails it; *"2,800 tools your agent can call, plus your own"* passes.

### Do not document what is not built

An agent that believes a feature exists fails in a way nobody can debug. In particular: treg
**compares** providers of the same capability (`treg catalog search` shows them side by side with
prices) but does **not** route automatically or fail over. Choosing is the caller's. Say so wherever
the subject comes up, and change it only when the router actually ships.

## Read first

- **Design docs are the source of truth.** `docs/context/` holds one fragment per subsystem, each citing
  the source files it covers in its frontmatter (`sources:`). Before changing code, load the fragment for
  that area; `docs/context/README.md` is the generated index (source file → fragment).

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
**zero DB connections** are held while an upstream request is in flight. Dataplane writes are limited
to an enumerated allowlist — see `docs/context/architecture/` (`composition.md`,
`import-boundaries.md`, `money.md`).

### Money code

The ledger lives inside `domain/money` and is the **only** code path that moves money; balances change
only through its five entries (grant · topup · reserve · settle · release). The Stripe SDK lives only
in `infra/stripe.py`, with orchestration in `application/billing.py`; `reconcile.py` is read-only.
Everything is **integer micro-USD** — never floats, never cents. Never route a ledger write through
`audit.py`: it drops rows past its queue bound, which is right for analytics and fatal for money.
See `docs/context/architecture/money.md`.

## Working agreement

- Run `uv run --frozen pytest -q` before and after changes; keep it green (add tests for new
  behavior).
- Keep changes minimal and scoped; match the surrounding style.
- When you change a subsystem, update its `docs/context/` fragment **in the same commit as the code**.
  Before pushing, run `bash .agents/skills/tools-registry-context/scripts/drift.sh`, map changed
  sources → fragments, and update them.
- **Three files move together** or they drift: `src/treg/web/tutorial.js` (the only interactive
  source) and its two hand-kept prose mirrors, `src/treg/web/tutorial.md` and `docs/TUTORIAL.md`.
- **Agent-facing files are the product's front door**, not documentation: `src/treg/web/llms.txt` and
  `src/treg/web/skill.md` (the latter is installed into every agent by `install.sh`). Any change to
  how treg works should ask whether these need to change too.
- When `/mcp/`, `/mcp/v2/`, or shared MCP code changes, review both MCP surfaces. Preserve the
  documented differences, run the paired MCP contract tests, and update
  `docs/context/architecture/mcp-oauth.md` when the contract changes.
- **Verify UI work in a browser.** Markup that reads correctly still breaks: a headline whose CSS is
  hand-tuned to its character count, a Vue `@click` naming a view that does not exist (fails
  silently), a cached asset that never reaches anyone.
- Commits follow Conventional Commits (`feat(scope): …`, `fix: …`, `docs: …`); one logical change per
  commit. PRs should say what changed and why, and note which fragments were updated.

## Development

```bash
uv run --frozen python -m pytest -q     # the whole suite
uv run --frozen treg --help             # the CLI from this checkout
uv run --frozen python -m treg          # the server
```

- **Always `--frozen`.** Running `uv lock` / `uv sync` on an older uv rewrites `uv.lock` into an older
  format — a ~650-line diff that changes no versions. Hand-add new dependencies to the lock instead.
- **The package is split.** The base install is the **light CLI** (`httpx` + `questionary`); the
  FastAPI/DB stack is the `[server]` extra and the certificate authority is `[proxy]`. Never import a
  heavy dependency at the top of a CLI-path module (`cli.py`, `convert.py`, `skills.py`,
  `providers.py`, `localrun.py`, `shell.py`, `agents.py`, `egress.py`, `fsjail.py`) — it would
  re-bloat `pip install`.

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

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. Quick version: `uv run --frozen pytest -q`; the live dev
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

## Pointers

- `README.md` — overview and quickstart · `USAGE.md` — the full CLI reference
- `/llms.txt` — the agent-onboarding file · `/tutorial` — the interactive walkthrough
- `docs/context/` — per-subsystem design fragments (start at `foundation/charter.md`)
