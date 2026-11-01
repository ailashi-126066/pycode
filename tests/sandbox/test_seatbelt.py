import asyncio
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from pycode.kernel import ToolError
from pycode.sandbox import Sandbox, SandboxSettings, build_profile


def test_profile_preserves_source_read_network_and_quoted_path_rules(tmp_path):
    root = str(tmp_path / 'quoted"path')
    profile = build_profile(root, False)
    assert '(allow file-read* (subpath "/"))' not in profile
    assert json.dumps(str(Path(root).resolve()), ensure_ascii=False) in profile
    assert "(deny network*)" in profile and ".git" in profile and ".pycode" in profile
    assert "(deny file-write-unlink" in profile
    assert "(allow network*)" in build_profile(root, True)
    assert '(allow file-write-data (literal "/dev/null"))' in profile


def test_dotenv_example_exception_escapes_workspace_regex_metacharacters(tmp_path):
    workspace = tmp_path / "project.[1](x)+$^"
    profile = build_profile(workspace, False)
    rule = next(
        line for line in profile.splitlines() if line.startswith("(allow file-read-data (regex")
    )
    pattern = re.search(r'\(regex #"(.*)"\)', rule).group(1)
    assert re.fullmatch(pattern, str(workspace.resolve() / ".env.example"))
    assert re.fullmatch(pattern, str(workspace.resolve() / "nested/.env.example"))
    assert not re.fullmatch(pattern, str(workspace.resolve() / ".env.production"))
    assert not re.fullmatch(pattern, str(tmp_path / "projectX1x" / ".env.example"))
    assert not re.fullmatch(pattern, str(tmp_path / ".env.example"))


