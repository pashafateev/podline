import json
import logging
from pathlib import Path
import re
from typing import Any, Optional
import xml.etree.ElementTree as ET

import httpx


LOGGER = logging.getLogger(__name__)
CACHE_FILENAME = "published-title-history.json"
_EPISODE_NUMBER_PREFIX = re.compile(r"^\s*\d+\s*[.)-]\s*")


def load_published_titles(
    *,
    feed_url: str,
    timeout_seconds: float,
    limit: int,
    cache_dir: Path,
    client: Optional[Any] = None,
) -> list[str]:
    """Return recent published titles, falling back to the matching disk cache."""
    feed_url = feed_url.strip()
    if not feed_url or limit <= 0:
        return []

    cache_path = cache_dir / CACHE_FILENAME
    cached_titles = _read_cache(cache_path, feed_url=feed_url)
    getter = client.get if client is not None else httpx.get

    try:
        response = getter(
            feed_url,
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        titles = _parse_published_titles(response.text)
        if not titles:
            raise ValueError("feed contained no episode titles")
    except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
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
        return cached_titles[:limit]

    _write_cache(cache_path, feed_url=feed_url, titles=titles)
    return titles[:limit]


def _parse_published_titles(feed_text: str) -> list[str]:
    root = ET.fromstring(feed_text)
    titles: list[str] = []
    seen: set[str] = set()
    for item in root.findall("./channel/item"):
        title = _normalize_title(item.findtext("title") or "")
        key = title.casefold()
        if not title or key in seen:
            continue
        titles.append(title)
        seen.add(key)
    return titles


def _normalize_title(title: str) -> str:
    return _EPISODE_NUMBER_PREFIX.sub("", title).strip()


def _read_cache(cache_path: Path, *, feed_url: str) -> list[str]:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
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
        key = title.casefold()
        if not title or key in seen:
            continue
        titles.append(title)
        seen.add(key)
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
