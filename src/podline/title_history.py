import json
import logging
import math
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import httpx


LOGGER = logging.getLogger(__name__)
CACHE_FILENAME = "published-title-history.json"
MAX_FEED_BYTES = 1_000_000
MAX_TITLE_CHARACTERS = 200
MAX_TITLE_HISTORY_LIMIT = 100
_EPISODE_NUMBER_PREFIX = re.compile(r"^\s*\d+\s*[.)-]\s*")


def load_published_titles(
    *,
    feed_url: str,
    timeout_seconds: float,
    limit: int,
    cache_dir: Path,
) -> list[str]:
    """Return recent published titles, falling back to the matching disk cache."""
    feed_url = feed_url.strip()
    if not feed_url or limit <= 0:
        return []

    effective_limit = min(limit, MAX_TITLE_HISTORY_LIMIT)
    cache_path = cache_dir / CACHE_FILENAME
    cached_titles = _read_cache(
        cache_path,
        feed_url=feed_url,
        limit=effective_limit,
    )

    try:
        feed_content = _download_feed(
            feed_url=feed_url,
            timeout_seconds=timeout_seconds,
        )
        titles = _parse_published_titles(feed_content, limit=effective_limit)
        if not titles:
            raise ValueError("feed contained no episode titles")
    except (httpx.HTTPError, ET.ParseError, LookupError, OSError, ValueError) as exc:
        if cached_titles:
            LOGGER.warning(
                "Published-title feed unavailable; using %d cached titles: %s",
                len(cached_titles),
                exc,
            )
        else:
            LOGGER.warning(
                "Published-title feed unavailable and no cache exists; "
                "continuing without title history: %s",
                exc,
            )
        return cached_titles

    _write_cache(cache_path, feed_url=feed_url, titles=titles)
    return titles


def _download_feed(*, feed_url: str, timeout_seconds: float) -> bytes:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("feed timeout must be a positive finite number")

    request = json.dumps({
        "feed_url": feed_url,
        "timeout_seconds": timeout_seconds,
        "max_bytes": MAX_FEED_BYTES,
    }).encode("utf-8")
    # Use this checkout's worker even when another checkout is installed editable.
    worker_path = Path(__file__).resolve().with_name("_title_feed_worker.py")
    deadline = time.monotonic() + timeout_seconds
    with subprocess.Popen(
        [sys.executable, str(worker_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as worker:
        try:
            content, error = worker.communicate(
                input=request, timeout=max(0, deadline - time.monotonic())
            )
        except subprocess.TimeoutExpired as exc:
            raise httpx.TimeoutException(
                "published-title feed exceeded total timeout",
                request=httpx.Request("GET", feed_url),
            ) from exc
        finally:
            # This also runs on interruption; never leave a resolver or request alive.
            if worker.poll() is None:
                worker.kill()
            worker.wait()

    if worker.returncode:
        detail = error.decode("utf-8", errors="replace").strip()[:1024]
        raise ValueError(f"feed download failed (exit {worker.returncode}): {detail}")
    if len(content) > MAX_FEED_BYTES:
        raise ValueError(f"feed exceeded {MAX_FEED_BYTES} byte response limit")
    return content


def _parse_published_titles(feed_content: bytes, *, limit: int) -> list[str]:
    root = ET.fromstring(feed_content)
    titles: list[str] = []
    seen: set[str] = set()
    for item in root.findall("./channel/item"):
        title = _normalize_title(item.findtext("title") or "")
        if len(title) > MAX_TITLE_CHARACTERS:
            continue
        key = title.casefold()
        if not title or key in seen:
            continue
        titles.append(title)
        seen.add(key)
        if len(titles) >= limit:
            break
    return titles


def _normalize_title(title: str) -> str:
    return _EPISODE_NUMBER_PREFIX.sub("", title).strip()


def _read_cache(cache_path: Path, *, feed_url: str, limit: int) -> list[str]:
    try:
        if cache_path.stat().st_size > MAX_FEED_BYTES:
            LOGGER.warning(
                "Published-title cache %s exceeds %d byte limit; ignoring it",
                cache_path,
                MAX_FEED_BYTES,
            )
            return []
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read published-title cache %s: %s", cache_path, exc)
        return []

    if not isinstance(payload, dict) or payload.get("feed_url") != feed_url:
        return []
    raw_titles = payload.get("titles")
    if not isinstance(raw_titles, list):
        return []

    titles: list[str] = []
    seen: set[str] = set()
    for raw_title in raw_titles:
        if not isinstance(raw_title, str):
            continue
        title = _normalize_title(raw_title)
        if len(title) > MAX_TITLE_CHARACTERS:
            continue
        key = title.casefold()
        if not title or key in seen:
            continue
        titles.append(title)
        seen.add(key)
        if len(titles) >= limit:
            break
    return titles


def _write_cache(cache_path: Path, *, feed_url: str, titles: list[str]) -> None:
    payload = {
        "feed_url": feed_url,
        "titles": titles,
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        LOGGER.warning("Could not write published-title cache %s: %s", cache_path, exc)
