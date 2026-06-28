import logging
from datetime import datetime, timezone
from typing import Optional

from podline.fingerprint import build_fingerprint, build_session_uid
from podline.models import ScanOutcome
from podline.repository import create_session, get_session_by_fingerprint
from podline.scanner import scan_raw_sessions


def scan_and_ingest(
    raw_root, db_file: str, logger: Optional[logging.Logger] = None
) -> ScanOutcome:
    scan_result = scan_raw_sessions(raw_root, logger=logger)
    created = 0
    duplicates = 0

    for candidate in scan_result.candidates:
        fingerprint = build_fingerprint(candidate)
        existing = get_session_by_fingerprint(db_file, fingerprint)
        if existing is not None:
            duplicates += 1
            continue

        timestamp = datetime.now(timezone.utc).isoformat()
        create_session(
            db_file,
            session_uid=build_session_uid(fingerprint),
            fingerprint=fingerprint,
            candidate=candidate,
            timestamp=timestamp,
        )
        created += 1

    return ScanOutcome(
        discovered=len(scan_result.candidates),
        created=created,
        duplicates=duplicates,
        unsupported=scan_result.unsupported_sessions,
    )
