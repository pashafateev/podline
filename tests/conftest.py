import struct
import wave
from pathlib import Path

import pytest


@pytest.fixture()
def fixture_raw_root() -> Path:
    return Path(__file__).parent / "fixtures" / "raw"


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
