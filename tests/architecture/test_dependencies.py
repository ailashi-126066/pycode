"""Architecture enforcement covers real imports, aliases, public origins and cycles."""

import pytest
from scripts.check_architecture import (
    APP_PACKAGES,
    ROOT,
    app_layout_errors,
    check_dependencies,
    dependency_errors,
)


def test_project_dependency_direction():
    assert check_dependencies() == []


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("pycode.tools.domain.policy", "from pycode.tools.services.executor import Executor"),
        ("pycode.memory.ports.store", "from pycode.memory.services.memory import MemoryService"),
        ("pycode.memory.services.memory", "from pycode.memory.adapters.store import Store"),
        ("pycode.models.services.control", "from pycode.context import ContextProjector"),
        ("pycode.hooks.services.engine", "from pycode.tools import ToolExecutor"),
        ("pycode.session.services.agent", "from pycode.models.domain.models import ProviderConfig"),
        ("pycode.agent.adapters.agent", "from pycode.tools.adapters.settlement import Middleware"),
        ("pycode.app.cli.main", "from pycode.models.adapters.factory import ModelFactory"),
        (
            "pycode.app.composition.runtime",
            "from pycode.models.adapters.factory import ModelFactory",
        ),
        ("pycode.app.cli.main", "from pycode.skills.module import SkillsModule"),
        ("pycode.skills.catalog", "from pycode.skills.module import SkillsModule"),
        ("pycode.mcp.module", "from pycode.skills.module import SkillsModule"),
        ("evals.coding.runner", "from pycode.app.composition import build_runtime"),
        ("pycode.agent.ports.runtime", "from langchain_core.messages import AIMessage"),
        ("pycode.context.services.summary", "import langgraph"),
        ("pycode.memory.domain.policy", "import pathlib as filesystem"),
        ("pycode.memory.domain.policy", "from builtins import open as reader\nreader('x')"),
        ("pycode.memory.domain.policy", "import builtins as b\nb.open('x')"),
        ("pycode.memory.domain.policy", "reader = open\nreader('x')"),
        ("pycode.tools.services.executor", "def f():\n    from pycode.kernel import ToolError"),
        ("pycode.tools.services.executor", "from ...models.adapters.factory import ModelFactory"),
        ("pycode.core.state", "from dataclasses import dataclass"),
        ("pycode.agent.adapters.agent", "from pycode.workflows.state import PycodeState"),
        ("pycode.app.composition.runtime", "from evals.coding import runner"),
        ("pycode", "import scripts.check_architecture"),
    ],
)
def test_prohibited_dependencies_are_detected(module, source):
    assert dependency_errors(source, module)


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("pycode.kernel.domain.messages", "from dataclasses import dataclass"),
        ("pycode.memory.services.memory", "from pycode.context import retained_context_entries"),
        ("pycode.tools.ports.outputs", "from pycode.kernel import ToolResult"),
        ("pycode.tools.adapters.settlement", "from ..domain.execution import batch_execution_mode"),
        ("pycode.agent.adapters.agent", "from langchain.agents import create_agent"),
        ("pycode.app.composition.runtime", "from pycode.models.module import ModelsModule"),
        ("evals.coding.runner", "from pycode.app.bootstrap import compose_application"),
        ("evals.scripts.compare_eval_runs", "from evals.coding import provenance"),
        ("scripts.verify_release", "from pycode.app.bootstrap import compose_application"),
    ],
)
def test_allowed_dependencies(module, source):
    assert dependency_errors(source, module) == []


def package_file(root, relative, source):
    path = root / "src" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_public_alias_cannot_reexport_an_adapter(tmp_path):
    package_file(
        tmp_path,
        "pycode/tools/__init__.py",
        "from .adapters.impl import Concrete as Public\n__all__ = ['Public']\n",
    )
    package_file(tmp_path, "pycode/tools/adapters/impl.py", "class Concrete: pass\n")
    package_file(tmp_path, "pycode/session/services/use.py", "from pycode.tools import Public\n")
    errors = check_dependencies(tmp_path)
    assert any("cannot re-export adapter Public" in item for item in errors)
    assert any("cross-feature adapters require composition" in item for item in errors)


