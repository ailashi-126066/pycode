"""The two architecture rules, checked on small fake source trees."""

import pytest

from scripts.check_architecture import DEPENDENCIES, check_dependencies


def tree(tmp_path, files):
    """Write a fake project; every package root exports `Thing`."""
    for relative, source in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    for package in DEPENDENCIES:
        init = tmp_path / "src" / package / "__init__.py"
        if not init.exists():
            init.parent.mkdir(parents=True, exist_ok=True)
            init.write_text('__all__ = ["Thing"]\nThing = object\n', encoding="utf-8")
    return check_dependencies(tmp_path)


@pytest.mark.parametrize(
    ("relative", "source", "error"),
    [
        (
            "kernel/messages.py",
            "from pycode.models import Thing\n",
            "kernel cannot depend on models",
        ),
        (
            "kernel/messages.py",
            "import os\n",
            "only kernel/io.py may import I/O module os",
        ),
        (
            "kernel/io.py",
            "from langchain_core.messages import AIMessage\n",
            "kernel cannot import framework langchain_core.messages",
        ),
        ("context/types.py", "from pycode.tools import Thing\n", "context cannot depend on tools"),
        ("memory/memory.py", "from pycode.skills import Thing\n", "memory cannot depend on skills"),
        ("ui/tui.py", "from pycode.app import Thing\n", "ui cannot depend on app"),
        ("agent/runtime.py", "import evals.runner\n", "pycode cannot import evals.runner"),
        (
            "agent/runtime.py",
            "from pycode.tools.adapters.backend import Thing\n",
            "import from pycode.tools, not pycode.tools.adapters.backend",
        ),
        ("agent/runtime.py", "from pycode.tools import Hidden\n", "Hidden is not in pycode.tools"),
    ],
)
def test_violations_are_reported(tmp_path, relative, source, error):
    errors = tree(tmp_path, {"src/" + relative: source})
    assert len(errors) == 1 and error in errors[0], errors


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("kernel/io.py", "import os\nfrom pathlib import Path\n"),
        (
            "kernel/runtime.py",
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pathlib import Path\n"
            "    from pycode.session import Thing\n",
        ),
        ("tools/executor.py", "from pycode.tools.adapters.backend import PycodeBackend\n"),
        ("tools/executor.py", "from .adapters.backend import PycodeBackend\n"),
        ("session/store.py", "from pycode.kernel import Thing\nfrom pycode.agent import Thing\n"),
        ("mcp/mcp.py", "from pycode.tools import Thing\nfrom pycode.models import Thing\n"),
        ("ui/tui.py", "from pycode.memory import Thing\nfrom pycode.session import Thing\n"),
        ("app/composition.py", "from pycode.ui import Thing\nfrom pycode.sandbox import Thing\n"),
        ("app/cli.py", "from pycode import __version__\n"),
    ],
)
def test_allowed_imports(tmp_path, relative, source):
    assert tree(tmp_path, {"src/" + relative: source}) == []


def test_type_checking_imports_still_need_the_package_root(tmp_path):
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from pycode.session.store import SqliteSessionStore\n"
    )
    errors = tree(tmp_path, {"src/kernel/runtime.py": source})
    assert len(errors) == 1 and "import from pycode.session" in errors[0], errors


@pytest.mark.parametrize("relative", ["scripts/probe.py", "evals/runner.py"])
def test_scripts_and_evals_use_package_roots(tmp_path, relative):
    errors = tree(
        tmp_path,
        {
            "src/kernel/messages.py": "",
            relative: "from pycode.kernel import Thing\n"
            "from pycode.kernel.messages import Message\n",
        },
    )
    assert len(errors) == 1 and "import from pycode.kernel" in errors[0], errors


def test_real_project_obeys_dependency_direction_and_public_imports():
    assert check_dependencies() == []
