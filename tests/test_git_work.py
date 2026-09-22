import subprocess


def git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_isolated_worktree_and_applicable_patch(core):
    work = core.universal_platform.work
    root = work.root
    git(root, "init")
    (root / "code.py").write_text("x = 1\n")
    git(root, "add", "code.py")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Initial fixture")
    result = work.worktree_create(branch="codex/test-change")
    location = result["path"]
    before = work.workspace_read(location + "/code.py")
    work.workspace_write(location + "/code.py", "x = 2\n", before["sha256"])
    work.workspace_write(location + "/new.txt", "a new file")
    assert (root / "code.py").read_text() == "x = 1\n"
    artifact = work.export_patch("changes.patch", location)
    patch = root / artifact["path"]
    git(root, "apply", "--check", str(patch))
    assert work.verify_artifact(artifact["id"])["verified"]
