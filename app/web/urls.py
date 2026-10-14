"""The panel's URL prefix (``WEB_PREFIX``) for routes, middlewares and URLs returned by the API.

The whole panel (index, static files, API) lives under ``app[PREFIX_KEY]`` — ``''`` (site root,
the default) or e.g. ``'/posts'`` behind a reverse proxy that serves several panels.

* Routes are registered through `prefixed_router(app)`, which adds the prefix to every path.
* Middlewares look at `local_path(request)` — the path relative to the prefix (``'/api/posts'``),
  or None for a path outside of it.
* URLs handed out by the API are built with `api_url('/api/...')`, which adds the prefix of the
  app handling the current request (set by `prefix_middleware` in a context variable, so the
  serializers need no request argument).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from aiohttp import web

from app.web.keys import PREFIX_KEY

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

_URL_PREFIX: ContextVar[str] = ContextVar("pcb_url_prefix", default="")


def local_path(request: web.Request) -> str | None:
    """Request path relative to the panel prefix: ``/posts/api/x`` → ``/api/x``, ``/posts`` → ``''``;
    None when the path is outside the prefix. Without a prefix: the path itself."""
    prefix = request.app.get(PREFIX_KEY, "")
    path = request.path
    if not prefix:
        return path
    if path == prefix:
        return ""
    if path.startswith(prefix + "/"):
        return path[len(prefix):]
    return None


def api_url(path: str) -> str:
    """``'/api/uploads/x.png'`` → ``'<prefix>/api/uploads/x.png'`` for the request being handled."""
    return _URL_PREFIX.get() + path


def cookie_path(request: web.Request) -> str:
    """Path of the session cookie: ``<prefix>/`` (``/`` without a prefix)."""
    return request.app.get(PREFIX_KEY, "") + "/"


@web.middleware
async def prefix_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    token = _URL_PREFIX.set(request.app.get(PREFIX_KEY, ""))
    try:
        return await handler(request)
    finally:
        _URL_PREFIX.reset(token)


class PrefixedRouter:
    """The subset of ``app.router`` used by the ``setup_routes`` functions, with the panel prefix
    added to every path."""

    def __init__(self, router: web.UrlDispatcher, prefix: str) -> None:
        self._router = router
        self.prefix = prefix

    def add_get(self, path: str, handler: Any, **kwargs: Any) -> web.AbstractRoute:
        return self._router.add_get(self.prefix + path, handler, **kwargs)

    def add_post(self, path: str, handler: Any, **kwargs: Any) -> web.AbstractRoute:
        return self._router.add_post(self.prefix + path, handler, **kwargs)

    def add_put(self, path: str, handler: Any, **kwargs: Any) -> web.AbstractRoute:
        return self._router.add_put(self.prefix + path, handler, **kwargs)

    def add_patch(self, path: str, handler: Any, **kwargs: Any) -> web.AbstractRoute:
        return self._router.add_patch(self.prefix + path, handler, **kwargs)

    def add_delete(self, path: str, handler: Any, **kwargs: Any) -> web.AbstractRoute:
        return self._router.add_delete(self.prefix + path, handler, **kwargs)

    def add_static(self, path: str, directory: Path, **kwargs: Any) -> web.AbstractResource:
        return self._router.add_static(self.prefix + path, directory, **kwargs)


def prefixed_router(app: web.Application) -> PrefixedRouter:
    return PrefixedRouter(app.router, app.get(PREFIX_KEY, ""))