def test_unexported_symbol_is_rejected(tmp_path):
    package_file(tmp_path, "pycode/snapshot/__init__.py", "class Hidden: pass\n__all__ = []\n")
    package_file(tmp_path, "pycode/tools/services/use.py", "from pycode.snapshot import Hidden\n")
    assert any("Hidden is not exported" in item for item in check_dependencies(tmp_path))


def test_domain_cannot_hide_io_behind_kernel_api(tmp_path):
    package_file(
        tmp_path,
        "pycode/kernel/__init__.py",
        "from .adapters.files import read\n__all__ = ['read']\n",
    )
    package_file(tmp_path, "pycode/kernel/adapters/files.py", "def read(): return ''\n")
    package_file(tmp_path, "pycode/memory/domain/value.py", "from pycode.kernel import read\n")
    assert any("domain cannot depend on adapters" in item for item in check_dependencies(tmp_path))


def test_relative_package_module_alias_checks_internal_layer(tmp_path):
    package_file(tmp_path, "pycode/memory/domain/__init__.py", "from .. import adapters as io\n")
    package_file(tmp_path, "pycode/memory/adapters/__init__.py", "")
    assert any("domain cannot depend on adapters" in item for item in check_dependencies(tmp_path))


def test_type_checking_imports_still_form_module_cycles(tmp_path):
    package_file(
        tmp_path,
        "pycode/memory/services/a.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from .b import B\nclass A: pass\n",
    )
    package_file(tmp_path, "pycode/memory/services/b.py", "from .a import A\nclass B: pass\n")
    assert any(
        "module dependency cycle:" in item and "services.a" in item and "services.b" in item
        for item in check_dependencies(tmp_path)
    )


def test_feature_cycle_is_reported_with_full_path(tmp_path):
    package_file(
        tmp_path,
        "pycode/memory/__init__.py",
        "from .services.use import Memory\n__all__ = ['Memory']\n",
    )
    package_file(
        tmp_path,
        "pycode/context/__init__.py",
        "from .services.use import Context\n__all__ = ['Context']\n",
    )
    package_file(
        tmp_path,
        "pycode/memory/services/use.py",
        "from pycode.context import Context\nclass Memory: pass\n",
    )
    package_file(
        tmp_path,
        "pycode/context/services/use.py",
        "from pycode.memory import Memory\nclass Context: pass\n",
    )
    assert any(
        "feature dependency cycle:" in item and "memory" in item and "context" in item
        for item in check_dependencies(tmp_path)
    )


def test_project_scan_covers_evals_and_skips_generated_data(tmp_path):
    package_file(tmp_path, "pycode/__main__.py", "import evals\n")
    evals = tmp_path / "evals"
    (evals / "coding").mkdir(parents=True)
    (evals / "coding/runner.py").write_text(
        "from pycode.app.composition import runtime\n", encoding="utf-8"
    )
    (evals / "cache").mkdir()
    (evals / "cache/solution.py").write_text(
        "from pycode.app.composition import runtime\n", encoding="utf-8"
    )
    assert check_dependencies(tmp_path) == [
        "pycode.__main__:1: pycode cannot import evals",
        "evals.coding.runner:1: application assembly requires app.bootstrap or configuration",
    ]


@pytest.mark.parametrize(
    "wrapper",
    [
        "Public = Concrete",
        "def Public(): return Concrete()",
        "class Public(Concrete): pass",
    ],
)
def test_public_assignments_factories_and_subclasses_cannot_hide_adapters(tmp_path, wrapper):
    package_file(
        tmp_path,
        "pycode/tools/__init__.py",
        "from .adapters.impl import Concrete\n" + wrapper + "\n__all__ = ['Public']\n",
    )
    package_file(tmp_path, "pycode/tools/adapters/impl.py", "class Concrete: pass\n")
    assert any("cannot re-export adapter Public" in item for item in check_dependencies(tmp_path))


