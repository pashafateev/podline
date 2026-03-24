from datetime import datetime, timezone

from sexdrunks_automation.fingerprint import build_fingerprint, build_session_uid
from sexdrunks_automation.models import ScanOutcome
from sexdrunks_automation.repository import create_session, get_session_by_fingerprint
from sexdrunks_automation.scanner import scan_raw_sessions


def scan_and_ingest(raw_root, db_file: str) -> ScanOutcome:
    scan_result = scan_raw_sessions(raw_root)
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
