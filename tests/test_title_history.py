import asyncio
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from podline.title_history import (
    MAX_FEED_BYTES,
    MAX_TITLE_CHARACTERS,
    load_published_titles,
)


FEED_URL = "https://feed.podbean.com/sexdrunks/feed.xml"


class FakeFeedResponse:
    def __init__(
        self, text: str = "", *, status_code: int = 200, chunks=None
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", FEED_URL)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "feed request failed", request=request, response=response
            )

    async def aiter_bytes(self, chunk_size=None):
        for chunk in self.chunks or [self.text.encode("utf-8")]:
            yield chunk


class FakeFeedClient:
    def __init__(self, *, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.calls = []

    def stream(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _rss(*titles: str) -> str:
    items = "".join(f"<item><title>{title}</title></item>" for title in titles)
    return f"<?xml version='1.0'?><rss><channel><title>Sex Drunks</title>{items}</channel></rss>"


def test_load_published_titles_normalizes_deduplicates_limits_and_caches(
    tmp_path: Path,
) -> None:
    client = FakeFeedClient(
        response=FakeFeedResponse(
            _rss(
                "32. Raise Your Hand—or Don’t",
                "31. Sober Dating, Future Tripping",
                "raise your hand—or don’t",
                "30. Welcome Is Not the Same as Safe",
            )
        )
    )

    titles = load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=4,
        limit=2,
        cache_dir=tmp_path,
        client=client,
    )

    assert titles == [
        "Raise Your Hand—or Don’t",
        "Sober Dating, Future Tripping",
    ]
    assert client.calls == [
        ("GET", FEED_URL, {"timeout": 4, "follow_redirects": True})
    ]
    cached = json.loads((tmp_path / "published-title-history.json").read_text())
    assert cached == {
        "feed_url": FEED_URL,
        "titles": [
            "Raise Your Hand—or Don’t",
            "Sober Dating, Future Tripping",
        ],
    }


def test_load_published_titles_uses_matching_cache_when_feed_is_unavailable(
    tmp_path: Path,
) -> None:
    successful_client = FakeFeedClient(
        response=FakeFeedResponse(_rss("28. I met God at the car dealership"))
    )
    load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=4,
        limit=20,
        cache_dir=tmp_path,
        client=successful_client,
    )
    failed_client = FakeFeedClient(
        error=httpx.ConnectError(
            "offline", request=httpx.Request("GET", FEED_URL)
        )
    )

    titles = load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=4,
        limit=20,
        cache_dir=tmp_path,
        client=failed_client,
    )

    assert titles == ["I met God at the car dealership"]


def test_load_published_titles_returns_empty_without_feed_or_matching_cache(
    tmp_path: Path,
) -> None:
    (tmp_path / "published-title-history.json").write_text(
        json.dumps({"feed_url": "https://other.example/feed.xml", "titles": ["Old"]}),
        encoding="utf-8",
    )
    failed_client = FakeFeedClient(
        error=httpx.ConnectError(
            "offline", request=httpx.Request("GET", FEED_URL)
        )
    )

    titles = load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=4,
        limit=20,
        cache_dir=tmp_path,
        client=failed_client,
    )

    assert titles == []


def test_load_published_titles_ignores_non_utf8_cache_when_feed_is_unavailable(
    tmp_path: Path,
) -> None:
    (tmp_path / "published-title-history.json").write_bytes(b"\xff\xfe")
    failed_client = FakeFeedClient(
        error=httpx.ConnectError(
            "offline", request=httpx.Request("GET", FEED_URL)
        )
    )

    titles = load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=4,
        limit=20,
        cache_dir=tmp_path,
        client=failed_client,
    )

    assert titles == []


def test_load_published_titles_ignores_oversized_cache_when_feed_is_unavailable(
    tmp_path: Path,
) -> None:
    (tmp_path / "published-title-history.json").write_bytes(
        b"x" * (MAX_FEED_BYTES + 1)
    )
    failed_client = FakeFeedClient(
        error=httpx.ConnectError(
            "offline", request=httpx.Request("GET", FEED_URL)
        )
    )

    titles = load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=4,
        limit=20,
        cache_dir=tmp_path,
        client=failed_client,
    )

    assert titles == []


def test_load_published_titles_rejects_oversized_feed(tmp_path: Path) -> None:
    client = FakeFeedClient(
        response=FakeFeedResponse(chunks=[b"x" * (MAX_FEED_BYTES + 1)])
    )

    titles = load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=4,
        limit=20,
        cache_dir=tmp_path,
        client=client,
    )

    assert titles == []


