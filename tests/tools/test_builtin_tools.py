"""Pi's builtin tools over the CompositeBackend mounts: limits, notices, paths and errors."""

import pytest
from deepagents.backends import CompositeBackend, StateBackend

from pycode.kernel import MAX_LINES, ToolContext, ToolError
from pycode.sandbox import Bash, Sandbox, SandboxSettings, find_bash
from pycode.skills.skills import SkillBackend
from pycode.tools.backend import PycodeBackend
from pycode.tools.builtin.edit import Edit, prepare_edit_arguments
from pycode.tools.builtin.find import Find
from pycode.tools.builtin.grep import Grep
from pycode.tools.builtin.ls import Ls
from pycode.tools.builtin.read import Read
from pycode.tools.builtin.write import Write
from pycode.tools.output import TempOutputStore


def mounts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nUse the demo.\n")
    prefix = "/skills/user/"
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/workspace/": PycodeBackend(workspace),
            prefix: SkillBackend(tmp_path / "skills", prefix=prefix),
        },
    )
    return workspace, backend


def context(name, workspace="/workspace"):
    return ToolContext("s", "r", "op", str(workspace), "c", name)


async def test_read_keeps_the_head_and_points_at_the_next_offset(tmp_path):
    workspace, backend = mounts(tmp_path)
    (workspace / "long.txt").write_text("\n".join(f"line {n}" for n in range(1, 3001)))
    result = await Read(backend).execute(context("read"), {"path": "long.txt"})
    body, _, notice = result.text.rpartition("\n\n")
    assert body.splitlines()[0] == "line 1" and len(body.splitlines()) == MAX_LINES
    assert notice == "[Showing lines 1-2000 of 3000. Use offset=2001 to continue.]"
    assert result.details["truncation"]["truncated_by"] == "lines"
    rest = await Read(backend).execute(context("read"), {"path": "long.txt", "offset": 2999})
    assert rest.text == "line 2999\nline 3000"


async def test_read_byte_limit_long_line_and_user_limit_notices(tmp_path):
    workspace, backend = mounts(tmp_path)
    (workspace / "wide.txt").write_text("\n".join("x" * 1000 for _ in range(100)))
    wide = await Read(backend).execute(context("read"), {"path": "wide.txt"})
    assert wide.text.endswith(
        "[Showing lines 1-51 of 100 (50.0KB limit). Use offset=52 to continue.]"
    )
    (workspace / "giant.txt").write_text("y" * 60000)
    giant = await Read(backend).execute(context("read"), {"path": "giant.txt"})
    assert giant.text == (
        "[Line 1 is 58.6KB, exceeds 50.0KB limit. Use bash: sed -n '1p' giant.txt | head -c 51200]"
    )
    (workspace / "short.txt").write_text("a\nb\nc\nd\ne")
    short = await Read(backend).execute(context("read"), {"path": "short.txt", "limit": 2})
    assert short.text == "a\nb\n\n[3 more lines in file. Use offset=3 to continue.]"


async def test_relative_paths_resolve_to_the_workspace_and_others_must_be_mounted(tmp_path):
    workspace, backend = mounts(tmp_path)
    (workspace / "notes.txt").write_text("hello")
    relative = await Read(backend).execute(context("read"), {"path": "notes.txt"})
    absolute = await Read(backend).execute(context("read"), {"path": "/workspace/notes.txt"})
    assert relative.text == absolute.text == "hello"
    for path in ("/etc/passwd", "../outside.txt"):
        with pytest.raises(ToolError) as error:
            await Read(backend).execute(context("read"), {"path": path})
        assert error.value.code == "outside_workspace"


async def test_read_loads_a_skill_through_its_mount(tmp_path):
    _, backend = mounts(tmp_path)
    result = await Read(backend).execute(context("read"), {"path": "/skills/user/demo/SKILL.md"})
    assert "Use the demo." in result.text and "Skill source directory:" in result.text


async def test_edit_requires_one_exact_match_and_keeps_line_endings(tmp_path):
    workspace, backend = mounts(tmp_path)
    (workspace / "code.py").write_bytes(b"a = 1\r\nb = 1\r\n")
    for old, code in (("missing", "edit_not_found"), ("= 1", "edit_not_unique")):
        with pytest.raises(ToolError) as error:
            await Edit(backend).execute(
                context("edit"), {"path": "code.py", "edits": [{"oldText": old, "newText": "x"}]}
            )
        assert error.value.code == code
    result = await Edit(backend).execute(
        context("edit"),
        {
            "path": "code.py",
            "edits": [
                {"oldText": "a = 1", "newText": "a = 2"},
                {"oldText": "b = 1", "newText": "b"},
            ],
        },
    )
    assert result.text == "Successfully replaced 2 block(s) in code.py."
    assert (workspace / "code.py").read_bytes() == b"a = 2\r\nb\r\n"
    assert result.details["first_changed_line"] == 1 and "+a = 2" in result.details["patch"]


