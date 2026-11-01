import asyncio
import json
from pathlib import Path

import pytest

from pycode.kernel import ToolError
from pycode.skills.catalog import FileSkillCatalog, parse_skill

VECTORS = json.loads(
    (Path(__file__).parents[2] / "fixtures/skill-baselines.json").read_text(encoding="utf-8")
)


def test_source_parser_vectors():
    skill = parse_skill(VECTORS["valid"], "/skills/test/SKILL.md", "user")
    assert skill.entry.name == "test-skill" and skill.entry.allowed_tools == ("Bash", "Read")
    for raw in VECTORS["invalid"]:
        with pytest.raises(ToolError):
            parse_skill(raw, "/skills/test/SKILL.md", "user")


def test_frontmatter_problems_warn_like_pi_and_only_a_missing_description_skips():
    upper = parse_skill("---\nname: UPPER\ndescription: x\n---\nbody", "/s/x/SKILL.md", "user")
    assert upper.entry.name == "UPPER" and upper.warnings == (
        "name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)",
    )
    long = parse_skill(f"---\nname: ok\ndescription: {'d' * 1025}\n---\n", "/s/ok/SKILL.md", "u")
    assert long.warnings == ("description exceeds 1024 characters (1025)",)
    unnamed = parse_skill("---\ndescription: x\n---\n", "/s/from-dir/SKILL.md", "user")
    assert unnamed.entry.name == "from-dir" and not unnamed.warnings
    with pytest.raises(ToolError):
        parse_skill("---\nname: ok\n---\nbody", "/s/ok/SKILL.md", "user")
    hidden = parse_skill(
        "---\nname: ok\ndescription: x\ndisable-model-invocation: true\n---\n",
        "/s/ok/SKILL.md",
        "user",
    )
    assert hidden.entry.disable_model_invocation


async def test_saving_a_skill_rejects_names_that_only_warn_on_load(tmp_path):
    catalog = FileSkillCatalog((("user", tmp_path / "skills"),))
    with pytest.raises(ToolError):
        await catalog.write("UPPER", "---\nname: UPPER\ndescription: x\n---\nbody")
    assert not (tmp_path / "skills" / "UPPER").exists()


async def test_recursive_sources_keep_first_found_name_without_importing_scripts(tmp_path):
    roots = []
    for source in VECTORS["precedence"]:
        root = tmp_path / source
        nested = root / "nested/task"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text(
            VECTORS["valid"].replace("A test skill", source), encoding="utf-8"
        )
        (nested / "danger.py").write_text(
            "raise RuntimeError('must never import')", encoding="utf-8"
        )
        roots.append((source, root))
    catalog = FileSkillCatalog(tuple(roots))
    index = await catalog.scan()
    # Pi: the first skill found keeps the name; later ones are reported as collisions.
    assert len(index.entries) == 1 and index.entries[0].source == "builtin"
    assert [entry.source for entry in index.shadowed] == ["user", "project-agents", "project"]
    assert sum("collision" in issue for issue in index.issues) == 3
    assert (await catalog.read(index.entries[0])).entry.description == "builtin"
    await asyncio.to_thread(
        Path(index.entries[0].path).write_text,
        VECTORS["valid"].replace("A test skill", "changed"),
        encoding="utf-8",
    )
    with pytest.raises(ToolError) as error:
        await catalog.read(index.entries[0])
    assert error.value.code == "skill_changed"
