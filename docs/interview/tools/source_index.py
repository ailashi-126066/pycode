"""Parse source without executing it; uncertain dynamic targets remain unresolved."""

import ast
import hashlib
from pathlib import Path

EXCLUDED = {"__pycache__", "cache", "work", "results", "memory_rules", ".runtime-tests"}
DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def module_name(path):
    parts = list(Path(path).with_suffix("").parts)
    if parts[0] == "src":
        parts[0] = "pycode"
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return base + "." + node.attr if base else ""
    return ""


class BodyCalls(ast.NodeVisitor):
    def __init__(self, code):
        self.code, self.calls = code, []

    def visit_Call(self, node):
        expression = (
            dotted(node.func) or ast.get_source_segment(self.code, node.func) or "<dynamic>"
        )
        self.calls.append(
            {
                "expression": expression,
                "line": node.lineno,
                "code": ast.get_source_segment(self.code, node) or expression,
            }
        )
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        return

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef


class LocalBindings(ast.NodeVisitor):
    """Unknown bindings stay unresolved; nested declarations keep lexical identity."""

    def __init__(self):
        self.unknown, self.declared = set(), set()

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.unknown.add(node.id)

    def visit_FunctionDef(self, node):
        self.declared.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Import(self, node):
        self.unknown.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node):
        self.unknown.update(alias.asname or alias.name for alias in node.names)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.unknown.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.unknown.update(node.names)

    visit_Nonlocal = visit_Global

    def visit_Lambda(self, node):
        # Do not resolve its captures as if the lambda parameters were absent.
        self.unknown.update(
            a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        )
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self.generic_visit(node)


def collect_imports(tree, module, package):
    imports = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parents = module.split(".") if package else module.split(".")[:-1]
                base = ".".join(parents[: len(parents) - node.level + 1] + ([base] if base else []))
            for alias in node.names:
                if alias.name != "*":
                    imports[alias.asname or alias.name] = base + "." + alias.name
    return imports


def child_statements(node):
    statements = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt):
            statements.append(child)
        elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
            statements.extend(child.body)
    return statements


def collect_symbols(
    tree,
    code,
    prefix="",
    owner=None,
    scopes=(),
    captured=frozenset(),
    receivers=frozenset(),
    direct_class=False,
):
    symbols = []
    for node in tree.body:
        if not isinstance(node, DEFINITIONS):
            body = child_statements(node)
            if body:
                symbols.extend(
                    collect_symbols(
                        ast.Module(body=body, type_ignores=[]),
                        code,
                        prefix,
                        owner,
                        scopes,
                        captured,
                        receivers,
                        direct_class,
                    )
                )
            continue
        name = prefix + node.name
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        collector = BodyCalls(code)
        unknown, own_receivers = set(captured), set(receivers)
        function_scopes = scopes
        if isinstance(node, FUNCTIONS):
            bound = LocalBindings()
            for child in node.body:
                bound.visit(child)
                collector.visit(child)
            args = node.args
            parameters = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
            parameters.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
            unknown.update(bound.unknown | parameters)
            own_receivers.difference_update(bound.unknown | parameters)
            decorators = {dotted(d) for d in node.decorator_list}
            if direct_class and owner and "staticmethod" not in decorators:
                first = [*args.posonlyargs, *args.args]
                if first and first[0].arg in {"self", "cls"} and first[0].arg not in bound.unknown:
                    own_receivers.add(first[0].arg)
            function_scopes = (*scopes, name)
        symbol = {
            "name": name,
            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
            "start": start,
            "end": node.end_lineno,
            "comment": ast.get_docstring(node) or "",
            "calls": collector.calls,
            "owner": owner,
            "lexical_scopes": list(function_scopes),
            "shadowed": sorted(unknown),
            "receivers": sorted(own_receivers),
            "overload": any(dotted(d).split(".")[-1] == "overload" for d in node.decorator_list),
        }
        symbols.append(symbol)
        if isinstance(node, ast.ClassDef):
            symbols.extend(
                collect_symbols(node, code, name + ".", name, scopes, captured, frozenset(), True)
            )
        else:
            symbols.extend(
                collect_symbols(
                    node,
                    code,
                    name + ".",
                    owner,
                    function_scopes,
                    frozenset(unknown),
                    frozenset(own_receivers),
                    False,
                )
            )
    return symbols