def test_load_published_titles_skips_oversized_titles(tmp_path: Path) -> None:
    client = FakeFeedClient(
        response=FakeFeedResponse(
            _rss("x" * (MAX_TITLE_CHARACTERS + 1), "28. A usable title")
        )
    )

    titles = load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=4,
        limit=20,
        cache_dir=tmp_path,
        client=client,
    )

    assert titles == ["A usable title"]


def test_load_published_titles_skips_disabled_or_zero_limit_feed(
    tmp_path: Path,
) -> None:
    client = FakeFeedClient(response=FakeFeedResponse(_rss("A title")))

    assert load_published_titles(
        feed_url="",
        timeout_seconds=4,
        limit=20,
        cache_dir=tmp_path,
        client=client,
    ) == []
    assert load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=4,
        limit=0,
        cache_dir=tmp_path,
        client=client,
    ) == []
    assert client.calls == []


def test_feed_deadline_interrupts_trickling_body_and_uses_cache(tmp_path: Path) -> None:
    disconnected = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            try:
                # Each write is faster than the read timeout; the full body is not.
                for _ in range(100):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    feed_url = f"http://127.0.0.1:{server.server_port}/feed"
    (tmp_path / "published-title-history.json").write_text(
        json.dumps({"feed_url": feed_url, "titles": ["Cached title"]})
    )
    try:
        started = time.monotonic()
        titles = load_published_titles(
            feed_url=feed_url, timeout_seconds=0.2, limit=20, cache_dir=tmp_path
        )
        elapsed = time.monotonic() - started
        assert titles == ["Cached title"]
        assert elapsed < 1.0, f"deadline failed to interrupt download: {elapsed:.3f}s"
        assert disconnected.wait(1), "timed-out response was not closed"
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_feed_deadline_interrupts_wait_for_headers_without_cache(tmp_path: Path) -> None:
    cancelled = []

    class WaitingForHeaders(FakeFeedResponse):
        async def __aenter__(self):
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return self

    started = time.monotonic()
    titles = load_published_titles(
        feed_url=FEED_URL,
        timeout_seconds=0.05,
        limit=20,
        cache_dir=tmp_path,
        client=FakeFeedClient(response=WaitingForHeaders(_rss("A title"))),
    )
    assert titles == []
    assert cancelled == [True]
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("has_cache", [True, False])
def test_feed_deadline_interrupts_slow_dns(monkeypatch, tmp_path: Path, has_cache) -> None:
    feed_url = "https://slow-dns.example.invalid/feed.xml"
    marker = tmp_path / "resolver-pid"
    # Apply the same resolver stall in this process and any Python download worker.
    customization = tmp_path / "sitecustomize.py"
    customization.write_text(
        "import os, socket, time\n"
        "from pathlib import Path\n"
        "def slow_resolution(*args, **kwargs):\n"
        f"    Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(2)\n"
        "    raise socket.gaierror('simulated stalled resolver')\n"
        "socket.getaddrinfo = slow_resolution\n"
    )

    def slow_resolution(*args, **kwargs):
        marker.write_text(str(os.getpid()))
        time.sleep(2)
        raise socket.gaierror("simulated stalled resolver")

    monkeypatch.setattr(socket, "getaddrinfo", slow_resolution)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    cache_path = tmp_path / "published-title-history.json"
    snapshot = json.dumps({"feed_url": feed_url, "titles": ["Cached title"]})
    if has_cache:
        cache_path.write_text(snapshot)

    started = time.monotonic()
    titles = load_published_titles(
        feed_url=feed_url, timeout_seconds=0.5, limit=20, cache_dir=tmp_path
    )
    elapsed = time.monotonic() - started
    assert marker.exists(), "test never reached DNS resolution"
    assert titles == (["Cached title"] if has_cache else [])
    assert elapsed < 1.5, f"deadline waited for DNS cleanup: {elapsed:.3f}s"
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    if has_cache:
        assert cache_path.read_text() == snapshot
    else:
        assert not cache_path.exists()


@pytest.mark.parametrize("has_cache", [True, False])
def test_unsupported_xml_encoding_falls_back(tmp_path: Path, has_cache) -> None:
    cache_path = tmp_path / "published-title-history.json"
    snapshot = json.dumps({"feed_url": FEED_URL, "titles": ["Cached title"]})
    if has_cache:
        cache_path.write_text(snapshot)
    client = FakeFeedClient(response=FakeFeedResponse(
        '<?xml version="1.0" encoding="unknown-encoding"?><rss><channel /></rss>'
    ))

    titles = load_published_titles(
        feed_url=FEED_URL, timeout_seconds=1, limit=20, cache_dir=tmp_path, client=client
    )

    assert titles == (["Cached title"] if has_cache else [])
    if has_cache:
        assert cache_path.read_text() == snapshot
    else:
        assert not cache_path.exists()
