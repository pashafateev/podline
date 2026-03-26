import struct
import shutil
import wave
from pathlib import Path

import pytest


@pytest.fixture()
def fixture_raw_root(tmp_path: Path) -> Path:
    source_root = Path(__file__).parent / "fixtures" / "raw"
    flat_root = tmp_path / "raw"
    flat_root.mkdir()

    for date_dir in source_root.iterdir():
        if not date_dir.is_dir():
            continue
        for session_dir in date_dir.iterdir():
            if session_dir.is_dir():
                shutil.copytree(session_dir, flat_root / session_dir.name)

    return flat_root


@pytest.fixture()
def wav_writer():
    def _write(path: Path, samples) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(44100)
            frames = b"".join(struct.pack("<h", int(sample)) for sample in samples)
            wav_file.writeframes(frames)
        return path

    return _write
