import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

import pycode.skills.install as installation
from pycode.kernel import ToolError
from pycode.skills.install import GitHubSkillInstaller, parse_skill_url

VECTORS = json.loads(
    (Path(__file__).parents[2] / "fixtures/skill-install-baselines.json").read_text(
        encoding="utf-8"
    )
)
MANIFEST = b"---\nname: vendor-sample\ndescription: sample skill\n---\nSample SOP"
COMMIT = "a" * 40
URL = VECTORS["valid"][1]


def file_entry(name, content, directory="skills/sample"):
    return {
        "name": name,
        "path": directory + "/" + name if directory else name,
        "type": "file",
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode(),
    }


def download_transport(*, manifest=MANIFEST, extra=(), requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": COMMIT})
        if "/contents/" not in request.url.path:
            return httpx.Response(200, json={"default_branch": "default"})
        assert request.url.params["ref"] == COMMIT
        if request.url.path.endswith("references"):
            return httpx.Response(
                200,
                json=[
                    file_entry(
                        "never.py",
                        b"raise RuntimeError('never imported')",
                        "skills/sample/references",
                    )
                ],
            )
        return httpx.Response(
            200,
            json=[
                file_entry("SKILL.md", manifest),
                {"name": "references", "path": "skills/sample/references", "type": "dir"},
                *extra,
            ],
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("url", VECTORS["valid"])
def test_source_url_vectors(url):
    source = parse_skill_url(url)
    assert (source.owner, source.repo, source.ref, source.subpath, source.directory_name) == (
        "owner",
        "repo",
        "main",
        "skills/sample",
        "sample",
    )


@pytest.mark.parametrize(
    "url",
    VECTORS["invalid"]
    + [
        "https://user:secret@github.com/owner/repo",
        "https://github.com/owner/repo?secret=1",
        "https://github.com/owner/repo/tree/main/CON",
        "https://github.com/owner/repo/tree/main/C:%5cfoo",
        "https://github.com/owner/.git",
        "https://github.com/owner/.hidden",
    ],
)
def test_source_invalid_url_vectors(url):
    with pytest.raises(ToolError):
        parse_skill_url(url)


def test_root_and_ref_path_overrides_are_unambiguous():
    source = parse_skill_url("https://github.com/owner/repo.git")
    assert source.subpath == "" and source.directory_name == "repo" and source.ref is None
    source = parse_skill_url(
        "https://github.com/owner/repo", ref="feature/task", subpath="skills/sample"
    )
    assert source.ref == "feature/task" and source.directory_name == "sample"


async def test_full_install_pins_commit_replaces_previous_and_never_imports_python(tmp_path):
    requests = []
    old = tmp_path / "sample"
    old.mkdir()
    (old / "notes.md").write_text("user notes", encoding="utf-8")
    installer = GitHubSkillInstaller(tmp_path, transport=download_transport(requests=requests))
    report = await installer.install(URL)
    assert report["skill_name"] == "vendor-sample" and Path(report["target_dir"]) == old
    assert report["commit"] == COMMIT and report["file_count"] == 2
    assert (old / "SKILL.md").read_bytes() == MANIFEST
    assert not (old / "notes.md").exists()
    assert not list(tmp_path.glob(".previous-*"))
    assert (old / "references/never.py").is_file()
    assert json.loads((old / ".pycode-source.json").read_text())["files"] == report["files"]
    assert not list(tmp_path.glob(".install-*"))
    assert requests[0].url.path.endswith("/commits/main")
    assert all(request.headers["User-Agent"].startswith("pycode/") for request in requests)


@pytest.mark.parametrize(
    "manifest,extra,error",
    [
        (b"no frontmatter", (), "invalid_skill"),
        (MANIFEST, (file_entry("../escape", b"bad"),), "invalid_skill_source"),
        (MANIFEST, (file_entry("large", b"x" * ((1 << 20) + 1)),), "skill_download_limit"),
        (MANIFEST, (file_entry("SKILL.md", b"duplicate"),), "invalid_skill_download"),
    ],
)
async def test_invalid_download_preserves_old_and_cleans_staging(tmp_path, manifest, extra, error):
    old = tmp_path / "sample"
    old.mkdir()
    (old / "notes.md").write_text("keep", encoding="utf-8")
    installer = GitHubSkillInstaller(
        tmp_path, transport=download_transport(manifest=manifest, extra=extra)
    )
    with pytest.raises(ToolError) as caught:
        await installer.install(URL)
    assert caught.value.code == error
    assert (old / "notes.md").read_text() == "keep" and not list(tmp_path.glob(".install-*"))
    assert not list(tmp_path.glob(".previous-*"))


async def test_failed_publication_restores_previous_directory(tmp_path, monkeypatch):
    old = tmp_path / "sample"
    old.mkdir()
    (old / "notes.md").write_text("keep", encoding="utf-8")
    replace_file = installation.os.replace

    def fail_once(source, target):
        if Path(source).name.startswith(".install-"):
            raise OSError("publish failed")
        replace_file(source, target)

    monkeypatch.setattr(installation.os, "replace", fail_once)
    installer = GitHubSkillInstaller(tmp_path, transport=download_transport())
    with pytest.raises(ToolError) as error:
        await installer.install(URL)
    assert error.value.code == "skill_install_io_error"
    assert (old / "notes.md").read_text() == "keep" and not list(tmp_path.glob(".install-*"))
    assert not list(tmp_path.glob(".previous-*"))


async def test_cancel_collects_download_and_removes_unpublished_staging(tmp_path):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    installer = GitHubSkillInstaller(tmp_path, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(installer.install(URL))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.glob(".install-*")) and not (tmp_path / "sample").exists()


async def test_repository_root_uses_default_branch_then_immutable_contents(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": COMMIT})
        if "/contents/" in request.url.path:
            assert request.url.params["ref"] == COMMIT
            return httpx.Response(200, json=[file_entry("SKILL.md", MANIFEST, "")])
        return httpx.Response(200, json={"default_branch": "trunk"})

    installer = GitHubSkillInstaller(tmp_path, transport=httpx.MockTransport(handler))
    report = await installer.install("https://github.com/owner/repo")
    assert requests[1].url.path.endswith("/commits/trunk")
    assert report["skill_name"] == "vendor-sample" and Path(report["target_dir"]).name == "repo"


@pytest.mark.parametrize("valid", [True, False])
async def test_raw_fallback_stays_under_resolved_commit_and_approved_host(tmp_path, valid):
    requested_raw = []
    raw_url = f"https://raw.githubusercontent.com/owner/repo/{COMMIT}/skills/sample/SKILL.md"
    if not valid:
        raw_url = raw_url.replace("raw.githubusercontent.com", "evil.invalid")

    def handler(request):
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": COMMIT})
        if "/contents/" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "SKILL.md",
                        "path": "skills/sample/SKILL.md",
                        "type": "file",
                        "size": len(MANIFEST),
                        "download_url": raw_url,
                    }
                ],
            )
        requested_raw.append(request)
        return httpx.Response(200, content=MANIFEST)

    installer = GitHubSkillInstaller(tmp_path, transport=httpx.MockTransport(handler))
    if valid:
        assert (await installer.install(URL))["file_count"] == 1 and len(requested_raw) == 1
    else:
        with pytest.raises(ToolError) as error:
            await installer.install(URL)
        assert error.value.code == "invalid_skill_download" and not requested_raw
        assert not (tmp_path / "sample").exists() and not list(tmp_path.glob(".install-*"))


async def test_actual_bytes_cannot_bypass_declared_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(installation, "MAX_FILE_SIZE", len(MANIFEST) + 10)
    entry = file_entry("large", b"x" * (len(MANIFEST) + 20))
    entry["size"] = 1
    installer = GitHubSkillInstaller(tmp_path, transport=download_transport(extra=(entry,)))
    with pytest.raises(ToolError) as error:
        await installer.install(URL)
    assert error.value.code == "skill_download_limit" and not (tmp_path / "sample").exists()