@pytest.mark.skipif(sys.platform != "darwin", reason="Physical Seatbelt requires macOS")
async def test_physical_seatbelt_allows_standard_pytest_logging_without_outside_file_writes(
    tmp_path,
):
    sandbox = Sandbox(SandboxSettings("on"))
    assert (await sandbox.status()).available
    test = tmp_path / "test_probe.py"
    test.write_text("def test_standard_logging():\n    assert 2 + 2 == 4\n")
    argv = await sandbox.launch(
        (sys.executable, "-m", "pytest", "-q", str(test), "--rootdir", str(tmp_path)),
        str(tmp_path),
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    async with asyncio.timeout(15):
        stdout, stderr = await process.communicate()
    assert process.returncode == 0, (stdout + stderr).decode(errors="replace")
    assert b"1 passed" in stdout


@pytest.mark.skipif(sys.platform == "darwin", reason="Unsupported hosts have no OS sandbox")
async def test_unavailable_backend_preserves_ordinary_launch_and_never_claims_auto_approval():
    for mode in ("off", "on", "on-auto"):
        backend = Sandbox(SandboxSettings(mode))
        status = await backend.status()
        assert not status.available and not status.enabled and not status.auto_approve
        assert await backend.launch(("bash", "-c", "echo test"), "/project") == (
            "bash",
            "-c",
            "echo test",
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt requires macOS")
async def test_missing_seatbelt_does_not_execute_unsandboxed_when_explicitly_enabled(tmp_path):
    missing = str(tmp_path / "missing")
    assert not (await Sandbox(SandboxSettings("on-auto"), executable=missing).status()).auto_approve
    with pytest.raises(ToolError) as error:
        await Sandbox(SandboxSettings("on"), executable=missing).launch(
            ("bash", "-c", "touch file"), str(tmp_path)
        )
    assert error.value.code == "sandbox_unavailable"


def test_profile_denies_private_home_state_after_workspace_and_temp_allows(tmp_path):
    home = tmp_path / "home"
    profile = build_profile(tmp_path / "ws", False, home=home)
    lines = profile.splitlines()
    workspace_allow = lines.index(
        f"(allow file-read* (subpath {json.dumps(str((tmp_path / 'ws').resolve()))}))"
    )
    private = next(i for i, line in enumerate(lines) if ".ssh" in line)
    dotenv = next(i for i, line in enumerate(lines) if "\\.env(" in line)
    assert lines[private].startswith("(deny file-read*")
    for name in (".ssh", ".aws", ".gnupg", ".config", ".pycode"):
        assert json.dumps(str(home.resolve() / name)) in lines[private]
    assert private < workspace_allow < dotenv
    assert "(allow file-read-metadata)" in lines


def test_workspace_containing_home_keeps_private_directories_denied(tmp_path):
    profile = build_profile(tmp_path, False, home=tmp_path)
    lines = profile.splitlines()
    workspace_allow = lines.index(
        f"(allow file-read* (subpath {json.dumps(str(tmp_path.resolve()))}))"
    )
    assert any(".ssh" in line and line.startswith("(deny") for line in lines[workspace_allow:])


async def run_sandboxed(workspace, command, home):
    # The fake HOME is passed explicitly; the real user HOME is never touched.
    profile = build_profile(workspace, False, home=home)
    argv = ("/usr/bin/sandbox-exec", "-p", profile, "/bin/bash", "-c", command)
    environment = {**os.environ, "HOME": str(home)}
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(workspace),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    async with asyncio.timeout(15):
        stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


@pytest.fixture
def fake_layout(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    for directory in (".ssh", ".aws", ".gnupg", ".config/git", ".pycode"):
        (home / directory).mkdir(parents=True)
    (home / ".ssh/id_rsa").write_text("fake-private-key-marker")
    (home / ".aws/credentials").write_text("fake-aws-marker")
    (home / ".gnupg/secring.gpg").write_text("fake-gpg-marker")
    (home / ".config/git/credentials").write_text("fake-git-credential-marker")
    (home / ".pycode/providers.json").write_text("fake-provider-marker")
    workspace.mkdir()
    outside.mkdir()
    (workspace / "readme.txt").write_text("workspace-content")
    (workspace / ".env").write_text("fake-dotenv-marker")
    (workspace / ".env.local").write_text("fake-dotenv-local-marker")
    (workspace / ".env.example").write_text("EXAMPLE=1")
    (workspace / "sub").mkdir()
    (workspace / "sub/.env").write_text("fake-nested-dotenv-marker")
    (outside / ".env").write_text("fake-outside-dotenv-marker")
    (outside / ".env.example").write_text("fake-outside-example-marker")
    (home / ".ssh/.env.example").write_text("fake-ssh-example-marker")
    return home, workspace, outside


physical = pytest.mark.skipif(sys.platform != "darwin", reason="Physical Seatbelt requires macOS")


@physical
@pytest.mark.parametrize(
    "relative",
    [
        "home/.ssh/id_rsa",
        "home/.aws/credentials",
        "home/.gnupg/secring.gpg",
        "home/.config/git/credentials",
        "home/.pycode/providers.json",
        "workspace/.env",
        "workspace/.env.local",
        "workspace/sub/.env",
        "outside/.env",
        "outside/.env.example",
        "home/.ssh/.env.example",
    ],
)
async def test_physical_seatbelt_denies_reading_fake_sensitive_files(fake_layout, relative):
    home, workspace, _ = fake_layout
    target = home.parent / relative
    code, stdout, stderr = await run_sandboxed(workspace, "cat " + shlex.quote(str(target)), home)
    assert code != 0 and "marker" not in stdout
    assert "Operation not permitted" in stderr


@physical
async def test_physical_seatbelt_denies_listing_fake_private_directory(fake_layout):
    home, workspace, _ = fake_layout
    code, stdout, _ = await run_sandboxed(workspace, "ls " + shlex.quote(str(home / ".ssh")), home)
    assert code != 0 and "id_rsa" not in stdout


@physical
@pytest.mark.parametrize(
    "command",
    [
        "mv .env.local notes.txt; cat notes.txt",
        "mv sub/.env sub/x; cat sub/x",
        "mv .env y; cat y",
        "ln .env.local z; cat z",
        "cp .env.local w; cat w",
        "mv .env.local .env.example; cat .env.example",
    ],
)
async def test_physical_seatbelt_denies_renaming_dotenv_to_read_it(fake_layout, command):
    home, workspace, _ = fake_layout
    _, stdout, _ = await run_sandboxed(workspace, command, home)
    assert "marker" not in stdout
    assert (workspace / ".env.local").read_text() == "fake-dotenv-local-marker"
    assert (workspace / "sub/.env").read_text() == "fake-nested-dotenv-marker"


@physical
async def test_physical_seatbelt_writes_env_example_only_inside_workspace(fake_layout):
    home, workspace, outside = fake_layout
    code, _, stderr = await run_sandboxed(workspace, "printf NEW=1 > sub/.env.example", home)
    assert code == 0, stderr
    code, _, _ = await run_sandboxed(workspace, "printf NEW=1 > ../outside/.env.example", home)
    assert code != 0
    assert (outside / ".env.example").read_text() == "fake-outside-example-marker"
    assert (workspace / "sub/.env.example").read_text() == "NEW=1"


@physical
async def test_physical_seatbelt_denies_reading_outside_the_allow_list(fake_layout):
    # /Users is not on the allow list; only its listing is requested, never file contents.
    home, workspace, _ = fake_layout
    code, _, stderr = await run_sandboxed(workspace, "ls /Users", home)
    assert code != 0 and "Operation not permitted" in stderr


@physical
async def test_physical_seatbelt_reads_workspace_and_env_example(fake_layout):
    home, workspace, _ = fake_layout
    code, stdout, stderr = await run_sandboxed(workspace, "cat readme.txt .env.example", home)
    assert code == 0, stderr
    assert stdout == "workspace-contentEXAMPLE=1"


@physical
async def test_physical_seatbelt_starts_bash_python_and_git(fake_layout):
    home, workspace, _ = fake_layout
    (home / ".gitconfig").write_text("[user]\n\tname = Sandbox Test\n")
    git = shutil.which("git") or "/usr/bin/git"
    # .git is write-protected inside the sandbox, so the repository is created outside it.
    initialized = await asyncio.create_subprocess_exec(
        git, "init", "-q", str(workspace), env={**os.environ, "HOME": str(home)}
    )
    assert await initialized.wait() == 0
    command = " && ".join(
        (
            "echo bash-ok",
            shlex.quote(sys.executable) + " -c 'import json, pycode; print(\"python-ok\")'",
            shlex.quote(git) + " config user.name",
            shlex.quote(git) + " status --short",
        )
    )
    code, stdout, stderr = await run_sandboxed(workspace, command, home)
    assert code == 0, stderr
    assert "bash-ok" in stdout and "python-ok" in stdout and "Sandbox Test" in stdout
    assert "readme.txt" in stdout


def test_python_read_paths_use_package_root_not_checkout_depth(tmp_path, monkeypatch):
    import pycode.sandbox.sandbox as implementation

    package = tmp_path / "src"
    monkeypatch.setattr(implementation, "__file__", str(package / "sandbox" / "sandbox.py"))
    monkeypatch.setattr(implementation.sys, "path", [])
    paths = implementation.python_read_paths()
    assert str(package) in paths
    assert str(tmp_path) not in paths
    assert str(tmp_path.parent) not in paths