def test_edit_prepare_arguments_accepts_pi_legacy_shapes():
    legacy = prepare_edit_arguments({"path": "f", "oldText": "a", "newText": "b"})
    assert legacy == {"path": "f", "edits": [{"oldText": "a", "newText": "b"}]}
    text = prepare_edit_arguments({"path": "f", "edits": '[{"oldText": "a", "newText": "b"}]'})
    assert text["edits"] == [{"oldText": "a", "newText": "b"}]


async def test_write_creates_parent_directories_and_rejects_read_only_mounts(tmp_path):
    workspace, backend = mounts(tmp_path)
    result = await Write(backend).execute(
        context("write"), {"path": "new/dir/a.txt", "content": "x"}
    )
    assert result.text == "Successfully wrote to new/dir/a.txt"
    assert (workspace / "new/dir/a.txt").read_text() == "x"
    with pytest.raises(ToolError) as error:
        await Write(backend).execute(
            context("write"), {"path": "/skills/user/demo/SKILL.md", "content": "x"}
        )
    assert error.value.code == "read_only_path"


@pytest.mark.requires_rg
async def test_grep_limits_matches_and_line_length(tmp_path):
    workspace, backend = mounts(tmp_path)
    (workspace / "many.txt").write_text("".join(f"needle {n}\n" for n in range(150)))
    many = await Grep(backend).execute(context("grep"), {"pattern": "needle"})
    lines, _, notice = many.text.rpartition("\n\n")
    assert len(lines.splitlines()) == 100 and lines.splitlines()[0] == "many.txt:1: needle 0"
    assert notice == "[100 matches limit reached. Use limit=200 for more, or refine pattern]"
    (workspace / "wide.txt").write_text("wide " + "z" * 600)
    wide = await Grep(backend).execute(context("grep"), {"pattern": "wide", "glob": "wide.txt"})
    assert wide.text.startswith("wide.txt:1: wide " + "z" * 495 + "... [truncated]")
    assert "Some lines truncated to 500 chars" in wide.text
    literal = await Grep(backend).execute(
        context("grep"), {"pattern": "NEEDLE 1(", "literal": True, "ignoreCase": True}
    )
    assert literal.text == "No matches found"


@pytest.mark.requires_rg
async def test_find_and_ls_limit_results(tmp_path):
    workspace, backend = mounts(tmp_path)
    (workspace / "files").mkdir()
    for n in range(1001):
        (workspace / "files" / f"f{n:04}.txt").write_text("")
    found = await Find(backend).execute(context("find"), {"pattern": "*.txt", "path": "files"})
    paths, _, notice = found.text.rpartition("\n\n")
    assert len(paths.splitlines()) == 1000 and paths.splitlines()[0] == "f0000.txt"
    assert notice == "[1000 results limit reached. Use limit=2000 for more, or refine pattern]"
    listed = await Ls(backend).execute(context("ls"), {"path": "files"})
    entries, _, notice = listed.text.rpartition("\n\n")
    assert len(entries.splitlines()) == 500
    assert notice == "[500 entries limit reached. Use limit=1000 for more]"
    root = await Ls(backend).execute(context("ls"), {})
    assert root.text == "files/"


async def test_bash_keeps_the_tail_and_marks_nonzero_exit_as_error(tmp_path):
    outputs = TempOutputStore("s")
    bash = Bash(find_bash(), outputs, Sandbox(SandboxSettings()))
    result = await bash.execute(
        context("bash", tmp_path), {"command": "for i in $(seq 1 3000); do echo line $i; done"}
    )
    path = result.details["full_output_path"]
    assert not result.is_error and path.endswith("/pycode/s/c.log")
    assert "line 3000" in result.text and "line 1\n" not in result.text
    assert result.text.endswith(f"[Showing lines 1001-3000 of 3000. Full output: {path}]")
    # The model reads the full output back with read through the temp-directory mount.
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            str(outputs.directory) + "/": PycodeBackend(
                outputs.directory, read_only=True, empty_missing_root=True
            )
        },
    )
    page = await Read(backend).execute(context("read"), {"path": path, "offset": 1, "limit": 2})
    assert page.text.startswith("line 1\nline 2")
    failed = await bash.execute(
        ToolContext("s", "r", "op2", str(tmp_path), "c2", "bash"), {"command": "exit 3"}
    )
    assert failed.is_error and failed.exit_code == 3


async def test_any_read_only_mount_is_readable_but_never_writable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    notes = tmp_path / "memory"
    notes.mkdir()
    (notes / "style.md").write_text("---\ndescription: d\n---\nPrefer tabs.")
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/workspace/": PycodeBackend(workspace),
            "/memory/user/": PycodeBackend(notes, read_only=True, empty_missing_root=True),
        },
    )
    read = await Read(backend).execute(context("read"), {"path": "/memory/user/style.md"})
    assert read.text.endswith("Prefer tabs.")
    listed = await Ls(backend).execute(context("ls"), {"path": "/memory/user/"})
    assert listed.text == "style.md"
    with pytest.raises(ToolError) as error:
        await Write(backend).execute(
            context("write"), {"path": "/memory/user/style.md", "content": "x"}
        )
    assert error.value.code == "read_only_path"