def test_public_module_attribute_alias_cannot_hide_adapter(tmp_path):
    package_file(
        tmp_path,
        "pycode/tools/__init__.py",
        "from .adapters import impl\nPublic = impl.Concrete\n__all__ = ['Public']\n",
    )
    package_file(tmp_path, "pycode/tools/adapters/impl.py", "class Concrete: pass\n")
    assert any("cannot re-export adapter Public" in item for item in check_dependencies(tmp_path))


def test_undefined_public_exports_are_rejected(tmp_path):
    package_file(tmp_path, "pycode/tools/__init__.py", "__all__ = ['Ghost']\n")
    assert any("undefined symbol Ghost" in item for item in check_dependencies(tmp_path))


def test_singular_service_file_obeys_framework_boundary():
    assert dependency_errors("import langchain_core", "pycode.memory.service")


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.import_module('pycode.tools')",
        "from importlib import import_module as load\nload('pycode.tools')",
        "__import__('pycode.tools')",
        "import sys\nsys.modules['pycode.tools'] = object()",
        "def __getattr__(name): return object()",
    ],
)
def test_dynamic_dependency_masking_is_rejected(source):
    assert dependency_errors(source, "pycode.memory.adapters.bad")


def test_verified_user_resource_registration_is_not_a_feature_alias():
    assert (
        dependency_errors(
            "import sys\nsys.modules[module.__name__] = module",
            "pycode.skills.adapters.python_worker",
        )
        == []
    )


@pytest.mark.parametrize("definition", ["", "Ghost = Other\nOther = Ghost\n"])
def test_undefined_or_cyclic_export_import_reports_instead_of_crashing(tmp_path, definition):
    package_file(tmp_path, "pycode/tools/__init__.py", definition + "__all__ = ['Ghost']\n")
    package_file(tmp_path, "pycode/session/services/use.py", "from pycode.tools import Ghost\n")
    errors = check_dependencies(tmp_path)
    assert any("undefined symbol Ghost" in item for item in errors)
    assert any("Ghost has no definition" in item for item in errors)


@pytest.mark.parametrize(
    "module",
    ["pycode.memory.adapters.store", "pycode.memory.module", "pycode.app.composition.runtime"],
)
def test_shared_kernel_infrastructure_is_explicit_at_implementation_boundaries(module):
    assert (
        dependency_errors("from pycode.kernel.adapters.path_io import atomic_write", module) == []
    )


@pytest.mark.parametrize(
    "module",
    ["pycode.memory.domain.value", "pycode.memory.ports.store", "pycode.memory.services.memory"],
)
def test_shared_kernel_infrastructure_does_not_bypass_pure_layers(module):
    assert dependency_errors("from pycode.kernel.adapters.path_io import atomic_write", module)


def test_kernel_infrastructure_exception_does_not_apply_to_other_feature_adapters():
    assert dependency_errors(
        "from pycode.models.adapters.factory import ModelFactory", "pycode.memory.module"
    )


def test_kernel_root_cannot_hide_shared_infrastructure(tmp_path):
    package_file(
        tmp_path,
        "pycode/kernel/__init__.py",
        "from .adapters.path_io import atomic_write\n__all__ = ['atomic_write']\n",
    )
    package_file(tmp_path, "pycode/kernel/adapters/path_io.py", "def atomic_write(): pass\n")
    errors = check_dependencies(tmp_path)
    assert any("cannot re-export adapter atomic_write" in error for error in errors)


def test_kernel_requires_one_consistent_layer_layout(tmp_path):
    package_file(tmp_path, "pycode/kernel/mixed.py", "VALUE = 1\n")
    assert any("kernel source must use" in item for item in check_dependencies(tmp_path))


