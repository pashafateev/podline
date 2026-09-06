import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", FEED_URL)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "feed request failed", request=request, response=response
            )

    def iter_bytes(self, chunk_size=None):
        yield from self.chunks or [self.text.encode("utf-8")]


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


def test_load_published_titles_enforces_total_elapsed_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    clock = iter([100.0, 105.0])
    monkeypatch.setattr("podline.title_history.time.monotonic", lambda: next(clock))
    client = FakeFeedClient(response=FakeFeedResponse(_rss("A title")))

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
