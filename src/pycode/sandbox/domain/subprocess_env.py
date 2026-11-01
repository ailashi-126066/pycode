"""Child processes receive an allowlisted environment without inherited credentials."""

import re
from collections.abc import Mapping

# Names a shell, git, Python or a virtual environment needs to behave normally.
ALLOWED_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TMPDIR",
        "TZ",
        "VIRTUAL_ENV",
        # Windows processes cannot start Python, sockets or the shell without these.
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "SYSTEMDRIVE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    }
)
ALLOWED_PREFIXES = ("LC_",)
CREDENTIAL = re.compile(
    r"(?:.*_)?API_?KEY|(?:.*_)?TOKEN|.*SECRET.*|.*PASSWORD.*|.*PASSWD.*|"
    r"(?:.*_)?CREDENTIALS?|AWS_.*",
    re.IGNORECASE,
)


def credential_name(name: str) -> bool:
    return CREDENTIAL.fullmatch(name) is not None


def allowed_name(name: str) -> bool:
    upper = name.upper()
    if credential_name(upper):
        return False
    return upper in ALLOWED_NAMES or upper.startswith(ALLOWED_PREFIXES)


def passthrough_name(name: str) -> str:
    """Accept an exact environment name; never allow credential-shaped names."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Environment passthrough requires an exact variable name")
    if credential_name(name):
        raise ValueError("Credential variables cannot be passed to subprocesses")
    return name


def validate_passthrough(names: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(names, tuple):
        raise ValueError("Environment passthrough must be a tuple of exact names")
    return tuple(passthrough_name(name) for name in names)


def subprocess_environment(
    source: Mapping[str, str], *, passthrough: tuple[str, ...] = ()
) -> dict[str, str]:
    """Keep allowlisted names; credential-shaped names are dropped even if allowlisted."""
    extra = set(validate_passthrough(passthrough))
    return {
        name: value
        for name, value in source.items()
        if allowed_name(name) or name in extra and not credential_name(name)
    }
