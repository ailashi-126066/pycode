import json
from dataclasses import replace

import pytest

from pycode.kernel import ModelError
from pycode.models.catalog import SDKModelDirectory
from pycode.models.requests import ProviderFailureClassifier
from tests.models.test_factory import configuration
from tests.support.http_provider import HTTPResponse, protocol_server
from tests.support.requests import TestTiming


@pytest.mark.parametrize("kind", ["deepseek", "openai", "anthropic"])
async def test_directory_uses_real_public_sdk_http_requests(kind):
    entry = {"id": "new-model", "object": "model", "created": 1, "owned_by": "test"}
    payload = {"object": "list", "data": [entry]}
    if kind == "anthropic":
        payload = {
            "data": [
                {
                    "id": "new-model",
                    "type": "model",
                    "display_name": "New model",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
            "has_more": False,
            "first_id": "new-model",
            "last_id": "new-model",
        }
    response = HTTPResponse(json.dumps(payload).encode(), content_type="application/json")
    with protocol_server([response]) as (url, received):
        provider = replace(
            configuration(kind), base_url=url if kind == "anthropic" else url + "/v1"
        )
        directory = SDKModelDirectory(
            {"PYCODE_TEST_API_KEY": "fake-key"}, ProviderFailureClassifier(TestTiming())
        )
        models = await directory.list_models(provider)
        assert models[0].model_id == "new-model"
        assert not models[0].capabilities.supports_tools
        assert received[0]["method"] == "GET"
        assert received[0]["path"].split("?")[0] == "/v1/models"


async def test_directory_authentication_error_has_no_silent_model_fallback():
    response = HTTPResponse(
        b'{"error":{"message":"fake-key"}}', status=401, content_type="application/json"
    )
    with protocol_server([response]) as (url, received):
        provider = replace(configuration("openai"), base_url=url + "/v1")
        directory = SDKModelDirectory(
            {"PYCODE_TEST_API_KEY": "fake-key"}, ProviderFailureClassifier(TestTiming())
        )
        with pytest.raises(ModelError) as error:
            await directory.list_models(provider)
        assert error.value.code == "provider_http_401"
        assert "fake-key" not in str(error.value)
        assert len(received) == 1


async def test_anthropic_model_directory_follows_sdk_pagination():
    def page(model_id, more):
        payload = {
            "data": [
                {
                    "id": model_id,
                    "type": "model",
                    "display_name": model_id,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
            "has_more": more,
            "first_id": model_id,
            "last_id": model_id,
        }
        return HTTPResponse(json.dumps(payload).encode(), content_type="application/json")

    with protocol_server([page("first", True), page("second", False)]) as (url, received):
        directory = SDKModelDirectory(
            {"PYCODE_TEST_API_KEY": "fake-key"}, ProviderFailureClassifier(TestTiming())
        )
        models = await directory.list_models(replace(configuration("anthropic"), base_url=url))
        assert [model.model_id for model in models] == ["first", "second"]
        assert len(received) == 2
        assert "after_id=first" in received[1]["path"]
