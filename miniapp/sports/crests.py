"""Club crests and competition emblems, served from our own origin (M3.3).

The browser never contacts the image host: the backend fetches the picture once,
keeps it in a bounded memory cache and serves it under ``/api/crests/...`` (so the
CSP stays ``img-src 'self'`` and the user's IP is not sent to a third party).

Only URLs the provider itself put in a payload are fetched, and only from one
host, over HTTPS, as PNG (verified by its magic bytes). SVG is refused on purpose:
served from our origin it could carry script. Nothing is invented: without a
learned URL there is no crest, and the app shows the initial badge.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

from .client import _OPENER, _read_with_deadline

logger = logging.getLogger("miniapp.sports")

ALLOWED_HOST = "crests.football-data.org"
_PATH = re.compile(r"^/[A-Za-z0-9_-]{1,40}\.(png|svg)$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 300 * 1024
FETCH_TIMEOUT_S = 5.0
QUEUE_WAIT_S = 3.0
KINDS = ("team", "competition")

# (url, timeout_s) -> body bytes or None (not an image we serve). Injected in tests.
CrestFetch = Callable[[str, float], Awaitable[bytes | None]]

# Picture downloads get their own threads: a slow image host never delays API calls.
CREST_CONCURRENCY = 6
_CREST_THREADS = ThreadPoolExecutor(max_workers=CREST_CONCURRENCY, thread_name_prefix="crest-fetch")


def crest_source(value: object) -> str | None:
    """The provider's crest URL if it is one we may fetch, else None."""
    if not isinstance(value, str) or len(value) > 200:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return None
    if port is not None:
        return None
    if (parts.scheme != "https" or parts.hostname != ALLOWED_HOST
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.netloc != ALLOWED_HOST or not _PATH.match(parts.path)):
        return None
    return f"https://{ALLOWED_HOST}{parts.path}"


def is_png(body: bytes) -> bool:
    return body.startswith(PNG_MAGIC) and len(body) <= MAX_IMAGE_BYTES


