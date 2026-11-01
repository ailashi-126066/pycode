"""Regression fixtures for false call targets; fixture source is parsed, never executed."""

import tempfile
from pathlib import Path

from source_index import build_index, module_name

FIXTURE = """from typing import overload
from .helper import imported

def target():
    return 1

def invoke(target):
    return target()

def imported_shadow(imported):
    return imported()

def direct_import():
    return imported()

def local_shadow():
    imported = target
    return imported()

def lexical():
    def child():
        return 3
    return child()

def helper():
    return 'global'

class Owner:
    def helper(self):
        return 'method'
    def method(self):
        def helper():
            return 'local'
        def inner():
            return self.helper()
        return inner()
    def bare(self):
        return helper()
    def masked(self):
        def inner(self):
            return self.helper()
        return inner

@overload
def convert(value: int) -> int:
    ...
@overload
def convert(value: str) -> str:
    ...
def convert(value):
    return value

def conditional(flag):
    if flag:
        def respond():
            return 1
    else:
        def respond():
            return 2
    return respond()
"""


def main():
    assert module_name("src/__init__.py") == "pycode"
    assert module_name("src/sample.py") == "pycode.sample"
    assert module_name("src/agent/__init__.py") == "pycode.agent"
    with tempfile.TemporaryDirectory(prefix="pycode-doc-index-") as temporary:
        root = Path(temporary)
        package = root / "src"
        package.mkdir(parents=True)
        (package / "sample.py").write_text(FIXTURE, encoding="utf-8")
        (package / "helper.py").write_text("def imported():\n    return 7\n", encoding="utf-8")
        indexed = build_index(root)
        sample = next(file for file in indexed if file["path"].endswith("sample.py"))
        assert sample["module"] == "pycode.sample"
        assert sample["imports"]["imported"] == "pycode.helper.imported"
        symbols = {symbol["name"]: symbol for symbol in sample["symbols"]}

        def call(name, expression):
            return next(c for c in symbols[name]["calls"] if c["expression"] == expression)

        assert "target" not in call("invoke", "target")
        assert "target" not in call("imported_shadow", "imported")
        assert "target" not in call("local_shadow", "imported")
        assert call("direct_import", "imported")["target"]["file"].endswith("helper.py")
        assert call("lexical", "child")["target"]["name"] == "lexical.child"
        assert call("Owner.method.inner", "self.helper")["target"]["name"] == "Owner.helper"
        assert call("Owner.bare", "helper")["target"]["name"] == "helper"
        assert "target" not in call("Owner.masked.inner", "self.helper")
        assert len([s for s in sample["symbols"] if s["name"] == "convert"]) == 1
        assert len(symbols["convert"]["overloads"]) == 2
        selected = symbols["convert"]
        code = "\n".join(sample["code"].splitlines()[selected["start"] - 1 : selected["end"]])
        assert "return value" in code and "..." not in code
        assert "target" not in call("conditional", "respond")
        assert len([s for s in sample["symbols"] if s.get("ambiguous_branch")]) == 2
        outside = root / "private.py"
        outside.write_text("private_marker = 'not indexed'\n", encoding="utf-8")
        (package / "linked.py").symlink_to(outside)
        try:
            build_index(root)
        except ValueError as error:
            assert "symlink" in str(error)
        else:
            raise AssertionError("Source symlink was indexed")
    print(
        "passed: shadowing, lexical scopes, receiver capture, "
        "overloads, branch identity, symlink guard"
    )


if __name__ == "__main__":
    main()
