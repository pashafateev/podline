"""Auphonic remote-input pilot.

Validates how a saved, unstarted Auphonic multitrack production can read
audio from Podline's S3 bucket. Two transports:

External-service mode (--service <uuid>) — the selected design: the bucket
is registered once in Auphonic (Settings → External Services, backed by the
read-only podline-auphonic IAM user) and tracks attach by short key path.
No presigning, no expiry, no URL-length ceiling. This mode validates
attach, WebM handling, and start → done end to end.

Presigned-URL mode (default, kept for reference) — run 1 finding: Auphonic's
input_file column is varchar(512); URLs signed with session credentials are
~1,400 chars and get rejected with a 500. Only long-term IAM-user keys
produce URLs that fit (~240 chars). This mode attaches short-expiry URLs,
starts after they expire, and on failure validates re-sign + re-attach.

Use --list-services to print registered Auphonic services and their UUIDs.

Costs a few seconds of Auphonic processing credit at most. Presigned URLs
are never printed; output shows redacted forms only.

Usage:
    python scripts/auphonic_remote_input_pilot.py --list-services
    python scripts/auphonic_remote_input_pilot.py --bucket podline-media --service <uuid>
    python scripts/auphonic_remote_input_pilot.py --bucket <bucket> [--region us-west-2]
        [--expiry-seconds 120] [--skip-start] [--keep] [--config ~/podline/config.yaml]
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, List

import boto3
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from podline.config import StorageConfig, load_config  # noqa: E402
from podline.storage import MediaStorage, redact_url, sha256_of_file  # noqa: E402

POLL_INTERVAL_SECONDS = 10


def main() -> int:
    args = _parse_args()
    config = load_config(args.config.expanduser().resolve())
    if not config.auphonic.api_key.strip():
        print("auphonic.api_key is missing in config; cannot run pilot")
        return 2

    auphonic = _AuphonicApi(
        base_url=config.auphonic.base_url,
        api_key=config.auphonic.api_key,
    )

    if args.list_services:
        services = auphonic.list_services()
        if not services:
            print("no external services registered in this Auphonic account")
        for service in services:
            print(
                f"{service.get('uuid')}  type={service.get('type')}  "
                f"name={service.get('display_name')}"
            )
        return 0

    if not args.bucket:
        print("--bucket is required unless --list-services is given")
        return 2

    storage = MediaStorage(
        StorageConfig(
            enabled=True,
            bucket=args.bucket,
            region=args.region,
            key_prefix="",
            profile=args.profile,
        )
    )
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    mode = "external-service" if args.service else "presigned-url"
    print(f"pilot run {run_id}: bucket={args.bucket} mode={mode}")

    source = args.input_file or _generate_test_tone()
    keys = []
    track_refs = []
    for track in ("tr1", "tr2"):
        key = f"pilot/{run_id}/{track}.webm"
        storage.upload_file(
            local_path=source,
            key=key,
            content_type="audio/webm",
            sha256=sha256_of_file(source),
        )
        keys.append(key)
        print(f"uploaded {storage.s3_uri(key)}")
        if args.service:
            track_refs.append(
                {
                    "type": "multitrack",
                    "id": track,
                    "service": args.service,
                    "input_file": key,
                }
            )
            print(f"attach   service={args.service} input_file={key}")
        else:
            url = storage.presigned_get_url(
                key=key, expires_seconds=args.expiry_seconds
            )
            track_refs.append({"type": "multitrack", "id": track, "input_file": url})
            print(f"signed   {redact_url(url)}")

    signed_at = time.monotonic()
    production = auphonic.create_production(
        {
            "metadata": {
                "title": f"podline remote-input pilot {run_id} — safe to delete"
            },
            "is_multitrack": True,
            "output_files": [{"format": "mp3"}],
            "multi_input_files": track_refs,
        }
    )
    production_id = production.get("uuid") or production.get("id")
    if not production_id:
        print("FAIL: create response had no production id:")
        print(json.dumps(_redact_node(production), indent=2)[:2000])
        return 1
    print(f"\ncreated unstarted production {production_id}")
    print(f"status page: {production.get('status_page') or 'n/a'}")
    _print_inspection("immediately after attach", auphonic.get_production(production_id))

    if not args.service:
        wait_seconds = (
            max(0, args.expiry_seconds - int(time.monotonic() - signed_at)) + 45
        )
        print(f"\nwaiting {wait_seconds}s for the signed URLs to expire...")
        time.sleep(wait_seconds)
        _print_inspection("after URL expiry", auphonic.get_production(production_id))

    if args.skip_start:
        print("\n--skip-start given; leaving production unstarted.")
        _cleanup(storage, keys, keep=args.keep)
        return 0

    verdict_lines = []
    if args.service:
        print("\nstarting production with external-service inputs...")
        outcome = _start_and_wait(auphonic, production_id, args.poll_minutes)
        if outcome == "done":
            verdict_lines.append(
                "VERDICT: external-service attach works end to end — tracks attached "
                "by key path, WebM accepted, production processed to done."
            )
        else:
            verdict_lines.append(
                f"VERDICT: external-service run did not complete (outcome={outcome}); "
                "inspect the production before building on this design."
            )
        _print_inspection("final state", auphonic.get_production(production_id))
        print()
        for line in verdict_lines:
            print(line)
        _cleanup(storage, keys, keep=args.keep)
        return 0 if outcome == "done" else 1

    print("\nstarting production with expired input URLs...")
    outcome = _start_and_wait(auphonic, production_id, args.poll_minutes)
    if outcome == "done":
        verdict_lines.append(
            "VERDICT: production completed although the URLs were expired —"
        )
        verdict_lines.append(
            "Auphonic fetches HTTP inputs at ATTACH time. auphonic-start needs no URL refresh."
        )
    else:
        print("\nstart failed with expired URLs; testing re-sign + re-attach...")
        refreshed = True
        for key, track in zip(keys, ("tr1", "tr2")):
            fresh = storage.presigned_get_url(key=key, expires_seconds=3600)
            try:
                auphonic.update_track_input(production_id, track, fresh)
                print(f"re-attached {track} with a fresh URL: {redact_url(fresh)}")
            except httpx.HTTPError as exc:
                refreshed = False
                print(f"re-attach for {track} FAILED: {exc}")
        if refreshed:
            outcome = _start_and_wait(auphonic, production_id, args.poll_minutes)
            if outcome == "done":
                verdict_lines.append(
                    "VERDICT: Auphonic fetches HTTP inputs at START time —"
                )
                verdict_lines.append(
                    "auphonic-start must re-sign and re-attach fresh URLs before starting. "
                    "Re-attach via POST /production/{id}/multi_input_files/{track}.json works."
                )
            else:
                verdict_lines.append(
                    "INCONCLUSIVE: start failed even after re-attaching fresh URLs; "
                    "inspect the production manually."
                )
        else:
            verdict_lines.append(
                "VERDICT: fetch-at-start AND the track-update endpoint rejected input_file "
                "changes — PR 2 must create the production fresh at start instead."
            )

    _print_inspection("final state", auphonic.get_production(production_id))
    print()
    for line in verdict_lines:
        print(line)
    _cleanup(storage, keys, keep=args.keep)
    return 0


class _AuphonicApi:
    def __init__(self, *, base_url: str, api_key: str) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(120, connect=30.0),
        )

    def create_production(self, payload: Dict) -> Dict:
        response = self._client.post(
            "/productions.json",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        return self._data(response)

    def get_production(self, production_id: str) -> Dict:
        return self._data(self._client.get(f"/production/{production_id}.json"))

    def list_services(self) -> List[Dict]:
        data = self._data(self._client.get("/services.json"))
        if isinstance(data, list):
            return data
        return data.get("data", []) if isinstance(data, dict) else []

    def start_production(self, production_id: str) -> Dict:
        return self._data(self._client.post(f"/production/{production_id}/start.json"))

    def update_track_input(
        self, production_id: str, track_id: str, input_url: str
    ) -> Dict:
        response = self._client.post(
            f"/production/{production_id}/multi_input_files/{track_id}.json",
            json={"type": "multitrack", "input_file": input_url},
            headers={"Content-Type": "application/json"},
        )
        return self._data(response)

    def _data(self, response: httpx.Response) -> Dict:
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise
        if response.status_code >= 400 or payload.get("status_code", 200) >= 400:
            raise httpx.HTTPError(
                f"Auphonic API error status={response.status_code} "
                f"body={json.dumps(_redact_node(payload))[:600]}"
            )
        data = payload.get("data", payload)
        return data if isinstance(data, dict) else payload


def _start_and_wait(api: _AuphonicApi, production_id: str, poll_minutes: int) -> str:
    try:
        api.start_production(production_id)
    except httpx.HTTPError as exc:
        print(f"start request failed: {exc}")
        return "start_rejected"

    deadline = time.monotonic() + poll_minutes * 60
    while time.monotonic() < deadline:
        data = api.get_production(production_id)
        status = str(data.get("status") or "")
        status_string = str(data.get("status_string") or "")
        error = data.get("error_message") or ""
        print(
            f"  poll: status={status} ({status_string})"
            + (f" error={redact_url(str(error))}" if error else "")
        )
        if status in {"3"} or status_string.lower() in {"done", "finished"}:
            return "done"
        if status in {"2", "11", "12"} or status_string.lower() in {
            "error",
            "failed",
            "incomplete",
        }:
            return "failed"
        time.sleep(POLL_INTERVAL_SECONDS)
    return "timeout"


def _print_inspection(label: str, data: Dict) -> None:
    print(f"\n--- production {label} ---")
    print(f"status: {data.get('status')} ({data.get('status_string')})")
    print(
        f"start_allowed={data.get('start_allowed')} change_allowed={data.get('change_allowed')}"
    )
    for track in data.get("multi_input_files") or []:
        print(
            "  track "
            f"id={track.get('id')} "
            f"input_file={redact_url(str(track.get('input_file') or 'none'))} "
            f"size={track.get('size')} length={track.get('input_length') or track.get('length')} "
            f"format={track.get('input_filetype') or track.get('format')}"
        )


def _redact_node(node):
    if isinstance(node, dict):
        return {key: _redact_node(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_redact_node(item) for item in node]
    if isinstance(node, str) and node.startswith("http"):
        return redact_url(node)
    return node


def _generate_test_tone() -> Path:
    destination = Path(tempfile.mkdtemp(prefix="podline-pilot-")) / "tone.webm"
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        str(destination),
    ]
    subprocess.run(command, check=True)
    print(f"generated test tone {destination}")
    return destination


def _cleanup(storage: MediaStorage, keys: List[str], *, keep: bool) -> None:
    if keep:
        print("\n--keep given; pilot objects left in the bucket.")
        return
    # Deletion needs broader credentials than the no-delete podline profile;
    # use the default chain and shrug if it lacks permission.
    try:
        client = boto3.client("s3")
        for key in keys:
            client.delete_object(Bucket=storage.bucket, Key=key)
        print(f"\ndeleted {len(keys)} pilot objects from s3://{storage.bucket}/pilot/")
    except Exception as exc:
        print(
            f"\ncleanup skipped ({exc}); remove s3://{storage.bucket}/pilot/ manually"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="")
    parser.add_argument("--region", default="")
    parser.add_argument("--profile", default="", help="AWS profile for S3 access.")
    parser.add_argument(
        "--service",
        default="",
        help="Auphonic external service UUID; attaches tracks by key path.",
    )
    parser.add_argument(
        "--list-services",
        action="store_true",
        help="Print registered Auphonic external services and exit.",
    )
    parser.add_argument("--expiry-seconds", type=int, default=120)
    parser.add_argument("--poll-minutes", type=int, default=5)
    parser.add_argument(
        "--config", type=Path, default=Path.home() / "podline" / "config.yaml"
    )
    parser.add_argument("--input-file", type=Path, default=None)
    parser.add_argument(
        "--skip-start",
        action="store_true",
        help="Only attach and inspect; never start processing.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Leave pilot objects in the bucket afterwards.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(main())
