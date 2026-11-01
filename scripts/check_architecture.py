"""Two architecture rules (dependency direction, package-root imports) plus the branding check."""

import ast
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ("kernel", "models", "context", "tools", "agent", "session")
FEATURES = frozenset(
    {
        "sandbox",
        "snapshot",
        "memory",
        "skills",
        "mcp",
        "hooks",
        "plans",
        "tasks",
        "subagents",
        "loopguard",
    }
)
# Rule 1: each package may import only these pycode packages (besides itself).
DEPENDENCIES = {name: frozenset(CORE[:index]) for index, name in enumerate(CORE)}
DEPENDENCIES.update(dict.fromkeys(FEATURES, frozenset(CORE)))
DEPENDENCIES["ui"] = frozenset(CORE) | FEATURES
DEPENDENCIES["app"] = DEPENDENCIES["ui"] | {"ui"}
# kernel/io.py is the only kernel file allowed to do I/O.
KERNEL_IO = "pycode.kernel.io"
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
EVAL_GENERATED = {"cache", "work", "results"}


def module_name(relative: Path) -> tuple[str, bool]:
    parts = list(relative.with_suffix("").parts)
    package = parts[-1] == "__init__"
    if package:
        parts.pop()
    return ".".join(parts), package


def package_of(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] != "pycode" or len(parts) < 2:
        return None
    return "app" if parts[1] == "__main__" else parts[1]


def import_base(node: ast.ImportFrom, module: str, package: bool) -> str:
    if not node.level:
        return node.module or ""
    parts = module.split(".") if package else module.split(".")[:-1]
    return ".".join(parts[: len(parts) - node.level + 1] + ([node.module] if node.module else []))


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


def type_checking_imports(tree: ast.AST) -> set[ast.stmt]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and (
            isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
            or isinstance(node.test, ast.Attribute)
            and node.test.attr == "TYPE_CHECKING"
        ):
            for child in node.body:
                for inner in ast.walk(child):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        found.add(inner)
    return found


def imports(tree: ast.AST, module: str, package: bool):
    """Yield (node, target module, imported name or None) for every import in the file."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name, None
        elif isinstance(node, ast.ImportFrom):
            base = import_base(node, module, package)
            for alias in node.names:
                yield node, base, alias.name


def dependency_errors(source: str, module: str, package: bool, exports) -> list[str]:
    """Both rules for one file. `exports` maps a package root module to its literal __all__."""
    own = package_of(module)
    top = module.split(".")[0]
    tree = ast.parse(source)
    typing_only = type_checking_imports(tree)
    errors = []
    for node, target, name in imports(tree, module, package):
        where = f"{module}:{node.lineno}"
        root = target.split(".")[0]
        # Rule 1: dependency direction (type-only imports create no runtime edge).
        if top == "pycode" and node not in typing_only:
            if root in {"evals", "scripts"}:
                errors.append(f"{where}: pycode cannot import {target}")
            if own == "kernel" and root in FRAMEWORKS:
                errors.append(f"{where}: kernel cannot import framework {target}")
            if own == "kernel" and module != KERNEL_IO and root in CORE_IO:
                errors.append(f"{where}: only kernel/io.py may import I/O module {target}")
            other = package_of(target)
            if own and other and other != own and other not in DEPENDENCIES.get(own, ()):
                errors.append(f"{where}: {own} cannot depend on {other}")
        # Rule 2: across packages, import only names listed in the package root's __all__.
        if root != "pycode":
            continue
        other = package_of(target)
        if other is None:
            # `from pycode import tools` names a package; `__version__` is metadata.
            if name is not None and name not in DEPENDENCIES and not name.startswith("__"):
                errors.append(f"{where}: import pycode packages, not pycode.{name}")
            continue
        if other == own:
            continue
        root_module = "pycode." + other
        if target != root_module:
            errors.append(f"{where}: import from {root_module}, not {target}")
        elif name is not None and name not in exports.get(root_module, ()):
            errors.append(f"{where}: {name} is not in {root_module}.__all__")
    return list(dict.fromkeys(errors))


def check_dependencies(root: Path = ROOT) -> list[str]:
    source_root = root / "src"
    exports = {}
    for path in sorted((source_root / "pycode").glob("*/__init__.py")):
        module, _ = module_name(path.relative_to(source_root))
        exports[module] = public_exports(path.read_text(encoding="utf-8")) or set()
    files = [
        (path, path.relative_to(source_root)) for path in (source_root / "pycode").rglob("*.py")
    ]
    for path in [*(root / "evals").rglob("*.py"), *(root / "scripts").rglob("*.py")]:
        relative = path.relative_to(root)
        if not any(part in EVAL_GENERATED or part.startswith(".") for part in relative.parts[1:]):
            files.append((path, relative))
    errors = []
    for path, relative in sorted(files):
        module, package = module_name(relative)
        errors.extend(dependency_errors(path.read_text(encoding="utf-8"), module, package, exports))
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
        ".agents",
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
        # Design documents name their upstream sources the same way.
        if relative.as_posix() in {
            "docs/source-map.md",
            "11.1.md",
            "docs/design/structure-slim-round2.md",
        } or relative.parts[:2] == (
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
            if stripped.startswith("#") and any(
                mark in line for mark in ("Source:", "来源", "参照")
            ):
                continue
            errors.append(f"{relative.as_posix()}:{number}: obsolete product brand")
    return errors


def main() -> int:
    errors = check_dependencies() + check_branding()
    for error in errors:
        print(error)
    if errors:
        return 1
    print("Architecture and branding checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