# SVG crests (national teams on football-data.org are SVG only). Shown only through <img> (no script runs
# there) and served with a no-script CSP; still, only plain vector graphics pass: no DOCTYPE/entities, no
# script, no event handlers, no foreignObject/image, no external references.
SVG_NS = "http://www.w3.org/2000/svg"
_SVG_ELEMENTS = frozenset({
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "defs", "linearGradient",
    "radialGradient", "stop", "clipPath", "mask", "use", "title", "desc", "symbol", "pattern", "style", "text",
    "tspan", "metadata",
    # Cheap per-pixel filter primitives only: no feImage (loads a picture), no blur/morphology (their cost
    # grows with a radius the file chooses); their number is capped (MAX_SVG_FILTER_STEPS).
    "filter", "feColorMatrix", "feOffset", "feFlood", "feBlend", "feComposite", "feMerge", "feMergeNode",
    "feComponentTransfer", "feFuncR", "feFuncG", "feFuncB", "feFuncA",
    "image",  # only with an embedded PNG/JPEG (data: URI), see _EMBEDDED_RASTER
})
# Editor data (Inkscape/Sodipodi) may also sit outside <metadata> (e.g. sodipodi:namedview): a browser
# ignores these elements; inside them only more editor data may follow.
_EDITOR_NS = frozenset({"http://www.inkscape.org/namespaces/inkscape", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"})
_EMBEDDED_RASTER = re.compile(r"^data:image/(png|jpeg);base64,([A-Za-z0-9+/=\s]+)$")
XLINK_NS = "http://www.w3.org/1999/xlink"
MAX_SVG_FILTER_STEPS = 32


def _embedded_raster(value: str) -> bool:
    """A data: URI holding a real PNG/JPEG (checked after decoding), never another document."""
    found = _EMBEDDED_RASTER.match(value.strip())
    if found is None:
        return False
    try:
        data = base64.b64decode("".join(found.group(2).split()), validate=True)
    except (ValueError, binascii.Error):
        return False
    magic = PNG_MAGIC if found.group(1) == "png" else b"\xff\xd8\xff"
    return data.startswith(magic) and len(data) > len(magic)
_METADATA_NS = frozenset({
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "http://purl.org/dc/elements/1.1/", "http://purl.org/dc/terms/",
    "http://creativecommons.org/ns#", "http://web.resource.org/cc/", "http://www.inkscape.org/namespaces/inkscape",
    "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
})
MAX_SVG_DEPTH = 40
_ENCODING = re.compile(rb"""encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")
_URL_REF = re.compile(r"url\(\s*['\"]?([^)'\"]*)", re.IGNORECASE)


def _safe_value(value: str) -> bool:
    if "\\" in value:
        return False  # CSS escapes could spell url( / @import past the scan: crests never need them
    low = value.lower()
    if "javascript:" in low or "@import" in low or "expression(" in low:
        return False
    return all(ref.strip().startswith("#") for ref in _URL_REF.findall(value))


def is_safe_svg(body: bytes) -> bool:
    if len(body) > MAX_IMAGE_BYTES:
        return False
    # UTF-8 only: another encoding (UTF-16 has NUL bytes or a BOM) would hide a DOCTYPE from the scan below.
    if b"\x00" in body or body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    declared = _ENCODING.search(body[:200])
    if declared and declared.group(1).lower().replace(b"_", b"-") not in (b"utf-8", b"utf8", b"us-ascii", b"ascii"):
        return False
    low = body.lower()
    if b"<!doctype" in low or b"<!entity" in low or b"<?xml-stylesheet" in low:
        return False
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError):
        return False
    if root.tag != f"{{{SVG_NS}}}svg":
        return False

    filter_steps = 0

    def walk(node: ElementTree.Element, in_metadata: bool, depth: int = 0) -> bool:
        nonlocal filter_steps
        if depth > MAX_SVG_DEPTH:
            return False  # a crest is never this deep; refuse instead of exhausting the recursion
        ns, _, local = node.tag[1:].partition("}") if node.tag.startswith("{") else ("", "", node.tag)
        editor = not in_metadata and ns in _EDITOR_NS
        if in_metadata or editor:
            if ns not in _METADATA_NS:
                return False  # inside <metadata> only editor data (RDF, Dublin Core, Inkscape) — never SVG/HTML
        elif ns != SVG_NS or local not in _SVG_ELEMENTS:
            return False
        image = local == "image" and ns == SVG_NS
        if ns == SVG_NS and local.startswith("fe"):
            filter_steps += 1
            if filter_steps > MAX_SVG_FILTER_STEPS:
                return False
        hrefs = 0
        for name, value in node.attrib.items():
            attr = name.rpartition("}")[2].lower()
            if attr.startswith("on") or attr == "base" or not _safe_value(value):
                return False  # xml:base would resolve even "#x" references against an external document
            if attr == "href":
                if image:
                    # Exactly the picture a browser reads: href / xlink:href holding a real PNG or JPEG.
                    if name not in ("href", f"{{{XLINK_NS}}}href") or not _embedded_raster(value):
                        return False
                    hrefs += 1
                elif not value.strip().startswith("#"):
                    return False
        if image and hrefs == 0:
            return False  # an <image> always names its embedded picture
        if local == "style" and not _safe_value(node.text or ""):
            return False
        return all(walk(child, in_metadata or editor or local == "metadata", depth + 1) for child in node)

    return walk(root, False)


def is_crest(body: bytes) -> bool:
    return is_png(body) or (not body.startswith(PNG_MAGIC) and is_safe_svg(body))


def media_type(body: bytes) -> str:
    return "image/png" if body.startswith(PNG_MAGIC) else "image/svg+xml"


def _blocking_fetch(url: str, timeout_s: float) -> bytes | None:
    deadline = time.monotonic() + timeout_s
    # No credentials of any kind; redirects are refused by the shared opener.
    svg = url.endswith(".svg")
    expected = "image/svg+xml" if svg else "image/png"
    request = urllib.request.Request(url, headers={"Accept": expected, "User-Agent": "ProqnozAI-MiniApp"})
    try:
        with _OPENER.open(request, timeout=timeout_s) as response:
            if response.status != 200:
                return None
            if (response.headers.get("Content-Type") or "").split(";")[0].strip().lower() != expected:
                return None
            body = _read_with_deadline(response, deadline, MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None
    ok = (not body.startswith(PNG_MAGIC) and is_safe_svg(body)) if svg else is_png(body)
    return body if ok else None


async def urllib_crest_fetch(url: str, timeout_s: float) -> bytes | None:
    loop = asyncio.get_running_loop()
    try:
        # Wall-clock bound on the async side too: the caller is released even if the thread is stuck.
        return await asyncio.wait_for(loop.run_in_executor(_CREST_THREADS, _blocking_fetch, url, timeout_s),
                                      timeout_s + 1.0)
    except TimeoutError:
        return None


class CrestStore:
    """Learned crest URLs plus a bounded cache of the images themselves."""

    def __init__(self, fetch: CrestFetch = urllib_crest_fetch, *, clock: Callable[[], float] = time.time,
                 max_urls: int = 5000, max_images: int = 800, max_bytes: int = 32 * 1024 * 1024,
                 per_minute: int = 240, per_second: int = 200, rejected_per_second: int = 200,
                 failure_ttl_s: float = 3600.0) -> None:
        self._fetch = fetch
        self._clock = clock
        self._urls: OrderedDict[str, tuple[str, ...]] = OrderedDict()  # key -> sources, tried in order
        self._images: OrderedDict[str, bytes] = OrderedDict()
        self._image_bytes = 0
        self._failed: dict[str, float] = {}
        # Pictures served from a fallback source, and when: the first source is tried again after the
        # failure TTL, so a passing failure of the emblem never leaves the flag in its place for good.
        self._fell_back: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Future[bytes | None]] = {}
        self._recent: deque[float] = deque()
        self._served: deque[float] = deque()
        self._per_second = per_second
        self._rejected: deque[float] = deque()
        self._rejected_per_second = rejected_per_second
        self._max_urls, self._max_images, self._max_bytes = max_urls, max_images, max_bytes
        self._per_minute = per_minute
        self._failure_ttl = failure_ttl_s
        self._gate = asyncio.Semaphore(CREST_CONCURRENCY)

    @staticmethod
    def key(kind: str, ident: str) -> str:
        return f"{kind}:{ident}"

    def learn(self, kind: str, ident: str, url: object, *, fallback: object = None) -> bool:
        """`fallback` (e.g. the country's flag) is fetched only when the first picture is unusable."""
        source = crest_source(url)
        if source is None:
            return False
        extra = crest_source(fallback)
        sources = (source,) if extra is None or extra == source else (source, extra)
        key = self.key(kind, ident)
        if self._urls.get(key) != sources:
            self._urls[key] = sources
            self._drop_image(key)
            self._failed.pop(key, None)
        self._urls.move_to_end(key)
        while len(self._urls) > self._max_urls:
            old, _ = self._urls.popitem(last=False)
            self._drop_image(old)
            self._failed.pop(old, None)
        return True

    def forget(self, kind: str, ident: str) -> None:
        key = self.key(kind, ident)
        self._urls.pop(key, None)
        self._drop_image(key)
        self._failed.pop(key, None)

    def is_fallback(self, kind: str, ident: str) -> bool:
        """The picture in memory is a stand-in (the country's flag), to be checked again soon."""
        return self.key(kind, ident) in self._fell_back

    def admit_rejected(self) -> bool:
        """Cap on cheap 404s (unknown ids, cache-busting queries) so junk cannot spin the origin."""
        return self._window(self._rejected, self._rejected_per_second)

    def path(self, kind: str, ident: str) -> str | None:
        """Our URL for the picture, only when the provider gave one."""
        return f"/api/crests/{kind}/{ident}" if self.key(kind, ident) in self._urls else None

    def _drop_image(self, key: str) -> None:
        self._fell_back.pop(key, None)
        body = self._images.pop(key, None)
        if body is not None:
            self._image_bytes -= len(body)

    def _store(self, key: str, body: bytes) -> None:
        self._drop_image(key)
        self._images[key] = body
        self._image_bytes += len(body)
        while self._images and (len(self._images) > self._max_images or self._image_bytes > self._max_bytes):
            old, dropped = self._images.popitem(last=False)
            self._image_bytes -= len(dropped)
            self._fell_back.pop(old, None)

    def admit(self) -> bool:
        """Global cap on crest responses per second (the route is public and outside the
        edge's per-IP API limit; the edge caches crests, so the origin sees few of them)."""
        return self._window(self._served, self._per_second)

    def _window(self, stamps: deque[float], limit: int) -> bool:
        now = self._clock()
        while stamps and now - stamps[0] >= 1.0:
            stamps.popleft()
        if len(stamps) >= limit:
            return False
        stamps.append(now)
        return True

    def _spend(self) -> bool:
        now = self._clock()
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()
        if len(self._recent) >= self._per_minute:
            return False
        self._recent.append(now)
        return True

    async def image(self, kind: str, ident: str) -> bytes | None:
        key = self.key(kind, ident)
        url = self._urls.get(key)
        if url is None:
            return None
        cached = stale = self._images.get(key)
        fell_back = self._fell_back.get(key)
        if cached is not None and fell_back is not None and self._clock() - fell_back >= self._failure_ttl:
            cached = None  # time to ask the emblem itself again; the flag is kept until a new answer
        if cached is not None:
            self._images.move_to_end(key)
            return cached
        failed_at = self._failed.get(key)
        if failed_at is not None and self._clock() - failed_at < self._failure_ttl:
            return None
        running = self._inflight.get(key)
        if running is not None:
            return await asyncio.shield(running)
        if not self._spend():
            return stale  # over the per-minute cap: try again later (the flag shown so far meanwhile)
        future: asyncio.Future[bytes | None] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            try:
                # A bounded wait for a download slot: a flood of cold requests cannot park
                # requests indefinitely behind the slots.
                await asyncio.wait_for(self._gate.acquire(), QUEUE_WAIT_S)
            except TimeoutError:
                future.set_result(stale)
                return stale  # busy, not remembered as a failure
            try:
                body, used = None, 0
                for used, source in enumerate(url):
                    body = await self._fetch(source, FETCH_TIMEOUT_S)
                    body = body if body is not None and is_crest(body) else None
                    if body is not None:
                        break
            finally:
                self._gate.release()
            if self._urls.get(key) != url:
                body = None  # the provider moved this crest meanwhile: neither its picture nor its failure counts
            elif body is None:
                kept = self._images.get(key)
                if kept is not None:  # a retry of the emblem found nothing at all: the earlier flag stays
                    self._fell_back[key] = self._clock()
                    body = kept
                else:
                    self._failed[key] = self._clock()
                    logger.info("crest.unavailable kind=%s", kind)
            else:
                self._store(key, body)
                if used:
                    self._fell_back[key] = self._clock()
                else:
                    self._fell_back.pop(key, None)
            future.set_result(body)
            return body
        except BaseException as exc:
            kept = self._images.get(key) if self._urls.get(key) == url else None
            future.set_result(kept)
            if isinstance(exc, Exception):
                if kept is not None:
                    self._fell_back[key] = self._clock()  # a failed refresh keeps the flag already shown
                    return kept
                if self._urls.get(key) == url:
                    self._failed[key] = self._clock()
                return None
            raise
        finally:
            self._inflight.pop(key, None)
