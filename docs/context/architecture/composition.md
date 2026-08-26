---
title: Application composition and deployment roles
status: shipped
sources:
  - src/treg/bootstrap.py
  - src/treg/routers/admin.py
  - src/treg/routers/web.py
  - scripts/dump_surface.py
related:
  - architecture/import-boundaries.md
  - interface/api.md
  - architecture/mcp-oauth.md
  - ops/deploy.md
---

# Application composition

`bootstrap.create_app(role)` is the FastAPI composition root. `api.py` hosts the ordered route table;
the Catalog, web, and admin modules define concern-specific `APIRouter` blocks that `api.py` appends
at their legacy registration points. It then calls the factory once at EOF so the deployed and
documented `treg.api:app` import path remains the default `all` role.

The factory owns concrete assembly: the three core pure-ASGI middleware registrations, the conditional
V2 path normalizer, five exception handlers, static mounts, optional MCP mounts and lifespans,
GET-to-HEAD widening, the OpenAPI wrapper that hides
implied HEAD operations, shared HTTP client creation, startup work, shutdown drains, and the Ads
conversion worker. Registration order is compatibility behavior. The four stage-0 snapshots stay
byte-identical for `role="all"` unless that composition intentionally changes.

The middleware stack is `_BodyDecodeMiddleware` -> `_SecurityHeadersMiddleware` ->
`_LegacyHostRedirectMiddleware` -> routes/mounts. All three are pure ASGI. The security wrapper adds
headers at `http.response.start` with case-insensitive setdefault semantics, and the redirect wrapper
either sends the same 301/302 response as before or calls its child directly. Keeping
`BaseHTTPMiddleware.call_next()` out of this stack matters for streaming and disconnects: an MCP
client may close while its stateless transport terminates without sending a response, which is a
normal end to an already-dead connection rather than a server 500.

When the Claude connector is enabled, `NormalizeDirectoryMCPPath` is the outermost middleware. It
rewrites the exact no-slash path `/mcp/v2` to `/mcp/v2/` before route matching. This prevents an MCP
client that removes the trailing slash from falling through to the legacy `/mcp` mount.

Pure ASGI does not make a genuine missing-response defect silent. Uvicorn's
`RequestResponseCycle.run_asgi` checks an app that returns while the connection is still live, logs
`ASGI callable returned without starting response.`, and sends a 500. It skips that error only when
the protocol has already marked the client disconnected, when no response can be delivered. Response
completion also remains responsible for Starlette background tasks: the `/call` relay's
`StreamingResponse` runs `BackgroundTask(upstream_resp.aclose)` after its body, and an assertion test
pins that the shared httpx connection is released exactly once. Removing the two AnyIO memory-stream
hops changes streaming backpressure and scheduling but not interruption semantics, which the
callmatrix stream-failure case pins.

## Role manifests

Every created app exposes `app.state.role_manifest` with explicit `routes`, `background_tasks`, and
`startup_checks` lists. `tests/test_app_roles.py` pins all three lists for every role.

| Role | HTTP routes and mounts | Background tasks | Startup checks |
|---|---|---|---|
| `all` | The complete surface, including `/run`, static files, `/mcp`, `/mcp/v2`, `/call/{rest:path}`, and `/catalog/call/{rest:path}` | Ads conversion worker when enabled | DB init, provider-tool backfill, single-user bootstrap, HTTP client, both MCP lifespans |
| `dataplane` | `/call/{rest:path}` and internal `/catalog/call/{rest:path}` only; no `/run`, static files, docs, OpenAPI, or MCP | None | DB init, provider-tool backfill, HTTP client |
| `control` | Everything except the two call routes; includes `/run`, static files, `/mcp`, and `/mcp/v2` | Ads conversion worker when enabled | DB init, provider-tool backfill, single-user bootstrap, HTTP client, both MCP lifespans |

`_CONTROL_ROUTE_KEYS` and `_DATAPLANE_ROUTE_KEYS` assign every `api.router` route to exactly one
owner. App creation fails on an unclassified, stale, duplicate, or multiply-owned key, so adding a
route cannot silently expand the dataplane. Role separation is preparatory in stage 1; only the
`all` role is deployed.

When `TREG_CLAUDE_CONNECTOR_ENABLED=true`, `/mcp/v2` is mounted before `/mcp`; otherwise the parent
Starlette mount consumes the nested path. The path normalizer makes `/mcp/v2` and `/mcp/v2/` the same
V2 resource. `all_mcp_lifespans()` nests both transport lifespan
contexts because mounted ASGI application lifespans are not started automatically.

When the flag is false or missing, only `/mcp` is mounted and only its lifespan starts. The legacy
MCP surface does not depend on this flag.

## Route cloning

Each factory call must produce an independent app whose dependency overrides belong to that app.
`_include_routes` therefore shallow-clones every `APIRoute`, points its dependency override provider
at the new FastAPI instance, and rebuilds its request handler. This also avoids the internal
`_IncludedRouter` wrapper added by the current FastAPI `include_router()` implementation, which would
otherwise change route inspection and the committed surface snapshot.

`scripts.dump_surface._lifespan` records the optional MCP lifespan condition against
`treg.bootstrap._mcp`, where optional MCP composition now lives. This is a documentation-only snapshot
correction; the mounted lifespan behavior is unchanged.
