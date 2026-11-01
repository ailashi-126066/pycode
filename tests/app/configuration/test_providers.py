import json
from dataclasses import asdict

import pytest
from tests.models.adapters.test_factory import configuration

from pycode.app.configuration.providers import load_providers
from pycode.kernel.domain.errors import DomainError


def write_config(tmp_path, data):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_multiple_provider_instances_have_distinct_stable_names(tmp_path):
    first = asdict(configuration("deepseek"))
    second = {**first, "name": "second-instance", "model_id": "unknown-configured-model"}
    configs = load_providers(write_config(tmp_path, [first, second]))
    assert [config.name for config in configs] == ["deepseek-instance", "second-instance"]
    assert configs[1].model_id == "unknown-configured-model"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key_env", "raw-key-would-not-be-an-environment-reference"),
        ("base_url", "https://user:secret@example.invalid"),
        ("base_url", "https://example.invalid?key=secret"),
        ("context_window", 100),
        ("max_output_tokens", 0),
        ("timeout_seconds", -1),
        ("supports_tools", "yes"),
        ("implementation", "dynamic-provider"),
        ("openai_api", "responses"),
        ("thinking", {"type": "enabled"}),
        ("base_url", "https://[broken"),
        ("base_url", "https://example.invalid:99999"),
        ("base_url", "http://api.example.invalid/v1"),
        ("base_url", "http://192.168.1.10:8000/v1"),
        ("base_url", "http://127.0.0.1.example.invalid/v1"),
        ("base_url", "http://localhost.example.invalid"),
        ("api_key", "must-not-be-stored"),
    ],
)
def test_invalid_provider_configuration_is_rejected_once_at_boundary(tmp_path, field, value):
    data = asdict(configuration("deepseek"))
    data[field] = value
    with pytest.raises(DomainError) as error:
        load_providers(write_config(tmp_path, [data]))
    assert error.value.code == "invalid_provider_config"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.invalid/v1",
        "http://127.0.0.1:8080/v1",
        "http://127.8.9.10/v1",
        "http://localhost:11434",
        "http://LOCALHOST.:11434",
        "http://[::1]:8000/v1",
    ],
)
def test_plain_http_is_allowed_only_for_loopback_provider_hosts(tmp_path, base_url):
    data = asdict(configuration("deepseek"))
    data["base_url"] = base_url
    assert load_providers(write_config(tmp_path, [data]))[0].base_url == base_url


def test_duplicate_provider_names_and_implicit_openai_mode_are_rejected(tmp_path):
    data = asdict(configuration("openai"))
    with pytest.raises(DomainError):
        load_providers(write_config(tmp_path, [data, data]))
    data["openai_api"] = None
    with pytest.raises(DomainError):
        load_providers(write_config(tmp_path, [data]))


def test_invalid_thinking_object_does_not_escape_as_programming_error(tmp_path):
    data = asdict(configuration("deepseek"))
    data.update(supports_thinking=True, thinking={"type": []})
    with pytest.raises(DomainError):
        load_providers(write_config(tmp_path, [data]))


def test_partial_model_override_is_validated_and_unknown_fields_are_rejected(tmp_path):
    data = asdict(configuration("deepseek"))
    data["model_overrides"] = {"new-model": {"context_window": 64000}}
    providers = load_providers(write_config(tmp_path, [data]))
    assert providers[0].model_overrides["new-model"].supports_tools is None
    data["model_overrides"]["new-model"]["misspelled_feature"] = True
    with pytest.raises(DomainError):
        load_providers(write_config(tmp_path, [data]))


@pytest.mark.parametrize(
    "selector",
    [
        {"max_tools": 0},
        {"candidate_threshold": True},
        {"enabled": "yes"},
        {"input_limit": -1},
        {"always_include": ["Bash", "Bash"]},
        {"on_failure": "silent"},
        {"unknown": 1},
        {"model": {"provider": "missing", "model_id": "model"}},
        {"model": {"provider": "openai-instance", "model_id": "model", "api_key": "unsafe"}},
    ],
)
def test_invalid_selector_configuration_is_rejected_at_boundary(tmp_path, selector):
    data = asdict(configuration("openai"))
    data["tool_selector"] = selector
    with pytest.raises(DomainError) as error:
        load_providers(write_config(tmp_path, [data]))
    assert error.value.code == "invalid_provider_config"


def test_selector_model_reference_and_limits_are_loaded_without_new_credentials(tmp_path):
    data = asdict(configuration("openai"))
    data["tool_selector"] = {
        "model": {"provider": "openai-instance", "model_id": "configured-model"},
        "max_tools": 3,
        "always_include": ["read"],
    }
    with pytest.raises(DomainError, match="Provider configuration"):
        load_providers(write_config(tmp_path, [data]))


@pytest.mark.parametrize(
    "references",
    [
        [],
        [{"provider": "missing", "model_id": "model"}],
        [{"provider": "openai-instance", "model_id": "configured-model"}],
        [{"provider": "other", "model_id": "other-model"}] * 2,
        [{"provider": "other", "model_id": "other-model", "api_key": "not-allowed"}],
        [{"provider": "other", "model_id": "other-model"}] * 5,
    ],
)
def test_removed_fallback_option_is_explicitly_rejected(tmp_path, references):
    first = asdict(configuration("openai"))
    second = {**first, "name": "other", "model_id": "other-model"}
    first["fallback_models"] = references
    with pytest.raises(DomainError) as error:
        load_providers(write_config(tmp_path, [first, second]))
    assert error.value.code == "unsupported_provider_option"
