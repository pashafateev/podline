"""Serialize local workflow operations; the OS releases locks on process exit."""
from contextlib import contextmanager
import fcntl
from hashlib import sha256
from pathlib import Path


class WorkflowBusy(RuntimeError):
    pass


@contextmanager
def session_lock(db_file, session_uid: str, operation: str):
    database = Path(db_file)
    key = sha256(session_uid.encode()).hexdigest()
    lockfile = database.with_name(f"{database.name}.{key}.{operation}.lock")
    with lockfile.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkflowBusy(f"{operation} is already running for {session_uid}.") from exc
        yield
