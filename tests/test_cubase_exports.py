from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from podline import cli
from podline.config import load_config
from podline.finalize import FinalizeError


@pytest.fixture
def config(tmp_path):
    paths = SimpleNamespace(
        cubase_projects_root=tmp_path / "Cubase projects",
        deliverables_root=tmp_path / "deliverables",
    )
    return SimpleNamespace(paths=paths)


def export(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.touch()
    return path


def test_show_config_expands_cubase_projects_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = Path(__file__).resolve().parents[1] / "config.yaml"
    config_file = tmp_path / "config.yaml"
    config_file.write_bytes(path.read_bytes())
    expected = (
        tmp_path / "Documents/Documents - Pasha’s MacBook Pro (2)"
        / "Steinberg/Cubase/Projects/sexdrunks-podcast"
    )
    assert load_config(config_file).paths.cubase_projects_root == expected
    data = yaml.safe_load(config_file.read_text())
    del data["paths"]["cubase_projects_root"]
    config_file.write_text(yaml.safe_dump(data))
    assert load_config(config_file).paths.cubase_projects_root is None


def test_episode_shortcut_resolves_only_requested_project(config):
    mixdown = config.paths.cubase_projects_root / "ep52" / "Mixdown"
    master = export(mixdown, "ep52.mp3")
    export(mixdown, "notes.txt")
    export(mixdown, "alternate.m4a")
    export(config.paths.cubase_projects_root / "ep53" / "Mixdown", "ep53.mp3")
    assert cli._resolve_finalize_master(config, Path("ep52")) == master


@pytest.mark.parametrize("names", [[], ["alternate.wav"], ["alternate.wav", "alt mix.AIF"]])
def test_episode_shortcut_requires_explicit_choice_on_missing_or_ambiguous_export(config, names):
    mixdown = config.paths.cubase_projects_root / "ep52" / "Mixdown"
    paths = [export(mixdown, name) for name in names]
    with pytest.raises(FinalizeError) as error:
        cli._resolve_finalize_master(config, Path("ep52"))
    assert error.value.candidates == tuple(sorted(paths))
    assert str(mixdown / "ep52.mp3") in str(error.value)
    for path in paths:
        assert str(path) in str(error.value)
        assert cli._resolve_finalize_master(config, path) == path
