"""The HTTP API (docs/API.md, docs/api/openapi.yaml) as a Starlette app."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from functools import partial

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from . import __version__, monitor, schema, views
from .deliveries import Deliveries
from .events import Broadcaster
from .errors import ApiError
from .store import CLOSED, OPEN, Client, Store
from .timeutil import parse_ts

log = logging.getLogger("waypoint.api")

MAX_BODY = 64 * 1024
READERS = ("agent", "human", "coordinator", "reader")


def error_response(err: ApiError) -> JSONResponse:
    return JSONResponse(err.body(), status_code=err.status)


async def _body(request: Request) -> dict:
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
        raise ApiError("invalid", "body: too large")
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_BODY:
            raise ApiError("invalid", "body: too large")
    try:
        body = json.loads(data or b"null")
    except (ValueError, UnicodeDecodeError):
        raise ApiError("invalid", "body: not JSON") from None
    if not isinstance(body, dict):
        raise ApiError("invalid", "body: must be a JSON object")
    return body


def _query(request: Request, allowed: set[str]) -> dict[str, str]:
    q = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed:
            raise ApiError("invalid", f"{key}: unknown query parameter", field=key)
        if key in q:
            raise ApiError("invalid", f"{key}: given twice", field=key)
        q[key] = value
    return q


def _bool(q: dict, key: str) -> bool | None:
    if key not in q:
        return None
    if q[key] in ("true", "1"):
        return True
    if q[key] in ("false", "0"):
        return False
    raise ApiError("invalid", f"{key}: true or false", field=key)


def _int(q: dict, key: str, default: int, lo: int, hi: int) -> int:
    if key not in q:
        return default
    v = q[key]
    if not v.isdigit() or not lo <= int(v) <= hi:
        raise ApiError("invalid", f"{key}: a number from {lo} to {hi}", field=key)
    return int(v)


def _ts(q: dict, key: str):
    if key not in q:
        return None
    try:
        return parse_ts(q[key])
    except ValueError:
        raise ApiError("invalid", f"{key}: a timestamp with a zone", field=key) from None


def _need(caller: Client, roles) -> None:
    if caller.role not in roles:
        raise ApiError("forbidden", "this client may not do this")


def create_app(store: Store, scheduler=None) -> "ErrorGuard":
    """The app; with a scheduler, its loop runs for the app's lifetime."""
    deliveries = Deliveries.of(store)
    broadcaster = Broadcaster.of(store)
    if scheduler is not None and deliveries.check_silent not in scheduler.pass_hooks:
        scheduler.pass_hooks.append(deliveries.check_silent)
    def caller_of(request: Request) -> Client:
        return request.state.caller

    async def call(fn, *args, **kw):
        return await run_in_threadpool(partial(fn, *args, **kw))

    # -------------------------------------------------------------- handlers
    async def health(request: Request):
        return JSONResponse({"status": "ok", "version": __version__})

    async def list_items(request: Request):
        _need(caller_of(request), READERS)
        q = _query(request, {"owner", "track", "state", "waiting_on", "waits_on_human", "late", "at_risk",
                             "changed_since", "limit"})
        states = q.get("state", "next,running,waiting")
        if states == "all":
            state_list = None
        else:
            state_list = tuple(s for s in states.split(",") if s)
            if not state_list or any(s not in OPEN + CLOSED for s in state_list):
                raise ApiError("invalid", "state: comma-separated states or all", field="state")
        for key in ("owner", "waiting_on"):
            if key in q:
                _check_scalar("ClientName", q[key], key)
        if "track" in q:
            _check_scalar("TrackId", q["track"], "track")
        since = _ts(q, "changed_since")
        items, truncated = await call(
            store.list_items, owner=q.get("owner"), track=q.get("track"), states=state_list,
            waiting_on=q.get("waiting_on"), waits_on_human=_bool(q, "waits_on_human"),
            late=_bool(q, "late"), at_risk=_bool(q, "at_risk"),
            changed_since=since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None,
            limit=_int(q, "limit", 500, 1, 2000))
        return JSONResponse({"items": items, "truncated": truncated})

    async def create_item(request: Request):
        caller = caller_of(request)
        _need(caller, ("agent", "human", "coordinator"))
        _query(request, set())
        body = await _body(request)
        item = await call(store.create_item, caller, body)
        return JSONResponse(item, status_code=201, headers={"Location": f"/v1/items/{item['id']}"})

    async def get_item(request: Request):
        _need(caller_of(request), READERS)
        _query(request, set())
        return JSONResponse(await call(store.get_item, request.path_params["id"], history=True))

    async def patch_item(request: Request):
        caller = caller_of(request)
        _need(caller, ("agent", "human", "coordinator"))
        _query(request, set())
        body = await _body(request)
        return JSONResponse(await call(store.patch_item, caller, request.path_params["id"], body))

    async def ask(request: Request):
        caller = caller_of(request)
        _need(caller, ("agent", "human", "coordinator"))
        _query(request, set())
        body = await _body(request)
        asked, item, created = await call(store.ask, caller, request.path_params["id"], body)
        return JSONResponse({"asked": asked, "item": item}, status_code=201 if created else 200)

    async def list_tracks(request: Request):
        _need(caller_of(request), READERS)
        q = _query(request, {"archived"})
        return JSONResponse({"tracks": await call(store.tracks, archived=bool(_bool(q, "archived")))})

    async def put_track(request: Request):
        caller = caller_of(request)
        _need(caller, ("agent", "human", "coordinator"))
        _query(request, set())
        body = await _body(request)
        return JSONResponse(await call(store.put_track, caller, request.path_params["id"], body))

    async def view_timeline(request: Request):
        _need(caller_of(request), READERS)
        q = _query(request, {"from", "to", "track"})
        tracks = None
        if "track" in q:
            tracks = [t for t in q["track"].split(",") if t]
            for t in tracks:
                _check_scalar("TrackId", t, "track")
        frm, to = _ts(q, "from"), _ts(q, "to")
        if frm and to and to < frm:
            raise ApiError("invalid", "to: before from", field="to")
        return JSONResponse(await call(views.timeline, store, frm, to, tracks))

    async def view_for(request: Request):
        _need(caller_of(request), READERS)
        _query(request, set())
        human = request.path_params["human"]
        cl = await call(store.client, human)
        if cl is None or cl.role != "human":
            raise ApiError("not_found", "no such human")
        return JSONResponse(await call(views.for_human, store, human))

    async def get_deliveries(request: Request):
        caller = caller_of(request)
        _need(caller, ("deliverer",))
        q = _query(request, {"wait", "limit"})
        wait = _int(q, "wait", 60, 0, 60)
        limit = _int(q, "limit", 50, 1, 100)
        got = await deliveries.long_poll(caller, wait, limit, call)
        return JSONResponse({"deliveries": got, "lease_seconds": store.settings.lease_seconds})

    async def ack_delivery(request: Request):
        caller = caller_of(request)
        _need(caller, ("deliverer",))
        _query(request, set())
        body = await _body(request)
        return JSONResponse(await call(deliveries.ack, caller, request.path_params["id"], body))

    async def list_clients(request: Request):
        _query(request, set())
        return JSONResponse({"clients": await call(store.list_clients, caller_of(request))})

    async def register_client(request: Request):
        _query(request, set())
        body = await _body(request)
        return JSONResponse(await call(store.register, caller_of(request), body), status_code=201)

    async def change_client(request: Request):
        _query(request, set())
        body = await _body(request)
        return JSONResponse(await call(store.change_client, caller_of(request), request.path_params["name"], body))

    async def rotate_client(request: Request):
        _query(request, set())
        return JSONResponse(await call(store.rotate, caller_of(request), request.path_params["name"]))

    async def get_status(request: Request):
        _need(caller_of(request), READERS)
        _query(request, set())
        return JSONResponse(await call(monitor.status, store))

    async def get_metrics(request: Request):
        _need(caller_of(request), READERS)
        _query(request, set())
        return PlainTextResponse(await call(monitor.metrics, store), media_type="text/plain; version=0.0.4")

    async def events(request: Request):
        _need(caller_of(request), READERS)
        _query(request, set())
        last = request.headers.get("last-event-id")
        last_id = last[:64] if last else None
        return StreamingResponse(broadcaster.stream(last_id, request.is_disconnected), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    routes = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/items", list_items, methods=["GET"]),
        Route("/v1/items", create_item, methods=["POST"]),
        Route("/v1/items/{id}", get_item, methods=["GET"]),
        Route("/v1/items/{id}", patch_item, methods=["PATCH"]),
        Route("/v1/items/{id}/ask", ask, methods=["POST"]),
        Route("/v1/tracks", list_tracks, methods=["GET"]),
        Route("/v1/tracks/{id}", put_track, methods=["PUT"]),
        Route("/v1/views/timeline", view_timeline, methods=["GET"]),
        Route("/v1/views/for/{human}", view_for, methods=["GET"]),
        Route("/v1/deliveries", get_deliveries, methods=["GET"]),
        Route("/v1/deliveries/{id}/ack", ack_delivery, methods=["POST"]),
        Route("/v1/events", events, methods=["GET"]),
        Route("/v1/status", get_status, methods=["GET"]),
        Route("/metrics", get_metrics, methods=["GET"]),
        Route("/v1/clients", list_clients, methods=["GET"]),
        Route("/v1/clients", register_client, methods=["POST"]),
        Route("/v1/clients/{name}", change_client, methods=["PATCH"]),
        Route("/v1/clients/{name}/rotate", rotate_client, methods=["POST"]),
    ]
    async def on_api_error(request: Request, err: ApiError):
        return error_response(err)

    async def on_http_error(request: Request, exc):
        if getattr(exc, "status_code", 500) in (404, 405):
            return error_response(ApiError("not_found", "no such endpoint"))
        return JSONResponse({"error": {"code": "invalid", "message": "bad request"}}, status_code=400)

    async def on_db_error(request: Request, exc):
        log.warning("database busy or unavailable")
        return error_response(ApiError("unavailable", "try again shortly"))

    async def on_error(request: Request, exc):
        return JSONResponse({"error": {"code": "unavailable", "message": "internal error"}}, status_code=500)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = None
        if scheduler is not None:
            # one pass before serving, so the first writes after an outage see its context
            try:
                await run_in_threadpool(scheduler.run_pass)
            except Exception as exc:
                log.error("first scheduler pass failed: %s", type(exc).__name__)
            task = asyncio.create_task(scheduler.run_forever(first_delay=store.settings.pass_seconds))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(TokenGuard, store=store)],
        exception_handlers={
            ApiError: on_api_error,
            HTTPException: on_http_error,
            sqlite3.OperationalError: on_db_error,
            Exception: on_error,
        },
    )
    app.state.store = store
    app.state.deliveries = deliveries
    app.state.broadcaster = broadcaster
    return ErrorGuard(app)


