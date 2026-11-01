import pytest

from pycode.sandbox import subprocess_environment, validate_passthrough


def test_allowlisted_names_and_locale_prefix_are_kept():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": "/tmp",
        "VIRTUAL_ENV": "/venv",
        "SystemRoot": "C:\\Windows",
        "EDITOR": "vim",
        "PYTHONPATH": "/elsewhere",
    }
    assert subprocess_environment(source) == {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": "/tmp",
        "VIRTUAL_ENV": "/venv",
        "SystemRoot": "C:\\Windows",
    }


@pytest.mark.parametrize(
    "name",
    [
        "FAKE_API_KEY",
        "OPENAI_API_KEY",
        "github_token",
        "CLIENT_SECRET",
        "LANGFUSE_SECRET_KEY",
        "DB_PASSWORD",
        "AWS_ACCESS_KEY_ID",
        "AWS_PROFILE",
        "LC_SECRET",
        "LC_TOKEN",
    ],
)
def test_credential_shaped_names_are_dropped(name):
    assert subprocess_environment({name: "value", "PATH": "/bin"}) == {"PATH": "/bin"}


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "API_KEY",
        "api_key",
        "APIKEY",
        "TOKEN",
        "token",
        "CREDENTIAL",
        "CREDENTIALS",
        "credentials",
        "AWS_REGION",
        "DB_PASSWORD",
        "*_PROXY",
        "A=B",
    ],
)
def test_explicit_passthrough_rejects_credentials_patterns_and_assignments(name):
    with pytest.raises(ValueError):
        validate_passthrough((name,))
