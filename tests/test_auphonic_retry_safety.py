import httpx
import pytest

from podline.auphonic import AuphonicClient, AuphonicError


@pytest.mark.parametrize("operation", ["create", "start", "refresh"])
@pytest.mark.parametrize("failure", ["timeout", "server-error"])
def test_non_idempotent_requests_are_not_blindly_retried(
    app_config_factory, tmp_path, monkeypatch, operation, failure,
):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(503, text="unknown remote outcome")

    monkeypatch.setattr("podline.auphonic.sleep", lambda _: None)
    client = AuphonicClient(app_config_factory(tmp_path))
    client._client = httpx.Client(
        base_url="https://auphonic.invalid", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AuphonicError):
        if operation == "create":
            client.create_production(title="episode part", track_count=2, preset_id="p2")
        elif operation == "start":
            client.start_production("production")
        else:
            client.create_single_input_production(
                title="final transcript", preset_id="vtt",
                service_id="s3", input_key="final.wav",
            )
    assert len(requests) == 1


def test_read_only_inspection_still_retries(app_config_factory, tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"uuid": "production"}})

    monkeypatch.setattr("podline.auphonic.sleep", lambda _: None)
    client = AuphonicClient(app_config_factory(tmp_path))
    client._client = httpx.Client(
        base_url="https://auphonic.invalid", transport=httpx.MockTransport(handler),
    )
    assert client.get_production("production")["data"]["uuid"] == "production"
    assert len(requests) == 2


def test_definitive_rejection_is_not_reported_as_unknown_creation(app_config_factory, tmp_path):
    client = AuphonicClient(app_config_factory(tmp_path))
    client._client = httpx.Client(
        base_url="https://auphonic.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(401, text="bad credentials")),
    )
    with pytest.raises(AuphonicError) as caught:
        client.create_production(title="episode", track_count=2, preset_id="p2")
    assert caught.value.rejected is True


def test_created_id_survives_invalid_preset_metadata(app_config_factory, tmp_path):
    client = AuphonicClient(app_config_factory(tmp_path))
    client._client = httpx.Client(
        base_url="https://auphonic.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": {
            "uuid": "known-created-production",
            "multi_input_files": [{"id": "one"}, {"id": "two"}, {"id": "three"}],
        }})),
    )
    with pytest.raises(AuphonicError) as caught:
        client.create_production(title="episode", track_count=2, preset_id="p2")
    assert caught.value.production_id == "known-created-production"