def canonical_symbols(symbols):
    groups = {}
    for symbol in symbols:
        groups.setdefault(symbol["name"], []).append(symbol)
    result = []
    for name, variants in groups.items():
        implementations = [symbol for symbol in variants if not symbol["overload"]]
        if len(implementations) > 1:
            for variant in implementations:
                selected = dict(variant)
                selected["name"] = name + "@L" + str(variant["start"])
                selected["qualified_name"] = name
                selected["ambiguous_branch"] = True
                selected["overloads"] = []
                result.append(selected)
            continue
        original = implementations[0] if implementations else variants[-1]
        selected = dict(original)
        selected["overloads"] = [
            {"start": s["start"], "end": s["end"]}
            for s in variants
            if s is not original and s["overload"]
        ]
        result.append(selected)
    return result


def build_index(root):
    root = root.resolve()
    selected = [
        *(root / "src").rglob("*.py"),
        *(root / "evals").rglob("*.py"),
        *(root / "scripts").glob("*.py"),
    ]
    allowed = (root / "src", root / "evals", root / "scripts")
    files = []
    for path in sorted(set(selected)):
        relative = path.relative_to(root)
        if any(part in EXCLUDED or part.startswith(".") for part in relative.parts):
            continue
        if relative.parts[:2] == ("evals", "tests"):
            continue
        if any(
            p.is_symlink() for p in (path, *path.parents) if p != root and p.is_relative_to(root)
        ):
            raise ValueError(f"Source index rejects symlink: {relative}")
        if not any(path.resolve().is_relative_to(scope) for scope in allowed):
            raise ValueError(f"Source index escapes its explicit roots: {relative}")
        raw = path.read_bytes()
        code = raw.decode("utf-8")
        tree = ast.parse(code, filename=relative.as_posix())
        module = module_name(relative)
        bindings = LocalBindings()
        for child in tree.body:
            if not isinstance(child, (ast.Import, ast.ImportFrom)):
                bindings.visit(child)
        files.append(
            {
                "path": relative.as_posix(),
                "module": module,
                "code": code,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "imports": collect_imports(tree, module, path.name == "__init__.py"),
                "shadowed": sorted(bindings.unknown),
                "symbols": canonical_symbols(collect_symbols(tree, code)),
            }
        )
    modules = {file["module"]: file for file in files}
    declarations = {
        file["module"] + "." + symbol["name"]: (file, symbol)
        for file in files
        for symbol in file["symbols"]
    }

    def resolve_qualified(name, visited=None):
        visited = set() if visited is None else visited
        if name in visited:
            return None
        visited.add(name)
        if name in declarations:
            return declarations[name]
        parts = name.split(".")
        for n in range(len(parts) - 1, 0, -1):
            base = modules.get(".".join(parts[:n]))
            if base and parts[n] in base["imports"] and parts[n] not in base["shadowed"]:
                replacement = base["imports"][parts[n]]
                return resolve_qualified(".".join([replacement, *parts[n + 1 :]]), visited)
        return None

    for file in files:
        for symbol in file["symbols"]:
            for call in symbol["calls"]:
                expression = call["expression"]
                parts = expression.split(".")
                candidates = []
                if parts[0] in symbol["receivers"] and len(parts) == 2 and symbol["owner"]:
                    candidates.append(file["module"] + "." + symbol["owner"] + "." + parts[1])
                elif parts[0] not in {*symbol["shadowed"], *file["shadowed"]}:
                    for scope in reversed(symbol["lexical_scopes"]):
                        candidates.append(file["module"] + "." + scope + "." + expression)
                    candidates.append(file["module"] + "." + expression)
                    if parts[0] in file["imports"]:
                        candidates.append(".".join([file["imports"][parts[0]], *parts[1:]]))
                target = next(
                    (found for name in candidates if (found := resolve_qualified(name))), None
                )
                if target:
                    call["target"] = {
                        "file": target[0]["path"],
                        "name": target[1]["name"],
                        "basis": "explicit import, lexical declaration, or class receiver",
                    }
                else:
                    call["resolution"] = (
                        "library, shadowed/injected/dynamic target, or callback; see curated chains"
                    )
    return files
