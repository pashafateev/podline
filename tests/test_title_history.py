import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from podline.title_history import (
    MAX_FEED_BYTES,
    MAX_TITLE_CHARACTERS,
    load_published_titles,
)


FEED_URL = "https://feed.podbean.com/sexdrunks/feed.xml"


def _rss(*titles: str) -> bytes:
    items = "".join(f"<item><title>{title}</title></item>" for title in titles)
    return f"<?xml version='1.0'?><rss><channel>{items}</channel></rss>".encode("utf-8")


@pytest.fixture
def feed_download(monkeypatch):
    state = SimpleNamespace(content=_rss("A title"), error=None, calls=[])

    def download(**kwargs):
        state.calls.append(kwargs)
        if state.error is not None:
            raise state.error
        return state.content

    monkeypatch.setattr("podline.title_history._download_feed", download)
    return state


@pytest.fixture
def feed_server(monkeypatch):
    state = SimpleNamespace(
        content=_rss("A title"), status=200, mode="normal", requests=[],
        disconnected=threading.Event(), stop=threading.Event(),
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state.requests.append(self.path)
            try:
                if state.mode == "headers":
                    state.stop.wait(3)
                if state.mode == "redirects":
                    state.stop.wait(0.15)
                    self.send_response(302)
                    self.send_header("Location", self.path + "/next")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(state.status)
                if state.mode != "trickle":
                    self.send_header("Content-Length", str(len(state.content)))
                self.end_headers()
                if state.mode == "trickle":
                    # Individual writes stay below the read timeout; EOF does not.
                    for _ in range(150):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        if state.stop.wait(0.02):
                            break
                else:
                    self.wfile.write(state.content)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                state.disconnected.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    state.url = f"http://127.0.0.1:{server.server_port}/feed"
    monkeypatch.setenv("NO_PROXY", "*")
    try:
        yield state
    finally:
        state.stop.set()
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.fixture
def worker_processes(monkeypatch):
    processes = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("podline.title_history.subprocess.Popen", start)
    yield processes
    for process in processes:
        try:
            assert process.poll() is not None, "feed worker was not reaped"
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_load_published_titles_normalizes_deduplicates_limits_and_caches(
    tmp_path: Path, feed_download,
) -> None:
    feed_download.content = _rss(
        "32. Raise Your Hand—or Don’t", "31. Sober Dating, Future Tripping",
        "raise your hand—or don’t", "30. Welcome Is Not the Same as Safe",
    )
    titles = load_published_titles(
        feed_url=FEED_URL, timeout_seconds=4, limit=2, cache_dir=tmp_path,
    )
    assert titles == ["Raise Your Hand—or Don’t", "Sober Dating, Future Tripping"]
    assert feed_download.calls == [{"feed_url": FEED_URL, "timeout_seconds": 4}]
    assert json.loads((tmp_path / "published-title-history.json").read_text()) == {
        "feed_url": FEED_URL, "titles": titles,
    }


def test_load_published_titles_uses_matching_cache_when_feed_is_unavailable(
    tmp_path: Path, feed_download,
) -> None:
    feed_download.content = _rss("28. I met God at the car dealership")
    load_published_titles(feed_url=FEED_URL, timeout_seconds=4, limit=20, cache_dir=tmp_path)
    snapshot = (tmp_path / "published-title-history.json").read_bytes()
    feed_download.error = httpx.ConnectError("offline")
    assert load_published_titles(
        feed_url=FEED_URL, timeout_seconds=4, limit=20, cache_dir=tmp_path,
    ) == ["I met God at the car dealership"]
    assert (tmp_path / "published-title-history.json").read_bytes() == snapshot


@pytest.mark.parametrize("cache_bytes", [
    json.dumps({"feed_url": "https://other.example/feed.xml", "titles": ["Old"]}).encode(),
    b"\xff\xfe", b"x" * (MAX_FEED_BYTES + 1), b"{invalid JSON",
])
def test_load_published_titles_ignores_unusable_cache_when_feed_is_unavailable(
    tmp_path: Path, feed_download, cache_bytes,
) -> None:
    (tmp_path / "published-title-history.json").write_bytes(cache_bytes)
    feed_download.error = httpx.ConnectError("offline")
    assert load_published_titles(
        feed_url=FEED_URL, timeout_seconds=4, limit=20, cache_dir=tmp_path,
    ) == []


def test_load_published_titles_skips_oversized_titles(tmp_path: Path, feed_download) -> None:
    feed_download.content = _rss("x" * (MAX_TITLE_CHARACTERS + 1), "28. A usable title")
    assert load_published_titles(
        feed_url=FEED_URL, timeout_seconds=4, limit=20, cache_dir=tmp_path,
    ) == ["A usable title"]


def test_load_published_titles_skips_disabled_or_zero_limit_feed(
    tmp_path: Path, feed_download,
) -> None:
    assert load_published_titles(
        feed_url="", timeout_seconds=4, limit=20, cache_dir=tmp_path,
    ) == []
    assert load_published_titles(
        feed_url=FEED_URL, timeout_seconds=4, limit=0, cache_dir=tmp_path,
    ) == []
    assert feed_download.calls == []


def test_worker_refreshes_saved_titles_on_each_successful_fetch(
    tmp_path: Path, feed_server, worker_processes, monkeypatch,
) -> None:
    # Launch must use this checkout even from an unrelated working directory.
    monkeypatch.chdir(tmp_path)
    for title in ("First title", "Updated title"):
        feed_server.content = _rss("42. " + title)
        assert load_published_titles(
            feed_url=feed_server.url, timeout_seconds=3, limit=20, cache_dir=tmp_path,
        ) == [title]
        assert json.loads((tmp_path / "published-title-history.json").read_text()) == {
            "feed_url": feed_server.url, "titles": [title],
        }
    assert len(worker_processes) == 2
    assert all(process.returncode == 0 for process in worker_processes)
    assert len(feed_server.requests) == 2


@pytest.mark.parametrize("failure", ["oversize", "http_error"])
def test_worker_download_failure_preserves_cache(
    tmp_path: Path, feed_server, worker_processes, failure,
) -> None:
    if failure == "oversize":
        feed_server.content = b"x" * (MAX_FEED_BYTES + 1)
    else:
        feed_server.status = 503
    cache_path = tmp_path / "published-title-history.json"
    snapshot = json.dumps({"feed_url": feed_server.url, "titles": ["Cached title"]})
    cache_path.write_text(snapshot)
    assert load_published_titles(
        feed_url=feed_server.url, timeout_seconds=3, limit=20, cache_dir=tmp_path,
    ) == ["Cached title"]
    assert cache_path.read_text() == snapshot
    assert worker_processes[0].returncode != 0


@pytest.mark.parametrize("mode", ["trickle", "headers", "redirects"])
def test_feed_deadline_interrupts_network_wait_and_reaps_worker(
    tmp_path: Path, feed_server, worker_processes, mode,
) -> None:
    feed_server.mode = mode
    has_cache = mode != "headers"
    cache_path = tmp_path / "published-title-history.json"
    snapshot = json.dumps({"feed_url": feed_server.url, "titles": ["Cached title"]})
    if has_cache:
        cache_path.write_text(snapshot)
    started = time.monotonic()
    titles = load_published_titles(
        feed_url=feed_server.url, timeout_seconds=0.5, limit=20, cache_dir=tmp_path,
    )
    elapsed = time.monotonic() - started
    assert titles == (["Cached title"] if has_cache else [])
    assert feed_server.requests, "test never reached the server"
    assert elapsed < 1.5, f"deadline failed to interrupt download: {elapsed:.3f}s"
    assert worker_processes[0].returncode != 0
    if mode == "trickle":
        assert feed_server.disconnected.wait(1), "timed-out response was not closed"
    if mode == "redirects":
        assert len(feed_server.requests) > 1, "test never followed a redirect"
    if has_cache:
        assert cache_path.read_text() == snapshot
    else:
        assert not cache_path.exists()


@pytest.mark.parametrize("has_cache", [True, False])
def test_feed_deadline_interrupts_slow_dns(monkeypatch, tmp_path: Path, has_cache, worker_processes) -> None:
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
def test_unsupported_xml_encoding_falls_back(tmp_path: Path, has_cache, feed_download) -> None:
    cache_path = tmp_path / "published-title-history.json"
    snapshot = json.dumps({"feed_url": FEED_URL, "titles": ["Cached title"]})
    if has_cache:
        cache_path.write_text(snapshot)
    feed_download.content = b'<?xml version="1.0" encoding="unknown-encoding"?><rss><channel /></rss>'
    titles = load_published_titles(
        feed_url=FEED_URL, timeout_seconds=1, limit=20, cache_dir=tmp_path,
    )
    assert titles == (["Cached title"] if has_cache else [])
    if has_cache:
        assert cache_path.read_text() == snapshot
    else:
        assert not cache_path.exists()


def test_worker_launch_failure_uses_cache(monkeypatch, tmp_path: Path) -> None:
    def unavailable(*args, **kwargs):
        raise OSError("cannot start worker")

    monkeypatch.setattr("podline.title_history.subprocess.Popen", unavailable)
    (tmp_path / "published-title-history.json").write_text(
        json.dumps({"feed_url": FEED_URL, "titles": ["Cached title"]})
    )
    assert load_published_titles(
        feed_url=FEED_URL, timeout_seconds=1, limit=20, cache_dir=tmp_path,
    ) == ["Cached title"]


def test_interrupted_download_reaps_worker(monkeypatch, tmp_path: Path) -> None:
    processes = []
    popen = subprocess.Popen

    def interrupted(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)

        def communicate(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(process, "communicate", communicate)
        return process

    monkeypatch.setattr("podline.title_history.subprocess.Popen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        load_published_titles(
            feed_url=FEED_URL, timeout_seconds=1, limit=20, cache_dir=tmp_path,
        )
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert not (tmp_path / "published-title-history.json").exists()
