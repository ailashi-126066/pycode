"""Static enforcement of feature APIs, dependency DAG, layer purity and product branding."""

import ast
import json
import os
import re
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEATURE_DEPENDENCIES = {
    "kernel": set(),
    "sandbox": {"kernel"},
    "snapshot": {"kernel"},
    "models": {"kernel"},
    "mcp": {"kernel", "context"},
    "skills": {"kernel", "sandbox", "context"},
    "plans": {"kernel", "sandbox", "context"},
    "hooks": {"kernel", "sandbox"},
    "context": {"kernel", "models"},
    "memory": {"kernel", "context", "models"},
    "tools": {"kernel", "sandbox", "snapshot", "plans"},
    "agent": {"kernel", "models", "context", "tools", "plans", "hooks"},
    "subagents": {"kernel"},
    "session": {
        "kernel",
        "context",
        "models",
        "skills",
        "snapshot",
        "tools",
        "memory",
        "subagents",
    },
    "ui": {"kernel", "tools", "plans", "mcp"},
}
FEATURE_DEPENDENCIES["app"] = set(FEATURE_DEPENDENCIES)
LEGACY = {"core", "ports", "services", "workflows", "adapters"}
CORE_IO = {
    "os",
    "pathlib",
    "subprocess",
    "socket",
    "urllib",
    "httpx",
    "httpx2",
    "requests",
    "sqlite3",
    "shutil",
    "tempfile",
    "aiofiles",
}
FRAMEWORKS = {
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langchain_deepseek",
    "langchain_anthropic",
    "langgraph",
    "langmem",
    "trustcall",
    "textual",
    "deepagents",
    "fastmcp",
}
FORBIDDEN_IMPORTS = {
    "pycode": frozenset({"evals", "scripts"}),
}
EVAL_GENERATED = {"cache", "work", "results"}
RANK = {"domain": 0, "ports": 1, "services": 2, "adapters": 3, "io": 3, "module": 4}
FLAT_FEATURES = frozenset({"skills", "mcp", "hooks", "subagents"})
FLAT_IO_MODULES = frozenset(
    {
        "catalog",
        "install",
        "framework",
        "connection",
        "config",
        "actions",
        "conditions",
        "worktrees",
    }
)
APP_PACKAGES = frozenset({"cli", "configuration", "composition", "lifecycle"})
APP_DEPENDENCIES = {
    "bootstrap": {"bootstrap", "api", "configuration", "composition", "lifecycle"},
    "api": {"api", "configuration", "composition", "lifecycle"},
    "cli": {"cli", "bootstrap", "api", "configuration"},
    "configuration": {"configuration"},
    "composition": {"composition", "configuration", "lifecycle"},
    "lifecycle": {"lifecycle", "configuration"},
}


def app_scope(module: str | None) -> str | None:
    if module is None or not module.startswith("pycode.app."):
        return None
    return module.split(".")[2]


