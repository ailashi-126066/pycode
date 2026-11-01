import pytest
from scripts.check_architecture import check_branding


def test_no_product_brand_residue():
    assert check_branding() == []


def test_residue_in_generated_configuration_is_detected(tmp_path):
    output = tmp_path / "config.json"
    output.write_text('{"name":"' + "MEW" + 'CODE"}', encoding="utf-8")
    assert len(check_branding(tmp_path)) == 1


def test_source_attribution_and_license_are_allowed(tmp_path):
    source_map = tmp_path / "docs" / "source-map.md"
    source_map.parent.mkdir()
    source_map.write_text("mew" + "code source mapping", encoding="utf-8")
    source = tmp_path / "source.py"
    source.write_text("# Source: " + "mew" + "code local SHA-256 baseline\n", encoding="utf-8")
    assert check_branding(tmp_path) == []


@pytest.mark.parametrize(
    "relative",
    [
        ".worktrees/s1-scripts/README.md",
        "tests/.runtime-tests/venv/notes.txt",
        "evals/cache/polyglot/README.md",
        "evals/work/RUN/solution.py",
        "evals/results/RUN/summary.json",
        "tests/evals/cache/polyglot/README.md",
    ],
)
def test_worktrees_and_generated_data_are_not_scanned(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("Product: " + "mew" + "code", encoding="utf-8")
    assert check_branding(tmp_path) == []


@pytest.mark.parametrize(
    "relative",
    [
        "src/pycode/app/cli/main.py",
        "tests/app/test_cli.py",
        "scripts/verify_release.py",
        "evals/coding/runner.py",
        "evals/data/manifest.json",
        "docs/architecture.md",
        "archived/README.md",
    ],
)
def test_maintained_directories_are_scanned(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("Product: " + "mew" + "code", encoding="utf-8")
    assert check_branding(tmp_path) == [relative + ":1: obsolete product brand"]
