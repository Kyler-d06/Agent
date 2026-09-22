import json
from pathlib import Path

import pytest

import public_repo_export as public


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
