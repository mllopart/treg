"""Application-wide HTTP exception adapters owned by the composition boundary."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException


# create_app supplies the call-specific compensation callback before registering these adapters.
_stamp_call_exit: Any


async def _saturated_503(request: Request, body: dict[str, Any]) -> JSONResponse:
    """Answer a typed saturation 503 (`treg_saturated` + `Retry-After: 2`) with /call/ bookkeeping.

    A saturation 503 is answered HERE, not through `_mark_treg_own_errors`, so it needs its own
    join key, row and label release. Without them the one failure mode a burst actually produces
    (#181) is the one a caller cannot report and `/calls` cannot show. `X-Treg-Error` stays off:
    the typed `treg_saturated` flag is this exit's signal, and the header is documented as the
    HTTPException handler's (interface/api.md)."""
    resp = JSONResponse({**body, "treg_saturated": True},
                        status_code=503, headers={"Retry-After": "2"})
    if request.url.path.startswith("/call/"):
        await _stamp_call_exit(request, resp, 503)
    return resp

async def _pool_saturated(request: Request, exc: PoolTimeoutError) -> JSONResponse:
    """The DB pool had no connection to give within `pool_timeout` (db.py). That is treg being
    saturated, not the caller's fault and not the provider's — so say so, typed, and fast. Before this
    handler the same condition escaped request handling and surfaced as a bare
    `500 Internal Server Error` after a 30 s wait, which an agent cannot tell from a provider bug.
    `treg_saturated` is the key a retrying client should branch on; `Retry-After` is how long to wait
    before doing so."""
    return await _saturated_503(
        request, {"detail": "treg's database pool is saturated — retry in a moment"})

async def _db_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """The database itself is unreachable - Postgres refused the TCP connection, or an established
    connection died mid-request. The 2026-08-29 outage: asyncpg's error translation has no OSError
    entry, so a refused connection escapes as a RAW `builtins.ConnectionRefusedError` (not an
    OperationalError), matched no handler, and surfaced as an anonymous 500 with no X-Treg-Call-Id,
    no audit row and a leaked idempotency claim. Same shape as `_pool_saturated`: `treg_saturated`
    stays the key a retrying client branches on; `reason` distinguishes DB-down from pool queueing."""
    if isinstance(exc, DBAPIError) and not (
        getattr(exc, "connection_invalidated", False)
        or isinstance(getattr(exc, "orig", None), OSError)
    ):
        # A statement-level failure (deadlock, lock/statement timeout - `orig` is a Postgres error,
        # not an OSError, and the connection is still good) is a bug to surface, not the database
        # being down. Re-raise so it keeps today's 500 via ServerErrorMiddleware.
        raise exc
    logging.getLogger("treg").warning(
        "database unavailable - answered a typed 503: path=%s reason=db_unavailable exc=%r",
        request.url.path, exc)
    return await _saturated_503(
        request, {"detail": "treg's database is unavailable - retry in a moment",
                  "reason": "db_unavailable"})

async def _mark_treg_own_errors(request: Request, exc: StarletteHTTPException):
    """Tag treg's OWN refusals on `/call/` with `X-Treg-Error`, then answer exactly as before.

    A caller cannot otherwise tell a treg 404 ("no tool registered for that host") from the vendor's
    own 404 — both are a status code and some JSON. The local proxy needs that distinction to explain
    a failure without ever rewriting a real vendor response, and an agent reading a raw 403 needs to
    know whether to fix its request or ask an admin. The header is only ever ADDED; the status and the
    body are untouched, and a client that ignores it sees exactly what it saw before."""
    resp = await http_exception_handler(request, exc)
    if request.url.path.startswith("/call/"):
        resp.headers["X-Treg-Error"] = "1"
        # Refusals that raised before the handler's own audit ran (bad token, unknown tool, ACL,
        # deny rule, daily cap) would otherwise leave NO row, and no id to report — the funnel's
        # early friction was invisible until this. Here because this is the ONE place every refusal
        # passes through; the handler has a dozen raise points and stamping at each would be a dozen
        # chances to miss one.
        await _stamp_call_exit(request, resp, exc.status_code)
    return resp


# Preserve the frozen composition snapshot until the Stage 4 close-out refreshes module paths.
_pool_saturated.__module__ = "treg.api"
_db_unavailable.__module__ = "treg.api"
_mark_treg_own_errors.__module__ = "treg.api"
