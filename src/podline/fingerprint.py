import hashlib
import uuid
from pathlib import Path

from podline.models import ScanCandidate


TRACK_HASH_BYTES = 65536


def build_fingerprint(candidate: ScanCandidate) -> str:
    digest = hashlib.sha256()
    digest.update(str(len(candidate.spoken_tracks)).encode("ascii"))

    for track in sorted(candidate.spoken_tracks, key=lambda item: item.track_name):
        digest.update(b"|")
        digest.update(track.track_name.encode("utf-8"))
        digest.update(b":")
        digest.update(str(track.file_size).encode("ascii"))
        digest.update(b":")
        digest.update(_build_track_content_signature(track.path).encode("ascii"))

    if candidate.mix_track is not None:
        digest.update(b"|mix:")
        digest.update(str(candidate.mix_track.file_size).encode("ascii"))
        digest.update(b":")
        digest.update(
            _build_track_content_signature(candidate.mix_track.path).encode("ascii")
        )

    return digest.hexdigest()


def build_session_uid(fingerprint: str) -> str:
    return f"sd-{uuid.uuid5(uuid.NAMESPACE_URL, fingerprint)}"


def _build_track_content_signature(path: Path) -> str:
    file_size = path.stat().st_size
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        if file_size <= TRACK_HASH_BYTES * 2:
            digest.update(handle.read())
        else:
            digest.update(handle.read(TRACK_HASH_BYTES))
            handle.seek(-TRACK_HASH_BYTES, 2)
            digest.update(handle.read(TRACK_HASH_BYTES))

    return digest.hexdigest()
