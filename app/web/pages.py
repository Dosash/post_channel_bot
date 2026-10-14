"""The SPA page: ``index.html`` with the deployment values injected, and the prefix redirects.

``static/index.html`` is a template with three placeholder tags that the server fills:

* ``<base href="/">``                  → ``<base href="<prefix>/">`` (every URL in the SPA is relative)
* ``<meta name="pcb-hub" content="">``  → ``HUB_URL`` (empty = no «← Все панели» link)
* ``<meta name="pcb-version" content="">`` → the app version

and every relative ``static/…`` link (``href`` / ``src``) gets ``?v=<short hash of the file's mtime
and size>``, so browsers pick up a new ``app.js`` / ``style.css`` right after an update. The tags
are found by element and ``name`` (attribute order, quotes and spacing do not matter).
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import re
from pathlib import Path

from aiohttp import web

from app.web.keys import CONFIG_KEY, PREFIX_KEY, STATIC_DIR_KEY

INDEX_FILE = "index.html"

_BASE_TAG = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_HEAD_TAG = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_STATIC_LINK = re.compile(
    r"""(?P<attr>\b(?:href|src)\s*=\s*)(?P<q>["'])(?P<path>(?:\./)?static/[^"'?#\s]+)(?:\?[^"'#]*)?(?P<frag>#[^"']*)?(?P=q)""",
    re.IGNORECASE,
)


def _meta_tag(name: str) -> re.Pattern[str]:
    return re.compile(rf"""<meta\b(?=[^>]*\bname\s*=\s*["']?{re.escape(name)}["'\s/>])[^>]*>""", re.IGNORECASE)


_HUB_META = _meta_tag("pcb-hub")
_VERSION_META = _meta_tag("pcb-version")


def asset_version(path: Path) -> str | None:
    """Short hash of a static file's mtime and size (None when the file is missing)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return hashlib.sha1(f"{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()[:10]


def _static_file(static_dir: Path, link: str) -> Path | None:
    """The file a relative ``static/…`` link points to, or None when it leaves the directory."""
    relative = link[2:] if link.startswith("./") else link
    relative = relative[len("static/"):]
    try:
        root = static_dir.resolve()
        target = (root / relative).resolve()
    except (OSError, RuntimeError):
        return None
    return target if target.is_relative_to(root) and target.is_file() else None


def render_index(template: str, *, prefix: str, hub_url: str | None, version: str, static_dir: Path) -> str:
    """Fill the placeholders of index.html (see the module docstring)."""
    base = f'<base href="{html.escape(prefix + "/", quote=True)}">'
    page, found = _BASE_TAG.subn(lambda _: base, template, count=1)
    if not found:  # no placeholder: insert it, every relative URL depends on it
        page, found = _HEAD_TAG.subn(lambda m: m.group(0) + base, page, count=1)
        if not found:
            page = base + page
    hub = f'<meta name="pcb-hub" content="{html.escape(hub_url or "", quote=True)}">'
    page = _HUB_META.sub(lambda _: hub, page, count=1)
    meta_version = f'<meta name="pcb-version" content="{html.escape(version, quote=True)}">'
    page = _VERSION_META.sub(lambda _: meta_version, page, count=1)

    def bust(match: re.Match[str]) -> str:
        file = _static_file(static_dir, match.group("path"))
        stamp = asset_version(file) if file is not None else None
        query = f"?v={stamp}" if stamp else ""
        quote = match.group("q")
        return f"{match.group('attr')}{quote}{match.group('path')}{query}{match.group('frag') or ''}{quote}"

    return _STATIC_LINK.sub(bust, page)


def _load_index(static_dir: Path, prefix: str, hub_url: str | None, version: str) -> str | None:
    try:
        template = (static_dir / INDEX_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return render_index(template, prefix=prefix, hub_url=hub_url, version=version, static_dir=static_dir)


async def index(request: web.Request) -> web.Response:
    config = request.app[CONFIG_KEY]
    page = await asyncio.to_thread(
        _load_index, request.app[STATIC_DIR_KEY], request.app[PREFIX_KEY], config.hub_url, config.version
    )
    if page is None:
        raise web.HTTPNotFound(text="index.html не найден")
    return web.Response(text=page, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-cache"})


def _location(request: web.Request) -> str:
    target = request.app[PREFIX_KEY] + "/"
    return f"{target}?{request.query_string}" if request.query_string else target


async def redirect_permanent(request: web.Request) -> web.StreamResponse:
    """``GET <prefix>`` → ``<prefix>/`` (relative URLs need the trailing slash)."""
    raise web.HTTPMovedPermanently(_location(request))


async def redirect_root(request: web.Request) -> web.StreamResponse:
    """``GET /`` → ``<prefix>/`` when the panel lives under a prefix."""
    raise web.HTTPFound(_location(request))
