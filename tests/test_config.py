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
