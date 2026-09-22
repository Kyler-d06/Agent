import json
import subprocess
from pathlib import Path

import pytest

import public_repo_export as public
from work_tools import WorkTools


def test_export_is_allowlisted_and_manifested(tmp_path, monkeypatch):
    root, output = tmp_path / "source", tmp_path / "public"
    (root / "tests").mkdir(parents=True)
    (root / "trading_data").mkdir()
    (root / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (root / "README.md").write_text("safe\n", encoding="utf-8")
    (root / "OPERATOR_CONTEXT.md").write_text("private\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (root / "trading_data" / "account.json").write_text('{"cash": 10}\n', encoding="utf-8")
    monkeypatch.setattr(public, "_managed_secret_values", lambda: [])

    result = public.export(root, output)

    assert result["files"] == 3
    assert (output / "app.py").is_file()
    assert (output / "tests" / "test_app.py").is_file()
    assert not (output / "OPERATOR_CONTEXT.md").exists()
    assert not (output / "trading_data").exists()
    manifest = json.loads((output / "PUBLIC_EXPORT_MANIFEST.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"README.md", "app.py", "tests/test_app.py"}


@pytest.mark.parametrize(
    "parts",
    [
        ("-----BEGIN ", "PRIVATE KEY-----\nvalue"),
        ("github", "_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"),
        ("C:", r"\Users\someone\private\file.txt"),
    ],
)
def test_export_refuses_sensitive_content(tmp_path, monkeypatch, parts):
    root, output = tmp_path / "source", tmp_path / "public"
    root.mkdir()
    (root / "app.py").write_text("".join(parts), encoding="utf-8")
    monkeypatch.setattr(public, "_managed_secret_values", lambda: [])

    with pytest.raises(ValueError, match="public export refused"):
        public.export(root, output)


def test_export_refuses_actual_managed_secret(tmp_path, monkeypatch):
    root, output = tmp_path / "source", tmp_path / "public"
    root.mkdir()
    (root / "app.py").write_text('TOKEN = "locally-configured-value"', encoding="utf-8")
    monkeypatch.setattr(public, "_managed_secret_values", lambda: [b"locally-configured-value"])

    with pytest.raises(ValueError, match="managed-secret"):
        public.export(root, output)


def test_public_publisher_creates_only_a_new_review_branch(tmp_path, monkeypatch):
    seed, remote, source = tmp_path / "seed", tmp_path / "public.git", tmp_path / "source"
    seed.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=seed, check=True)
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "clone", "--bare", str(seed), str(remote)], check=True, capture_output=True)
    (source / "tests").mkdir(parents=True)
    (source / "README.md").write_text("updated\n", encoding="utf-8")
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "tests" / "test_smoke.py").write_text("def test_smoke(): assert True\n", encoding="utf-8")
    monkeypatch.setattr(public, "_managed_secret_values", lambda: [])
    tools = WorkTools(source, None)
    monkeypatch.setattr(tools, "_public_remote_url", lambda: str(remote))

    result = tools.publish_public_branch("claude/smoke", "Publish tested snapshot")

    assert result["branch"] == "claude/smoke"
    assert len(result["commit"]) == 40
    heads = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                           cwd=remote, check=True, capture_output=True, text=True).stdout.splitlines()
    assert set(heads) == {"main", "claude/smoke"}
    with pytest.raises(ValueError, match="new claude/ or codex/ branch"):
        tools.publish_public_branch("main", "Forbidden")
