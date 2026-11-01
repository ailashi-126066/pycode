#!/usr/bin/env python3
"""Build offline prose, nested call chains and a Python source browser from real source."""

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from source_index import build_index

BOOK = Path(__file__).resolve().parents[1]
ROOT = BOOK.parents[1]
REPOSITORY = "https://github.com/ailashi-126066/pycode"
REQUIRED = ("框架",)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def load_specs(pattern):
    result = {}
    for path in sorted((BOOK / "content").glob(pattern)):
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            if key in result:
                raise ValueError(f"Duplicate documentation key {key}")
            result[key] = value
    return result


def build(check=False):
    files = build_index(ROOT)
    by_file = {file["path"]: file for file in files}
    symbols = {
        file["path"] + "::" + symbol["name"]: symbol for file in files for symbol in file["symbols"]
    }
    chains, inline = load_specs("chains-*.json"), load_specs("inline-*.json")
    counts = {
        "chapters": 0,
        "files": len(files),
        "symbols": len(symbols),
        "interactive_chains": 0,
        "complete_code_blocks": 0,
        "helper_nodes": 0,
        "explained_symbols": 0,
    }
    used_chains, used_symbols, explanations = set(), set(), {}
    reference_errors = []

    def render_node(node, number="", helper=False, trail=()):
        path, name = node["file"], node["name"]
        key = path + "::" + name
        if key not in symbols:
            raise ValueError(f"Missing Python symbol: {key}")
        if key in trail:
            raise ValueError(f"Recursive node expansion: {key}")
        if not node.get("purpose") or len(node.get("notes", [])) < 2:
            raise ValueError(f"Node requires purpose and at least two concrete explanations: {key}")
        if not isinstance(node.get("guide"), str) or not node["guide"].strip():
            raise ValueError(f"Node requires a reader guide explaining why it is called: {key}")
        symbol = symbols[key]
        code = "\n".join(by_file[path]["code"].splitlines()[symbol["start"] - 1 : symbol["end"]])
        counts["complete_code_blocks"] += 1
        counts["helper_nodes"] += int(helper)
        used_symbols.add(key)
        explanations.setdefault(key, node["purpose"])
        notes = "".join("<li>" + html.escape(note) + "</li>" for note in node["notes"])
        relationship = (
            ('<p class="call-code-caption">关系：' + html.escape(node["edge"]) + "</p>")
            if helper and node.get("edge")
            else ""
        )
        children = "".join(
            render_node(child, helper=True, trail=(*trail, key))
            for child in node.get("helpers", [])
        )
        if children:
            children = (
                '<div class="call-children"><p class="call-child-title">'
                "这段处理还会用到下面的方法：先读它们分别帮什么忙，再按需展开代码。</p>"
                + children
                + "</div>"
            )
        badge = '<span class="call-number">' + html.escape(number) + "</span>" if number else ""
        return (
            '<div class="reading-step"><p class="reading-guide" data-call-guide="'
            + html.escape(key, quote=True)
            + '">'
            + html.escape(node["guide"])
            + "</p>"
            f'<details class="call-node{" call-helper" if helper else ""}" '
            f'data-function="{html.escape(key, quote=True)}"><summary>{badge}'
            '<span class="call-label"><strong>' + html.escape(node["purpose"]) + "</strong>"
            '<span class="call-signature">' + html.escape(name) + "</span>"
            '<span class="call-file">' + html.escape(path) + "</span></span>"
            '<span class="call-toggle" aria-hidden="true"></span></summary>'
            '<div class="call-body">' + relationship + "<ol>" + notes + "</ol>"
            '<p class="call-code-caption">完整'
            + ("定义" if symbol["kind"] == "class" else "函数")
            + f" · 源码 L{symbol['start']}–L{symbol['end']}</p>"
            '<pre><code class="language-python">'
            + html.escape(code).replace("\n", "&#10;")
            + "</code></pre>"
            + children
            + "</div></details></div>"
        )

    def chain_block(match):
        key = match.group(1)
        if key not in chains:
            raise ValueError(f"Missing chain {key}")
        if key in used_chains:
            raise ValueError(f"Each chain must appear once; duplicate {key}")
        used_chains.add(key)
        spec = chains[key]
        if not spec.get("nodes") or not spec.get("result"):
            raise ValueError(f"Chain requires input, actual nodes and result: {key}")
        counts["interactive_chains"] += 1
        body = [
            '<div class="call-terminal"><span class="call-eyebrow">'
            + html.escape(spec.get("input_label", "入口"))
            + "</span><pre><code>"
            + html.escape(spec["request"])
            + "</code></pre></div>"
        ]
        if spec.get("groups"):
            body.append('<div class="call-branches">')
        for i, node in enumerate(spec["nodes"], 1):
            if spec.get("groups"):
                body.append("<div>")
            edge = node.get(
                "edge",
                "这是另一条单独的入口"
                if spec.get("groups")
                else "进入"
                if i == 1
                else "接着看这一步",
            )
            body.append(
                '<div class="call-arrow" aria-hidden="true"><span>'
                + html.escape(edge)
                + "</span>↓</div>"
            )
            body.append(render_node(node, str(i)))
            if spec.get("groups"):
                body.append("</div>")
        if spec.get("groups"):
            body.append("</div>")
        body.append(
            '<div class="call-arrow" aria-hidden="true"><span>'
            "这些处理完成后，得到什么</span>↓</div>"
            '<div class="call-terminal call-result"><span class="call-eyebrow">'
            "这一段处理的结果</span><ol>"
            + "".join("<li>" + html.escape(v) + "</li>" for v in spec["result"])
            + "</ol></div>"
        )
        return (
            '<div class="account-flow" data-chain="'
            + html.escape(key, quote=True)
            + '">'
            + "".join(body)
            + "</div>\n"
        )

    def inline_block(match):
        path, name = match.group(1).split(":", 1)
        key = path + "::" + name
        if key not in inline:
            raise ValueError(f"Missing inline explanation: {key}")
        return render_node(inline[key])

    chapters = []
    templates = sorted(
        (BOOK / "content").glob("*.md.in"), key=lambda p: (p.name != "README.md.in", p.name)
    )
    for template in templates:
        raw = template.read_text(encoding="utf-8")
        chapter_number = re.match(r"(\d{2})-", template.name)
        if chapter_number:
            raw = re.sub(r"\A# ", f"# {chapter_number[1]}　", raw, count=1)
        if re.match(r"\d\d-", template.name) and template.name[:2] not in {"00", "19"}:
            missing = [word for word in REQUIRED if word not in raw]
            if missing:
                raise ValueError(f"{template.name}: missing chapter sections {missing}")
        text = re.sub(r"\{\{chain:([^}]+)\}\}", chain_block, raw)
        text = re.sub(r"\{\{inline:([^}]+)\}\}", inline_block, text)
        if re.search(r"\{\{(?:chain|inline):[^}]+\}\}", text):
            raise ValueError(f"Unexpanded documentation directive in {template.name}")
        text = text.rstrip() + "\n"
        name = template.name.removesuffix(".in")
        for field, value in re.findall(r"source\.html#(symbol|file)=([^\s)]+)", text):
            from urllib.parse import unquote

            target = unquote(value)
            if target not in (symbols if field == "symbol" else by_file):
                reference_errors.append(f"{name}: missing source link {field}={target}")
        for target in re.findall(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", text):
            if not (BOOK / "content" / (target + ".in")).exists() and not (BOOK / target).exists():
                reference_errors.append(f"{name}: missing chapter {target}")
        chapters.append({"file": name, "text": text})
    if reference_errors:
        raise ValueError("\n".join(reference_errors))
    if set(chains) != used_chains:
        raise ValueError(f"Unused chain specifications: {sorted(set(chains) - used_chains)}")
    counts["chapters"] = len(chapters)
    counts["explained_symbols"] = len(used_symbols)
    source_commit = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", "src", "evals", "scripts"],
        cwd=ROOT,
        text=True,
    ).strip()
    hashes = {file["path"]: file["sha256"] for file in files}
    manifest = {
        "source_commit": source_commit,
        "source_identity_scope": (
            "indexed maintained Python sources; no environment, state, archive or runtime artifacts"
        ),
        "source_sha256": digest(canonical(hashes).encode()),
        "coverage": counts,
        "source_hashes": hashes,
        "module_chapters": {
            name: [c["file"] for c in chapters if c["file"].startswith(prefix)]
            for name, prefix in [
                ("app", "01-"),
                ("kernel", "02-"),
                ("models", "03-"),
                ("context", "04-"),
                ("agent", "05-"),
                ("tools", "06-"),
                ("sandbox", "07-"),
                ("snapshot", "08-"),
                ("skills", "09-"),
                ("mcp", "10-"),
                ("hooks", "11-"),
                ("session", "12-"),
                ("memory", "13-"),
                ("plans", "14-"),
                ("subagents", "15-"),
                ("ui", "16-"),
            ]
        },
    }
    if any(len(pages) != 1 for pages in manifest["module_chapters"].values()):
        raise ValueError("Every maintained feature must have exactly one primary chapter")
    payload = {
        "chapters": chapters,
        "files": files,
        "head": source_commit,
        "counts": counts,
        "repository": REPOSITORY,
        "explanations": explanations,
    }
    inputs = sorted(
        [
            *(BOOK / "content").glob("*.md.in"),
            *(BOOK / "content").glob("*.json"),
            *(
                p
                for p in (BOOK / "tools").iterdir()
                if p.suffix in {".py", ".cjs", ".js", ".css", ".json"}
                and p.name != "package-lock.json"
            ),
        ]
    )
    manifest["generation_inputs_sha256"] = digest(
        canonical({p.relative_to(BOOK).as_posix(): digest(p.read_bytes()) for p in inputs}).encode()
    )
    if check:
        manifest["generated_hashes"] = {
            p.name: digest(p.read_bytes())
            for p in [
                *(BOOK / c["file"] for c in chapters),
                *(BOOK / name for name in ["index.html", "source.html", "run.html"]),
            ]
        }
        for chapter in chapters:
            if (
                not (BOOK / chapter["file"]).exists()
                or (BOOK / chapter["file"]).read_text(encoding="utf-8") != chapter["text"]
            ):
                raise ValueError(f"Stale generated chapter: {chapter['file']}; rebuild the book")
        current = json.loads((BOOK / "coverage.json").read_text(encoding="utf-8"))
        if current != manifest:
            raise ValueError("Source/coverage changed; rebuild the book")
    else:
        for chapter in chapters:
            (BOOK / chapter["file"]).write_text(chapter["text"], encoding="utf-8")
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False)
            temporary = Path(stream.name)
        try:
            subprocess.run(
                [
                    os.environ.get("NODE_BINARY", "node"),
                    str(BOOK / "tools/render.cjs"),
                    str(temporary),
                    str(BOOK),
                ],
                check=True,
            )
        finally:
            temporary.unlink(missing_ok=True)
        manifest["generated_hashes"] = {
            p.name: digest(p.read_bytes())
            for p in [
                *(BOOK / c["file"] for c in chapters),
                *(BOOK / name for name in ["index.html", "source.html", "run.html"]),
            ]
        }
        (BOOK / "coverage.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"status": "checked" if check else "built", **counts}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate source references and generated Markdown without writes",
    )
    build(parser.parse_args().check)
