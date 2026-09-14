"""Download bounded RSS bytes in a process the caller can terminate during DNS."""

import json
import sys

import httpx


def download_feed(*, feed_url: str, timeout_seconds: float, max_bytes: int) -> bytes:
    content = bytearray()
    with httpx.stream(
        "GET", feed_url, timeout=timeout_seconds, follow_redirects=True,
        headers={"Accept-Encoding": "identity"},
    ) as response:
        response.raise_for_status()
        if response.headers.get("Content-Encoding", "identity").strip().lower() not in {
            "", "identity",
        }:
            raise ValueError("compressed title feeds are unsupported")
        # Never invoke HTTPX's decompressor before applying the byte cap.
        for chunk in response.iter_raw(chunk_size=64 * 1024):
            if len(content) + len(chunk) > max_bytes:
                raise ValueError(f"feed exceeded {max_bytes} byte response limit")
            content.extend(chunk)
    return bytes(content)


def main() -> int:
    try:
        request = json.load(sys.stdin)
        content = download_feed(**request)
        sys.stdout.buffer.write(content)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        # stdout is exclusively bounded feed bytes; failures have a short diagnostic.
        print(str(exc)[:1024], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
