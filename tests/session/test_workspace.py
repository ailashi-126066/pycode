from pycode.session.sessions import FileWorkspaceInspector


def test_worktree_git_file_is_recognized_without_mutation(tmp_path):
    marker = tmp_path / ".git"
    marker.write_text("gitdir: /example/worktrees/test", encoding="utf-8")
    status = FileWorkspaceInspector(tmp_path).inspect()
    assert status.snapshot_available
    assert marker.read_text(encoding="utf-8") == "gitdir: /example/worktrees/test"


def test_project_subdirectory_recognizes_parent_git_marker(tmp_path):
    (tmp_path / ".git").mkdir()
    project = tmp_path / "subdir"
    project.mkdir()
    assert FileWorkspaceInspector(project).inspect().is_git
