import json
import subprocess

import yaml

import dsh_integration


def test_managed_patch_pins_ollama_model_context_and_omits_default_effort(tmp_path, monkeypatch):
    source = tmp_path / "ollama-settings.yaml"
    source.write_text(yaml.safe_dump({
        "agent-default-model": {"provider": "deepseek-official", "model": "qwen3.5:4b"},
        "llm-pi-ai": {"providers": {"ollama": {"models": [
            {"id": "qwen3.5:4b", "contextWindow": 262144, "input": ["text"]},
        ]}}},
    }), encoding="utf-8")
    provider_file = tmp_path / "providers.json"
    provider_file.write_text(json.dumps({"providers": [{
        "enabled": True, "type": "api", "cost_class": "local", "model": "qwen3.5:4b",
    }]}), encoding="utf-8")
    discovered = {
        "available": True, "settings": str(source), "provider": "ollama",
        "model": "qwen3.5:4b", "context_window": 262144,
        "runtime_patch": None, "ollama_patch": str(tmp_path / "ollama.cordis.yml"),
        "node": "node", "bin": "dsh", "version": "test", "settings_error": None,
    }
    monkeypatch.setattr(dsh_integration, "discover_dsh", lambda: discovered)

    result = dsh_integration.prepare_runtime_patch(
        tmp_path / "private", provider_file,
        {"dsh": {"model": "qwen3.5:4b", "context_window": 8192,
                 "reasoning_effort": "default"}},
    )

    managed = yaml.safe_load((tmp_path / "private" / "dsh" / "settings.yaml").read_text(encoding="utf-8"))
    selection = managed["agent-default-model"]
    assert selection == {"provider": "ollama", "model": "qwen3.5:4b"}
    assert managed["llm-pi-ai"]["providers"]["ollama"]["models"][0]["contextWindow"] == 8192
    assert result["reasoning_effort"] == "default"


def test_dirty_checkout_is_snapshotted_into_clean_isolated_repo(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
    (source / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", "baseline"], cwd=source, check=True, capture_output=True)
    (source / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (source / "new.py").write_text("new = True\n", encoding="utf-8")

    copied = dsh_integration.isolated_clean_checkout(source)

    destination = dsh_integration.Path(copied["workspace"])
    assert copied["isolated"] and copied["source_workspace"] == str(source.resolve())
    assert not copied["dirty"] and (destination / "tracked.py").read_text() == "value = 2\n"
    assert (destination / "new.py").is_file()
