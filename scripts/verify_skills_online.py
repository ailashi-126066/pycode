"""Isolated public GitHub installation acceptance; no installed resource is executed."""

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pycode.skills import FileSkillCatalog, GitHubSkillInstaller


def verify_files(report):
    root = Path(report["target_dir"])
    for relative, expected in report["files"].items():
        path = root.joinpath(*relative.split("/"))
        assert path.resolve().is_relative_to(root.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


async def verify(args):
    root = (await asyncio.to_thread(Path(__file__).resolve)).parents[1]
    storage = root / "tests/.runtime-tests/skill-online/skills"
    installer = GitHubSkillInstaller(storage)
    first = await installer.install(args.url)
    await asyncio.to_thread(verify_files, first)
    # Reinstall the exact same immutable version, independently of later upstream changes.
    second = await installer.install(args.url, ref=first["commit"])
    await asyncio.to_thread(verify_files, second)
    assert first["files"] == second["files"]
    assert second["backup_dir"] is not None
    catalog = FileSkillCatalog((("user", storage),))
    index = await catalog.scan()
    installed = next(item for item in index.entries if item.name == second["skill_name"])
    document = await catalog.read(installed)
    assert document.entry.content_hash == second["files"]["SKILL.md"]
    evidence = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "commit": second["commit"],
        "skill_name": second["skill_name"],
        "file_count": second["file_count"],
        "total_bytes": second["total_bytes"],
        "files": second["files"],
        "checks": [
            "actual HTTPS GitHub API",
            "immutable commit",
            "all resource hashes",
            "reinstall backup",
            "catalog refresh",
            "complete document on demand",
        ],
    }
    destination = root / "tests/.runtime-tests/skill-online/skill-online-final.json"
    await asyncio.to_thread(
        destination.write_text, json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: evidence[key] for key in ("skill_name", "commit", "file_count")}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    asyncio.run(verify(parser.parse_args()))


if __name__ == "__main__":
    main()