def app_layout_errors(source_root: Path) -> list[str]:
    errors = []
    app_root = source_root / "pycode" / "app"
    if app_root.exists():
        for path in sorted(app_root.iterdir()):
            if path.name.startswith(".") or path.name == "__pycache__":
                continue
            if path.is_dir() and path.name not in APP_PACKAGES:
                errors.append(f"app root has an unsupported package: {path.name}")
            elif path.is_file() and path.name not in {
                "__init__.py",
                "api.py",
                "bootstrap.py",
                "README.md",
            }:
                errors.append(
                    f"app root only permits bootstrap, package documentation and init: {path.name}"
                )
    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(app_root)
        if len(relative.parts) == 1:
            if relative.name not in {"__init__.py", "api.py", "bootstrap.py"}:
                errors.append(f"app root only permits __init__.py and bootstrap.py: {relative}")
        elif relative.parts[0] not in APP_PACKAGES:
            errors.append(
                f"app source must use cli/configuration/lifecycle/composition: {relative}"
            )
        if path.name == "__init__.py":
            body = ast.parse(path.read_text(encoding="utf-8")).body
            if body and not (
                len(body) == 1
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                errors.append(f"app package initializers must contain only a docstring: {relative}")
    return errors


def app_import_errors(module: str, target: str, line: int) -> list[str]:
    scope, dependency = app_scope(module), app_scope(target)
    if module.split(".")[0] in {"evals", "scripts"} and (
        target == "pycode.app" or dependency is not None
    ):
        if dependency not in {"bootstrap", "api", "configuration"}:
            return [
                f"{module}:{line}: application assembly requires app.bootstrap or configuration"
            ]
    if scope in APP_DEPENDENCIES and dependency is not None:
        if dependency not in APP_DEPENDENCIES[scope]:
            return [f"{module}:{line}: app {scope} cannot depend on app {dependency}"]
    return []


def module_name(relative: Path) -> tuple[str, bool]:
    parts = list(relative.with_suffix("").parts)
    package = parts[-1] == "__init__"
    if package:
        parts.pop()
    return ".".join(parts), package


def feature_of(module: str | None) -> str | None:
    if module is None:
        return None
    parts = module.split(".")
    if parts[0] != "pycode" or len(parts) < 2:
        return None
    return "app" if parts[1] == "__main__" else parts[1]


def layer_of(module: str | None) -> str | None:
    if module is None:
        return None
    parts = module.split(".")
    if len(parts) < 3 or parts[1] == "app":
        return None
    if parts[1] in FLAT_FEATURES and parts[2] in {"types", "contracts"}:
        return "domain" if parts[2] == "types" else "ports"
    if parts[2] == "service":
        return "services"
    if parts[2] in RANK:
        return parts[2]
    return "domain" if parts[1] == "kernel" else None


def import_base(node: ast.ImportFrom, module: str, package: bool) -> str:
    if not node.level:
        return node.module or ""
    parts = module.split(".") if package else module.split(".")[:-1]
    return ".".join(parts[: len(parts) - node.level + 1] + ([node.module] if node.module else []))


def import_refs(source: str, module: str, package=False, modules=None):
    """Yield (node, target module, symbol or None, local binding) including TYPE_CHECKING."""
    known = modules or {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name, None, alias.asname or alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = import_base(node, module, package)
            for alias in node.names:
                submodule = base + "." + alias.name
                if submodule in known or (
                    base == "pycode" and alias.name in set(FEATURE_DEPENDENCIES) | LEGACY
                ):
                    yield node, submodule, None, alias.asname or alias.name
                else:
                    yield node, base, alias.name, alias.asname or alias.name


def public_exports(source: str) -> set[str] | None:
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                return None
            if isinstance(value, (list, tuple)) and all(isinstance(x, str) for x in value):
                return set(value)
    return None


def symbol_origin(module: str, symbol: str | None, sources, packages, seen=None):
    """Resolve a real definition, including aliases and adapter-producing wrappers."""
    if symbol is None or module not in sources:
        return module
    visited = set() if seen is None else seen
    key = module, symbol
    if key in visited:
        return None
    visited.add(key)
    source = sources[module]
    imports = {
        binding: (target, name)
        for _, target, name, binding in import_refs(source, module, module in packages, sources)
    }
    bindings = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            bindings[node.target.id] = node.value

    def expression_origin(expression):
        if isinstance(expression, ast.Name):
            return symbol_origin(module, expression.id, sources, packages, set(visited))
        if isinstance(expression, ast.Attribute):
            parts = []
            while isinstance(expression, ast.Attribute):
                parts.insert(0, expression.attr)
                expression = expression.value
            if isinstance(expression, ast.Name) and expression.id in imports:
                target, imported = imports[expression.id]
                if imported is not None:
                    return symbol_origin(target, imported, sources, packages, set(visited))
                full = target + "." + ".".join(parts)
                prefixes = [name for name in sources if full.startswith(name + ".")]
                if prefixes:
                    longest = max(prefixes, key=len)
                    return symbol_origin(
                        longest,
                        full[len(longest) + 1 :].split(".")[0],
                        sources,
                        packages,
                        set(visited),
                    )
                return target
        if isinstance(expression, ast.Call):
            return expression_origin(expression.func)
        return None

    if symbol in bindings:
        node = bindings[symbol]
        if isinstance(node, (ast.Name, ast.Attribute)):
            return expression_origin(node)
        if isinstance(node, ast.Call):
            origin = expression_origin(node)
            return origin if origin is not None else module
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and layer_of(module) is None
        ):
            # Factory wrappers/subclasses cannot launder an adapter through a public name.
            for child in ast.walk(node):
                expression = child.func if isinstance(child, ast.Call) else None
                if isinstance(node, ast.ClassDef) and child in node.bases:
                    expression = child
                if expression is not None:
                    origin = expression_origin(expression)
                    if origin is not None and "adapters" in origin.split("."):
                        return origin
        return module
    if symbol in imports:
        target, imported = imports[symbol]
        return symbol_origin(target, imported, sources, packages, set(visited))
    return None


def delayed_imports(tree):
    found = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.depth = 0

        def visit_FunctionDef(self, node):
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Import(self, node):
            if self.depth:
                found.append(node)

        visit_ImportFrom = visit_Import

    Visitor().visit(tree)
    return found


def dependency_errors(
    source: str, module: str, is_package: bool = False, *, sources=None, packages=None
):
    sources, packages = sources or {}, packages or set()
    top = module.split(".")[0]
    feature, layer = feature_of(module), layer_of(module)
    composition = (
        module == "pycode.app.bootstrap"
        or module == "pycode.app.composition"
        or module.startswith("pycode.app.composition.")
    )
    assembly = module == f"pycode.{feature}.module"
    errors = []
    if feature in LEGACY:
        errors.append(f"{module}: legacy layer package must be removed")
    tree = ast.parse(source)
    refs = list(import_refs(source, module, is_package, sources))
    aliases = {}
    for node, target, symbol, binding in refs:
        aliases[binding] = target + ("." + symbol if symbol else "")
        if target.split(".")[0] in FORBIDDEN_IMPORTS.get(top, ()):
            errors.append(f"{module}:{node.lineno}: {top} cannot import {target}")
        app_target = target + "." + symbol if target == "pycode.app" and symbol else target
        errors.extend(app_import_errors(module, app_target, node.lineno))
        if top != "pycode":
            if target.startswith("pycode.app"):
                origin = symbol_origin(target, symbol, sources, packages)
                public_input = (
                    target == "pycode.app.api"
                    and symbol in {"SessionResources", "RunOptions"}
                    and origin == "pycode.app.composition.inputs"
                )
                if origin is not None and origin != app_target and not public_input:
                    errors.extend(app_import_errors(module, origin, node.lineno))
            continue
        other = feature_of(target)
        if other in LEGACY:
            errors.append(f"{module}:{node.lineno}: legacy import {target}")
        if (
            layer == "domain" or feature == "kernel" and layer in {"ports", "services"}
        ) and target.split(".")[0] in CORE_IO:
            errors.append(f"{module}:{node.lineno}: domain cannot import I/O module {target}")
        if layer in {"domain", "ports", "services"} and target.split(".")[0] in FRAMEWORKS:
            errors.append(f"{module}:{node.lineno}: framework dependency outside adapters")
        origin = symbol_origin(target, symbol, sources, packages)
        if origin is not None and origin != app_target:
            errors.extend(app_import_errors(module, origin, node.lineno))
        if feature == "app" and (
            target.split(".")[0] in FRAMEWORKS
            or origin is not None
            and origin.split(".")[0] in FRAMEWORKS
        ):
            errors.append(f"{module}:{node.lineno}: app cannot import framework dependencies")
        origin_layer = layer_of(origin) if origin is not None else None
        if origin is None and target in sources and symbol is not None:
            errors.append(f"{module}:{node.lineno}: {symbol} has no definition in {target}")
        if (
            layer in {"domain", "ports", "services"}
            and origin_layer
            and RANK[origin_layer] > RANK[layer]
        ):
            errors.append(
                f"{module}:{node.lineno}: {layer} cannot depend on {origin_layer} {origin}"
            )
        target_assembly = other is not None and target == f"pycode.{other}.module"
        origin_assembly = (
            origin is not None and len(origin.split(".")) >= 3 and origin.split(".")[2] == "module"
        )
        if target_assembly or origin_assembly:
            if not composition or not target_assembly or symbol is None:
                errors.append(
                    f"{module}:{node.lineno}: feature assembly requires an explicit module entry "
                    "at the application composition root"
                )
            elif other in FEATURE_DEPENDENCIES.get(feature, set()):
                entry_source = sources.get(target)
                if entry_source is not None and not any(
                    isinstance(definition, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and definition.name == symbol
                    for definition in ast.parse(entry_source).body
                ):
                    errors.append(
                        f"{module}:{node.lineno}: module entry cannot forward "
                        f"imported implementations: {symbol}"
                    )
                continue
        if not other or other == feature:
            continue
        if other not in FEATURE_DEPENDENCIES.get(feature, set()):
            errors.append(f"{module}:{node.lineno}: disallowed feature edge {feature} -> {other}")
        # Shared OS infrastructure is explicit and only usable from implementation
        # or assembly boundaries; native contracts/services cannot acquire I/O.
        kernel_infrastructure = other == "kernel" and target.startswith("pycode.kernel.adapters.")
        flat_io = (
            feature in FLAT_FEATURES
            and len(module.split(".")) == 3
            and module.split(".")[2] in FLAT_IO_MODULES
        )
        if kernel_infrastructure and (layer == "adapters" or composition or assembly or flat_io):
            continue
        if "adapters" in target.split(".") or (
            origin is not None and "adapters" in origin.split(".")
        ):
            errors.append(
                f"{module}:{node.lineno}: cross-feature adapters require composition "
                "through the feature module entry"
            )
        if target != "pycode." + other or symbol is None:
            errors.append(
                f"{module}:{node.lineno}: cross-feature imports require named public API of {other}"
            )
        elif target in sources:
            exports = public_exports(sources[target])
            if exports is None or symbol not in exports:
                errors.append(f"{module}:{node.lineno}: {symbol} is not exported by {target}")
    if top == "pycode":
        for node in delayed_imports(tree):
            errors.append(
                f"{module}:{node.lineno}: function-local import must move to module scope"
            )
    if layer == "domain" or feature == "kernel" and layer in {"ports", "services"}:

        def qualified(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return qualified(node.value) + "." + node.attr
            return ""

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = qualified(node.value)
            if isinstance(node, ast.Call) and qualified(node.func) in {
                "open",
                "input",
                "print",
                "builtins.open",
                "builtins.input",
                "builtins.print",
            }:
                errors.append(f"{module}:{node.lineno}: domain cannot perform I/O")
    if top == "pycode":

        def dynamic_name(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return dynamic_name(node.value) + "." + node.attr
            return ""

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and dynamic_name(node.func) in {
                "importlib.import_module",
                "__import__",
                "builtins.__import__",
            }:
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    if node.args[0].value == "pycode" or node.args[0].value.startswith("pycode."):
                        errors.append(
                            f"{module}:{node.lineno}: dynamic pycode imports "
                            "hide feature dependencies"
                        )
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and dynamic_name(target.value) == "sys.modules"
                    ):
                        key = target.slice
                        if (
                            isinstance(key, ast.Constant)
                            and isinstance(key.value, str)
                            and (key.value == "pycode" or key.value.startswith("pycode."))
                        ):
                            errors.append(
                                f"{module}:{node.lineno}: pycode module alias "
                                "registration is forbidden"
                            )
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "__getattr__"
            ):
                errors.append(f"{module}:{node.lineno}: lazy module APIs are forbidden")
    return list(dict.fromkeys(errors))


def dependency_cycles(graph):
    completed, stack, found = set(), [], []

    def visit(module):
        if module in stack:
            found.append(stack[stack.index(module) :] + [module])
            return
        if module in completed:
            return
        stack.append(module)
        for target in sorted(graph.get(module, ())):
            visit(target)
        stack.pop()
        completed.add(module)

    for module in sorted(graph):
        visit(module)
    return found


def check_dependencies(root: Path = ROOT) -> list[str]:
    source_root = root / "src"
    sources, packages = {}, set()
    for path in sorted((source_root / "pycode").rglob("*.py")):
        module, package = module_name(path.relative_to(source_root))
        sources[module] = path.read_text(encoding="utf-8")
        if package:
            packages.add(module)
    errors = app_layout_errors(source_root)
    module_graph, feature_graph = defaultdict(set), defaultdict(set)
    for module, source in sources.items():
        errors.extend(
            dependency_errors(
                source, module, module in packages, sources=sources, packages=packages
            )
        )
        for _, target, symbol, _ in import_refs(source, module, module in packages, sources):
            origin = symbol_origin(target, symbol, sources, packages)
            if target in sources:
                module_graph[module].add(target)
            if origin in sources and origin != target:
                module_graph[module].add(origin)
            feature, other = feature_of(module), feature_of(origin) if origin is not None else None
            if feature and other and feature != other:
                feature_graph[feature].add(other)
        if module in {"pycode." + name for name in FEATURE_DEPENDENCIES if name != "app"}:
            exports = public_exports(source)
            if exports is None:
                errors.append(f"{module}: public API requires a literal __all__")
            for symbol in exports or ():
                origin = symbol_origin(module, symbol, sources, packages)
                if origin is None:
                    errors.append(f"{module}: public API exports undefined symbol {symbol}")
                elif (
                    "adapters" in origin.split(".")
                    or "module" in origin.split(".")
                    or origin.split(".")[0] in FRAMEWORKS
                ):
                    errors.append(
                        f"{module}: public API cannot re-export adapter {symbol} from {origin}"
                    )
    kernel_root = source_root / "pycode" / "kernel"
    for path in kernel_root.rglob("*.py"):
        relative = path.relative_to(kernel_root)
        if relative.as_posix() == "__init__.py":
            continue
        if len(relative.parts) < 2 or relative.parts[0] not in {
            "domain",
            "ports",
            "services",
            "adapters",
        }:
            errors.append(f"kernel source must use domain/ports/services/adapters: {relative}")
    for label, graph in [("module", module_graph), ("feature", feature_graph)]:
        errors.extend(
            label + " dependency cycle: " + " -> ".join(cycle) for cycle in dependency_cycles(graph)
        )
    manifest = root / "docs" / "design" / "feature-dependencies.json"
    if manifest.exists():
        declared = json.loads(manifest.read_text())["allowed_dependencies"]
        if {k: set(v) for k, v in declared.items()} != FEATURE_DEPENDENCIES:
            errors.append("Feature dependency manifest differs from enforced architecture")
    for path in sorted([*(root / "evals").rglob("*.py"), *(root / "scripts").rglob("*.py")]):
        relative = path.relative_to(root)
        if any(part in EVAL_GENERATED or part.startswith(".") for part in relative.parts[1:]):
            continue
        module, package = module_name(relative)
        errors.extend(
            dependency_errors(
                path.read_text(encoding="utf-8"),
                module,
                package,
                sources=sources,
                packages=packages,
            )
        )
    return errors


def check_port_implementations(root: Path = ROOT) -> list[str]:
    """Every declared public port must name an existing non-Protocol production binding."""
    manifest = root / "docs" / "design" / "port-implementations.json"
    if not manifest.exists():
        return []
    records = json.loads(manifest.read_text())["ports"]
    bindings = {record["port"]: record for record in records}
    found = set()
    errors = []
    source_root = root / "src"
    for path in (source_root / "pycode").rglob("*.py"):
        if (
            "ports" not in path.parts
            and "contracts" not in path.parts
            and path.name != "contracts.py"
        ):
            continue
        module, _ = module_name(path.relative_to(source_root))
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.ClassDef):
                continue
            protocol = any(
                (isinstance(base, ast.Name) and base.id == "Protocol")
                or (
                    isinstance(base, ast.Subscript)
                    and isinstance(base.value, ast.Name)
                    and base.value.id == "Protocol"
                )
                for base in node.bases
            )
            if protocol:
                found.add(module + "." + node.name)
    for port in sorted(found - bindings.keys()):
        errors.append("Port has no implementation mapping: " + port)
    for port in sorted(bindings.keys() - found):
        errors.append("Implementation mapping names no current port: " + port)
    for port, record in bindings.items():
        module, _, symbol = record["implementation"].rpartition(".")
        path = source_root.joinpath(*module.split(".")).with_suffix(".py")
        if not path.exists():
            errors.append("Port implementation module is missing: " + record["implementation"])
            continue
        candidates = [
            node
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == symbol
        ]
        if not candidates:
            errors.append("Port implementation symbol is missing: " + record["implementation"])
        elif any(
            isinstance(node, ast.ClassDef)
            and any(
                (isinstance(base, ast.Name) and base.id == "Protocol")
                or (
                    isinstance(base, ast.Subscript)
                    and isinstance(base.value, ast.Name)
                    and base.value.id == "Protocol"
                )
                for base in node.bases
            )
            for node in candidates
        ):
            errors.append("Protocol is not a concrete implementation: " + port)
    return errors


def branding_excluded(relative: Path) -> bool:
    parts = relative.parts
    if parts[:1] == ("evals",) and any(part in EVAL_GENERATED for part in parts[1:]):
        return True
    if parts[:2] == ("tests", "evals") and any(part in EVAL_GENERATED for part in parts[2:]):
        return True
    return False


def check_branding(root: Path = ROOT) -> list[str]:
    obsolete = re.compile("mew" + "code", re.IGNORECASE)
    errors = []
    excluded = {
        ".venv",
        ".source-cache",
        ".git",
        ".worktrees",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "dist",
        "build",
        ".runtime-tests",
        ".t",
        ".fix-20261013",
        "audit-20261013",
        "dependency-artifacts",
    }
    extensions = {".py", ".md", ".toml", ".json", ".tcss", ".txt", ".yaml", ".yml"}
    paths = []
    for directory, names, files in os.walk(root):
        current = Path(directory)
        # Prune excluded trees instead of walking caches and worktrees.
        names[:] = [
            name
            for name in names
            if name not in excluded and not branding_excluded((current / name).relative_to(root))
        ]
        paths.extend(current / name for name in files)
    for path in sorted(paths):
        relative = path.relative_to(root)
        if branding_excluded(relative):
            continue
        # Source attribution keeps actual upstream directory names; licenses keep notices.
        if relative.as_posix() == "docs/source-map.md" or relative.parts[:2] == (
            "docs",
            "licenses",
        ):
            continue
        if path.suffix not in extensions or path.name.startswith("._"):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not obsolete.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith("#") and ("Source:" in line or "来源" in line):
                continue
            errors.append(f"{relative.as_posix()}:{number}: obsolete product brand")
    return errors


def check_harness(root: Path = ROOT) -> list[str]:
    errors = []
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = set(project["dependencies"])
    for locked in ("deepagents==0.7.32", "langchain[mcp]==1.4.7", "langgraph==1.2.18"):
        if locked not in dependencies:
            errors.append("Final harness dependency is missing: " + locked)
    if project.get("scripts") != {"pycode": "pycode.app.cli.main:main"}:
        errors.append("Only the pycode console entry point may be registered")
    for dependency in dependencies:
        name = re.split(r"[<>=\[ ]", dependency)[0]
        if name in {"mcp", "langchain-mcp-adapters"}:
            errors.append("Obsolete main-runtime direct dependency: " + name)
    for path in sorted((root / "src" / "pycode").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if (
                    module == "mcp"
                    or module.startswith("mcp.")
                    or module.startswith("langchain_mcp_adapters")
                ):
                    errors.append(f"{relative}:{node.lineno}: use LangChain MCPAdapter")
                if module == "langchain.agents" and any(
                    alias.name == "create_agent" for alias in node.names
                ):
                    errors.append(
                        f"{relative}:{node.lineno}: use create_deep_agent for Agent execution"
                    )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "execute_batch":
                    errors.append(f"{relative}:{node.lineno}: obsolete tool batch scheduler")
        for obsolete in (
            "CREATE TABLE IF NOT EXISTS tool_operations",
            "CREATE TABLE IF NOT EXISTS runtime_events",
        ):
            if obsolete in source:
                errors.append(f"{relative}: obsolete main-runtime persistence: {obsolete}")
    return errors


def main() -> int:
    errors = (
        check_dependencies() + check_branding() + check_harness() + check_port_implementations()
    )
    for error in errors:
        print(error)
    if errors:
        return 1
    print("Architecture and branding checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
