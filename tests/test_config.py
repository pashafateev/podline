from pathlib import Path

from sexdrunks_automation.config import load_config


def test_load_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime_home: /tmp/sexdrunks-automation
paths:
  raw_root: /tmp/raw
  deliverables_root: /tmp/deliverables
polling:
  interval_seconds: 30
notifications:
  enabled: true
auphonic:
  enabled: false
  api_key: ""
  base_url: https://auphonic.example/api
  request_timeout_seconds: 600
  poll_interval_seconds: 10
  max_poll_attempts: 180
  preset_ids:
    speakers_2: ""
    speakers_3: ""
    speakers_4: ""
podbean:
  enabled: false
  client_id: ""
  client_secret: ""
  base_url: https://api.podbean.example/v1
  podcast_id: ""
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.runtime_home == Path("/tmp/sexdrunks-automation")
    assert config.paths.raw_root == Path("/tmp/raw")
    assert config.polling.interval_seconds == 30
    assert config.auphonic.base_url == "https://auphonic.example/api"
    assert config.auphonic.max_poll_attempts == 180
    assert config.auphonic.request_timeout_seconds == 600
    assert config.podbean.base_url == "https://api.podbean.example/v1"


def test_load_config_overlays_local_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime_home: /tmp/sexdrunks-automation
paths:
  raw_root: /tmp/raw
  deliverables_root: /tmp/deliverables
polling:
  interval_seconds: 30
notifications:
  enabled: true
auphonic:
  enabled: false
  api_key: ""
  base_url: https://auphonic.example/api
  request_timeout_seconds: 600
  poll_interval_seconds: 10
  max_poll_attempts: 180
  preset_ids:
    speakers_2: ""
    speakers_3: ""
    speakers_4: ""
podbean:
  enabled: false
  client_id: ""
  client_secret: ""
  base_url: https://api.podbean.example/v1
  podcast_id: ""
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "config.local.yaml").write_text(
        """
paths:
  raw_root: /tmp/real-recordings
auphonic:
  enabled: true
  api_key: secret-token
  request_timeout_seconds: 1200
  preset_ids:
    speakers_2: preset-2
podbean:
  podcast_id: my-podcast-id
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.paths.raw_root == Path("/tmp/real-recordings")
    assert config.auphonic.enabled is True
    assert config.auphonic.api_key == "secret-token"
    assert config.auphonic.request_timeout_seconds == 1200
    assert config.auphonic.preset_ids["speakers_2"] == "preset-2"
    assert config.auphonic.preset_ids["speakers_3"] == ""
    assert config.podbean.podcast_id == "my-podcast-id"
