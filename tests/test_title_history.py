import json
from pathlib import Path

import httpx

from podline.title_history import load_published_titles


FEED_URL = "https://feed.podbean.com/sexdrunks/feed.xml"


class FakeFeedResponse:
    def __init__(self, text: str, *, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", FEED_URL)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "feed request failed", request=request, response=response
            )


class FakeFeedClient:
    def __init__(self, *, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
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
        (FEED_URL, {"timeout": 4, "follow_redirects": True})
    ]
    cached = json.loads((tmp_path / "published-title-history.json").read_text())
    assert cached == {
        "feed_url": FEED_URL,
        "titles": [
            "Raise Your Hand—or Don’t",
            "Sober Dating, Future Tripping",
            "Welcome Is Not the Same as Safe",
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