class ErrorGuard:
    """Outermost wrapper. Starlette answers an unexpected error with the 500 handler and then
    re-raises it, so the server would log the full traceback, including the exception's message
    (which may quote a value from a request). This logs only the error's type and swallows it.
    Everything else (attributes such as ``state`` or ``router``) is the app's."""

    def __init__(self, app: Starlette):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except Exception as exc:  # the 500 has been sent by then
            if scope["type"] != "http":
                raise
            log.error("internal error on %s %s: %s", scope.get("method"), scope.get("path"), type(exc).__name__)


def _check_scalar(name: str, value: str, field: str) -> None:
    try:
        schema.check(name, value)
    except ApiError:
        raise ApiError("invalid", f"{field}: not valid", field=field) from None


class TokenGuard:
    """Pure ASGI middleware: a valid token on every request but the health check.

    The caller is put in the scope's state; a request without a valid token gets 401 and nothing
    else, whatever the path (so it can't learn which endpoints or items exist).
    """

    def __init__(self, app, store: Store):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/v1/health":
            return await self.app(scope, receive, send)
        auth = ""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                auth = v.decode("latin-1")
                break
        scheme, _, token = auth.partition(" ")
        caller = None
        if scheme.lower() == "bearer" and token.strip():
            caller = await run_in_threadpool(self.store.authenticate, token.strip())
        if caller is None:
            response = error_response(ApiError("unauthorized", "a valid token is needed"))
            return await response(scope, receive, send)
        scope.setdefault("state", {})["caller"] = caller
        return await self.app(scope, receive, send)