def test_feature_assembly_is_not_a_backdoor_public_api(tmp_path):
    package_file(tmp_path, "pycode/skills/module.py", "class SkillsModule: pass\n")
    package_file(
        tmp_path,
        "pycode/skills/__init__.py",
        "from .module import SkillsModule\n__all__ = ['SkillsModule']\n",
    )
    package_file(tmp_path, "pycode/app/cli/main.py", "from pycode.skills import SkillsModule\n")
    errors = check_dependencies(tmp_path)
    assert any("feature assembly requires" in item for item in errors)
    assert any("public API cannot re-export" in item for item in errors)


def test_only_explicit_composition_can_construct_a_feature(tmp_path):
    package_file(
        tmp_path,
        "pycode/skills/module.py",
        "from .adapters.catalog import Catalog\n"
        "class SkillsModule:\n    def create(self): return Catalog()\n",
    )
    package_file(tmp_path, "pycode/skills/adapters/catalog.py", "class Catalog: pass\n")
    package_file(
        tmp_path, "pycode/app/bootstrap.py", "from pycode.skills.module import SkillsModule\n"
    )
    assert check_dependencies(tmp_path) == []


@pytest.mark.parametrize("layer", ["ports", "services"])
@pytest.mark.parametrize(
    "source", ["import pathlib", "from builtins import open as read\nread('x')"]
)
def test_shared_native_layers_cannot_perform_direct_io(layer, source):
    assert dependency_errors(source, f"pycode.kernel.{layer}.invalid")


def test_project_scan_enforces_script_bootstrap_entry(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "legacy.py").write_text("from pycode.app.composition import build_runtime\n")
    assert any(
        "application assembly requires app.bootstrap" in error
        for error in check_dependencies(tmp_path)
    )


def test_module_entry_does_not_forward_an_imported_adapter(tmp_path):
    package_file(tmp_path, "pycode/skills/adapters/catalog.py", "class Catalog: pass\n")
    package_file(tmp_path, "pycode/skills/module.py", "from .adapters.catalog import Catalog\n")
    package_file(tmp_path, "pycode/app/bootstrap.py", "from pycode.skills.module import Catalog\n")
    assert any(
        "module entry cannot forward imported implementations" in item
        for item in check_dependencies(tmp_path)
    )


@pytest.mark.parametrize(
    ("module", "target"),
    [
        ("configuration.settings", "bootstrap"),
        ("configuration.providers", "composition.modules"),
        ("configuration.credentials", "lifecycle.session"),
        ("configuration.settings", "cli.parser"),
        ("lifecycle.session", "composition.run"),
        ("lifecycle.run", "bootstrap"),
        ("lifecycle.session", "cli.commands"),
        ("composition.session", "cli.main"),
        ("composition.modules", "bootstrap"),
        ("cli.commands", "composition.session"),
        ("cli.main", "lifecycle.run"),
        ("bootstrap", "cli.main"),
    ],
)
def test_app_scopes_reject_reversed_dependencies(module, target):
    errors = dependency_errors(f"import pycode.app.{target}", "pycode.app." + module)
    assert any("cannot depend on app" in error for error in errors)


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("cli.main", "from pycode.app.bootstrap import compose_application"),
        ("cli.commands", "from pycode.app.configuration.settings import AppConfig"),
        ("configuration.providers", "from pycode.models import ProviderConfig"),
        ("lifecycle.run", "from pycode.agent import Runtime"),
        ("lifecycle.session", "from pycode.app.configuration.settings import AppConfig"),
        ("composition.run", "from pycode.app.lifecycle.run import RuntimeContainer"),
        ("bootstrap", "from pycode.app.composition.modules import default_modules"),
    ],
)
def test_app_scope_dependencies_point_towards_configuration_and_feature_ports(module, source):
    assert dependency_errors(source, "pycode.app." + module) == []


@pytest.mark.parametrize("consumer", ["evals.coding.assembly", "scripts.verify_live_provider"])
@pytest.mark.parametrize("scope", ["cli.main", "composition.run", "lifecycle.session"])
def test_external_callers_use_the_application_facade(consumer, scope):
    assert dependency_errors(f"from pycode.app.{scope} import Factory", consumer)


