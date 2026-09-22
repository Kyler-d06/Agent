from pathlib import Path
import sys
import types

import pytest

from integrations import check_manifest


def test_browser_transfer_manifest_requires_declared_write_effect(tmp_path):
    manifest = {"name": "site", "type": "browser", "tools": [{"name": "download", "description": "Download file", "effect": "read",
                 "input_schema": {"type": "object"}, "steps": [{"action": "download"}]}]}
    with pytest.raises(ValueError):
        check_manifest(manifest)
    manifest["transfer_root"] = str(tmp_path)
    manifest["tools"][0]["effect"] = "external"
    assert check_manifest(manifest)


def test_browser_upload_download_confined_and_hashed(core, monkeypatch):
    registry = core.universal_platform.integrations
    root = core.universal_platform.work.root
    (root / "upload.txt").write_text("input")
    observed = {}
    class Scope:
        def __init__(self, value): self.value = value
        def __enter__(self): return self.value
        def __exit__(self, *args): pass
    class Download:
        def failure(self): return None
        def save_as(self, path): Path(path).write_text("downloaded")
    class Locator:
        def click(self): observed["clicked"] = True
        def set_input_files(self, path): observed["uploaded"] = path
        def inner_text(self): return "Complete"
    class Page:
        url = "https://example.test/"
        def set_default_timeout(self, timeout): pass
        def goto(self, *args, **kwargs): pass
        def locator(self, selector): return Locator()
        def expect_download(self): return Scope(types.SimpleNamespace(value=Download()))
    class Context:
        def new_page(self): return Page()
        def close(self): observed["closed"] = True
    class Chromium:
        def launch_persistent_context(self, *args, **kwargs):
            assert kwargs["accept_downloads"] is True
            return Context()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(sync_playwright=lambda: Scope(types.SimpleNamespace(chromium=Chromium()))))
    manifest = {"profile_dir": "dedicated-profile", "transfer_root": str(root), "url": "https://example.test/"}
    spec = {"steps": [{"action": "upload", "selector": "input", "argument": "input"}, {"action": "download", "selector": "button", "argument": "output"}],
            "result_selector": "status", "expected_text": "Complete"}
    result = registry._browser(manifest, spec, {"input": "upload.txt", "output": "downloads/result.txt"})
    assert result["verified"] and observed["closed"]
    assert len(result["downloads"][0]["sha256"]) == 64
    assert Path(observed["uploaded"]).read_text() == "input"
    with pytest.raises(ValueError):
        registry._browser(manifest, spec, {"input": "../private.txt", "output": "other.txt"})
