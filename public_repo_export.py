"""Build a fail-closed, secret-scanned snapshot for the public GitHub mirror."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
from pathlib import Path


PUBLIC_TOP_LEVEL = (
    ".gitattributes",
    ".gitignore",
    "*.cmd",
    "*.py",
    "requirements*.txt",
    "pytest.ini",
    "ANDROID_MESH.md",
    "CAPABILITIES_COSTS_REQUIREMENTS.md",
    "FREE_FIRST_OPERATIONS.md",
    "LICENSE",
    "MCP_SETUP.md",
    "README.md",
    "SECURITY.md",
    "TRADING_RESEARCH.md",
)
PUBLIC_DIRECTORIES = {"capabilities", "evaluations", "examples", "tests"}
PUBLIC_SUFFIXES = {".cmd", ".ini", ".json", ".md", ".py", ".txt", ".yaml", ".yml"}
PRIVATE_NAMES = {"OPERATOR_CONTEXT.md", "PROJECT_HANDOFF.md", "PROJECT_MEMORY.md"}
MAX_FILE_BYTES = 2_000_000
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "OpenAI/Anthropic key": re.compile(rb"(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,})"),
    "Slack token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "Telegram bot token": re.compile(rb"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "Windows user path": re.compile(rb"[A-Za-z]:\\Users\\[^\\\r\n\"']+", re.IGNORECASE),
}


def _data_dir() -> Path:
    configured = os.environ.get("ASSISTANT_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "UniversalAssistant").resolve()
    return (Path.home() / ".local" / "share" / "universal-assistant").resolve()


def _managed_secret_values() -> list[bytes]:
    path = _data_dir() / "managed-secrets.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    values = []
    for record in records.values() if isinstance(records, dict) else []:
        value = record.get("value") if isinstance(record, dict) else record
        if isinstance(value, str) and len(value) >= 8:
            values.append(value.encode("utf-8"))
    return values


def public_files(root: Path) -> list[Path]:
    selected = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if relative.name in PRIVATE_NAMES or any(part.startswith(".") for part in relative.parts[:-1]):
            continue
        if len(relative.parts) == 1:
            allowed = any(fnmatch.fnmatch(relative.name, pattern) for pattern in PUBLIC_TOP_LEVEL)
        else:
            allowed = relative.parts[0] in PUBLIC_DIRECTORIES and path.suffix.lower() in PUBLIC_SUFFIXES
        if allowed:
            selected.append(relative)
    return sorted(selected, key=lambda item: item.as_posix())


def validate(root: Path, files: list[Path]) -> dict[str, str]:
    managed = _managed_secret_values()
    hashes = {}
    failures = []
    for relative in files:
        data = (root / relative).read_bytes()
        if len(data) > MAX_FILE_BYTES:
            failures.append(f"{relative.as_posix()}: exceeds {MAX_FILE_BYTES} bytes")
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                failures.append(f"{relative.as_posix()}: matched {label}")
        if any(value in data for value in managed):
            failures.append(f"{relative.as_posix()}: contains a configured managed-secret value")
        hashes[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    if failures:
        raise ValueError("public export refused:\n- " + "\n- ".join(sorted(set(failures))))
    return hashes


def export(root: Path, output: Path) -> dict:
    root, output = root.resolve(), output.resolve()
    if output == root or root in output.parents:
        raise ValueError("output must be outside the source tree")
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be absent or empty")
    files = public_files(root)
    hashes = validate(root, files)
    output.mkdir(parents=True, exist_ok=True)
    for relative in files:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
    manifest = {"schema": 1, "files": hashes}
    (output / "PUBLIC_EXPORT_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"output": str(output), "files": len(files), "manifest": manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = export(Path(args.root), Path(args.output))
    print(json.dumps({"output": result["output"], "files": result["files"]}, indent=2))


if __name__ == "__main__":
    main()