@pytest.mark.parametrize("consumer", ["evals.coding.assembly", "scripts.verify_live_provider"])
@pytest.mark.parametrize("scope", ["bootstrap", "configuration.providers"])
def test_external_callers_may_use_bootstrap_and_configuration(consumer, scope):
    assert dependency_errors(f"from pycode.app.{scope} import Public", consumer) == []


@pytest.mark.parametrize(
    "module",
    ["bootstrap", "cli.main", "configuration.settings", "lifecycle.run", "composition.run"],
)
@pytest.mark.parametrize(
    "source",
    [
        "import langgraph",
        "from textual.app import App",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from langchain_core import Runnable",
    ],
)
def test_app_does_not_own_framework_adapters(module, source):
    assert any(
        "app cannot import framework" in error
        for error in dependency_errors(source, "pycode.app." + module)
    )


def test_relative_configuration_import_cannot_reach_bootstrap(tmp_path):
    package_file(tmp_path, "pycode/app/bootstrap.py", "class Application: pass\n")
    package_file(
        tmp_path, "pycode/app/configuration/settings.py", "from ..bootstrap import Application\n"
    )
    assert any(
        "configuration cannot depend on app bootstrap" in e for e in check_dependencies(tmp_path)
    )


def test_bootstrap_cannot_forward_private_lifecycle_to_external_scripts(tmp_path):
    package_file(tmp_path, "pycode/app/lifecycle/run.py", "class PrivateRuntime: pass\n")
    package_file(
        tmp_path, "pycode/app/bootstrap.py", "from .lifecycle.run import PrivateRuntime as Public\n"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "consumer.py").write_text("from pycode.app.bootstrap import Public\n")
    assert any(
        "application assembly requires app.bootstrap" in e for e in check_dependencies(tmp_path)
    )


@pytest.mark.parametrize(
    "relative", ["cli.py", "config.py", "runtime_launcher.py", "team_runtime.py"]
)
def test_old_flat_app_modules_are_not_compatible_entrypoints(tmp_path, relative):
    package_file(tmp_path, "pycode/app/" + relative, '"""Old module."""\n')
    assert app_layout_errors(tmp_path / "src")


def test_app_package_initializers_cannot_hide_construction_exports(tmp_path):
    package_file(tmp_path, "pycode/app/__init__.py", '"""App."""\n')
    package_file(
        tmp_path,
        "pycode/app/cli/__init__.py",
        "from .main import main\n__all__ = ['main']\n",
    )
    assert any(
        "initializers must contain only a docstring" in e for e in check_dependencies(tmp_path)
    )


def test_actual_app_layout_contains_only_named_scope_packages():
    app = ROOT / "src" / "pycode" / "app"
    assert app_layout_errors(ROOT / "src") == []
    assert {
        path.name for path in app.iterdir() if path.is_file() and not path.name.startswith(".")
    } == {"__init__.py", "api.py", "bootstrap.py", "README.md"}
    assert {
        path.name
        for path in app.iterdir()
        if path.is_dir() and not path.name.startswith((".", "__"))
    } == APP_PACKAGES
    assert all((app / name / "__init__.py").is_file() for name in APP_PACKAGES)


def test_scripts_can_use_public_assembly_data_but_not_private_factories(tmp_path):
    package_file(
        tmp_path,
        "pycode/app/api.py",
        "from .composition.inputs import RunOptions, SessionResources\n",
    )
    package_file(
        tmp_path,
        "pycode/app/composition/inputs.py",
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\nclass RunOptions:\n    run_id: str\n"
        "@dataclass(frozen=True)\nclass SessionResources:\n    session_id: str\n",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    caller = scripts / "verify.py"
    caller.write_text("from pycode.app.api import RunOptions, SessionResources\n")
    assert check_dependencies(tmp_path) == []
    caller.write_text("from pycode.app.composition.inputs import RunOptions\n")
    assert any("application assembly requires" in error for error in check_dependencies(tmp_path))


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
