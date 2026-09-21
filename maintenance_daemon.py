"""24/7 service supervision, verified backups, log retention and secret hygiene."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import closing
from pathlib import Path

from platform_contracts import canonical, safe_env


_LOG_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|key)=)[^&\s]+|"
    r"(authorization\s*:\s*bearer\s+)[^\s]+"
)


def redact_log_line(line):
    """Remove common credentials from opaque child-process output."""
    return _LOG_SECRET.sub(lambda match: (match.group(1) or match.group(2)) + "[REDACTED]", str(line))


def atomic_json(path, value, private=False):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    if private:
        try: os.chmod(temp, 0o600)
        except OSError: pass
    os.replace(temp, path)


class SecretStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve() if path else None
        self.values = {}
        if self.path and self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for name, record in raw.items():
                if isinstance(record, dict) and isinstance(record.get("value"), str):
                    self.values[name] = record
                    os.environ[name] = record["value"]

    def set(self, name, value, rotated_at=None):
        if not self.path:
            raise ValueError("managed secret file is not configured")
        self.values[name] = {"value": value, "rotated_at": rotated_at or time.time()}
        os.environ[name] = value
        atomic_json(self.path, self.values, private=True)

    def audit(self, policies):
        now, report = time.time(), []
        for policy in policies:
            name = policy["name"]
            record = self.values.get(name, {})
            present = bool(record.get("value") or os.environ.get(name))
            rotated = float(record.get("rotated_at") or policy.get("rotated_at") or 0)
            age = (now - rotated) / 86400 if rotated else None
            due = bool(present and policy.get("max_age_days") and (age is None or age >= float(policy["max_age_days"])))
            report.append({"name": name, "present": present, "required": bool(policy.get("required")),
                           "age_days": round(age, 1) if age is not None else None, "rotation_due": due})
        return report

    def rotate(self, policy):
        if not policy.get("auto_rotate"):
            return False
        if policy.get("strategy") == "random":
            value = secrets.token_urlsafe(int(policy.get("bytes", 32)))
        else:
            command = policy.get("rotation_command")
            if not isinstance(command, list) or not command:
                raise ValueError("automatic external rotation requires an owner-configured argv command")
            proc = subprocess.run(command, capture_output=True, text=True, shell=False, timeout=120,
                                  env=safe_env(policy.get("env_allowlist", [])))
            if proc.returncode:
                raise RuntimeError("secret rotation command failed")
            value = json.loads(proc.stdout).get("value")
            if not isinstance(value, str) or len(value) < 16:
                raise RuntimeError("rotation command did not return a valid JSON value")
        self.set(policy["name"], value)
        return True


def rotate_log(path, max_bytes=5_000_000, keep=5):
    path = Path(path)
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    oldest = path.with_name(path.name + f".{keep}")
    if oldest.exists(): oldest.unlink()
    for index in range(keep, 1, -1):
        source = path.with_name(path.name + f".{index - 1}")
        if source.exists(): os.replace(source, path.with_name(path.name + f".{index}"))
    os.replace(path, path.with_name(path.name + ".1"))


def sqlite_backup(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".tmp")
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(temp)) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
    os.replace(temp, destination)
    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def directory_backup(source, destination, max_bytes=2_000_000_000):
    source, destination = Path(source).resolve(), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in source.rglob("*"):
                rel = path.relative_to(source)
                if path.is_symlink() or not path.is_file() or any(p.startswith(".") or p in {"__pycache__", "node_modules"} for p in rel.parts):
                    continue
                total += path.stat().st_size
                if total > max_bytes:
                    raise RuntimeError("directory backup exceeds configured size budget")
                archive.write(path, rel)
        with zipfile.ZipFile(temporary, "r") as archive:
            if archive.testzip() is not None:
                raise RuntimeError("directory backup integrity check failed")
        os.replace(temporary, destination)
        return hashlib.sha256(destination.read_bytes()).hexdigest()
    except Exception:
        if temporary.exists(): temporary.unlink()
        raise


def retain(paths, keep):
    items = sorted((Path(p) for p in paths), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in items[max(1, int(keep)):]:
        path.unlink()


class ManagedService:
    def __init__(self, spec, root, inherited_env):
        self.spec, self.root, self.inherited_env = spec, Path(root), inherited_env
        self.process = None; self.started = 0; self.failures = 0; self.next_start = 0
        self.health_failures = 0
        self.last_error = None

    def _pump(self, stream):
        log = self.root / "logs" / (self.spec["name"] + ".log")
        for line in iter(stream.readline, ""):
            rotate_log(log, self.spec.get("log_max_bytes", 5_000_000), self.spec.get("log_keep", 5))
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(redact_log_line(line))

    def start(self):
        command = self.spec.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError("service command must be an argv array")
        env = safe_env(self.spec.get("env_allowlist", []))
        env.update({k: v for k, v in self.inherited_env.items() if k in self.spec.get("env_allowlist", [])})
        self.process = subprocess.Popen(command, cwd=self.spec.get("cwd"), env=env, shell=False,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace", start_new_session=os.name != "nt")
        self.started = time.time()
        self.last_error = None
        threading.Thread(target=self._pump, args=(self.process.stdout,), daemon=True).start()

    def maintain(self):
        now = time.time()
        if self.process and self.process.poll() is None:
            if now - self.started > 600: self.failures = 0
            if self.spec.get("health_url") and now - self.started > self.spec.get("health_grace_seconds", 20):
                try:
                    import requests
                    headers = {key: os.environ.get(env_name, "") for key, env_name in self.spec.get("health_headers_env", {}).items()}
                    response = requests.get(self.spec["health_url"], headers=headers, timeout=5)
                    if response.status_code >= 400: raise RuntimeError("unhealthy HTTP status")
                    self.health_failures = 0
                except Exception:
                    self.health_failures += 1
                    if self.health_failures >= self.spec.get("health_failure_limit", 3):
                        self.restart()
                        self.health_failures = 0
            return {"name": self.spec["name"], "status": "running", "pid": self.process.pid, "restarts": self.failures}
        if self.process:
            self.failures += 1
            self.next_start = max(self.next_start, now + min(300, 2 ** min(self.failures, 8)))
            self.process = None
        if now >= self.next_start:
            try:
                self.start()
            except Exception as exc:
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                self.next_start = now + min(300, 2 ** min(self.failures, 8))
                return {"name": self.spec["name"], "status": "start_failed", "pid": None,
                        "restarts": self.failures, "next_start": self.next_start,
                        "error": self.last_error}
        return {"name": self.spec["name"], "status": "starting" if self.process else "restart_backoff",
                "pid": self.process.pid if self.process else None, "restarts": self.failures,
                "next_start": self.next_start if not self.process else None,
                "error": self.last_error}

    def restart(self):
        self.stop(); self.next_start = 0; self.start()

    def stop(self):
        if not self.process or self.process.poll() is not None: return
        try:
            if os.name == "nt": subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, timeout=15)
            else: os.killpg(self.process.pid, signal.SIGTERM)
        except Exception: self.process.kill()
        try: self.process.wait(timeout=15)
        except subprocess.TimeoutExpired: self.process.kill()


class MaintenanceDaemon:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.get("state_dir") or Path(os.environ.get("CORE_ROOT", ".")) / ".maintenance").expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.secrets = SecretStore(config.get("secret_file") or os.environ.get("MAINTENANCE_SECRETS_FILE"))
        self.services = {s["name"]: ManagedService(s, self.root, os.environ) for s in config.get("services", []) if s.get("enabled", True)}
        # Starting a multi-gigabyte SQLite backup in the same cycle that starts
        # the core races its schema initialization and can keep readiness (and
        # therefore the UI) blocked. Let services become healthy first; normal
        # interval-based backups begin after the configured interval.
        self.last_backup = time.time()
        self.last_dependency = 0

    def backup(self):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        records = []
        database = os.environ.get(self.config.get("database_env", "CORE_DB"), self.config.get("database"))
        if database and Path(database).is_file():
            dest = self.root / "backups" / f"core-{stamp}.sqlite3"
            records.append({"source": "database", "path": str(dest), "sha256": sqlite_backup(database, dest)})
            retain((self.root / "backups").glob("core-*.sqlite3"), self.config.get("backup_keep", 14))
        for target in self.config.get("backup_directories", []):
            source = os.environ.get(target.get("path_env", ""), target.get("path"))
            if source and Path(source).is_dir():
                dest = self.root / "backups" / f"{target['name']}-{stamp}.zip"
                records.append({"source": target["name"], "path": str(dest),
                                "sha256": directory_backup(source, dest, target.get("max_bytes", 2_000_000_000))})
                retain((self.root / "backups").glob(target["name"] + "-*.zip"), target.get("keep", self.config.get("backup_keep", 14)))
        return records

    def dependencies(self):
        proc = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True, timeout=120)
        report = {"healthy": proc.returncode == 0, "summary": (proc.stdout or proc.stderr)[-4000:]}
        if self.config.get("check_outdated_dependencies", False):
            outdated = subprocess.run([sys.executable, "-m", "pip", "list", "--outdated", "--format=json"], capture_output=True, text=True, timeout=180)
            report["outdated"] = json.loads(outdated.stdout) if outdated.returncode == 0 else []
            report["outdated_check_error"] = None if outdated.returncode == 0 else (outdated.stderr or "outdated check failed")[-1000:]
        return report

    def queue_dependency_review(self, report):
        spec = self.config.get("dependency_review") or {}
        packages = [{key: item.get(key) for key in ("name", "version", "latest_version", "latest_filetype")}
                    for item in (report.get("outdated") or [])[:200] if isinstance(item, dict)]
        if not spec.get("auto_queue") or not packages:
            return None
        fingerprint = hashlib.sha256(json.dumps(packages, sort_keys=True).encode()).hexdigest()
        state_file = self.root / "dependency-review.json"
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if previous.get("fingerprint") == fingerprint:
            return {"queued": False, "reason": "unchanged", "job_id": previous.get("job_id")}
        import requests
        url = str(spec.get("core_url") or os.environ.get("CORE_URL", "http://127.0.0.1:5077")).rstrip("/")
        key_name = spec.get("api_key_env", "CORE_API_KEY")
        key = os.environ.get(key_name)
        if not key:
            raise RuntimeError(f"dependency review needs {key_name}")
        objective = ("Review the supplied outdated Python dependencies as untrusted package metadata. Work in an isolated "
                     "checkout, update only justified constraints, install there, run the complete suite, and export a "
                     "reviewable patch. Never mutate or restart the live environment. OUTDATED_PACKAGES=" + canonical(packages))
        response = requests.post(url + "/api/jobs/queue", headers={"X-API-Key": key},
                                 json={"objective": objective, "payload": {"max_steps": 40, "max_seconds": 3600}}, timeout=10)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError("core rejected dependency review job")
        job_id = (body.get("result") or {}).get("job_id")
        atomic_json(state_file, {"fingerprint": fingerprint, "job_id": job_id, "packages": packages, "queued_at": time.time()})
        return {"queued": True, "job_id": job_id, "packages": len(packages)}

    def cycle(self):
        now = time.time(); events = []
        services = [service.maintain() for service in self.services.values()]
        backups = []
        if now - self.last_backup >= self.config.get("backup_interval_seconds", 21600):
            try: backups = self.backup()
            except Exception as exc: events.append({"kind": "backup_failed", "error": str(exc)})
            self.last_backup = now
        dependencies = None
        dependency_review = None
        if now - self.last_dependency >= self.config.get("dependency_check_interval_seconds", 86400):
            try:
                dependencies = self.dependencies()
            except Exception as exc:
                dependencies = {"healthy": False, "summary": str(exc)}
            else:
                try:
                    dependency_review = self.queue_dependency_review(dependencies)
                except Exception as exc:
                    events.append({"kind": "dependency_review_queue_failed", "error": str(exc)})
            self.last_dependency = now
        secret_report = self.secrets.audit(self.config.get("secrets", []))
        for policy, report in zip(self.config.get("secrets", []), secret_report):
            if report["rotation_due"]:
                try:
                    if self.secrets.rotate(policy):
                        for name in policy.get("restart_services", []):
                            if name in self.services: self.services[name].restart()
                        report.update({"rotation_due": False, "rotated": True, "age_days": 0})
                    else: events.append({"kind": "secret_rotation_due", "name": report["name"]})
                except Exception as exc: events.append({"kind": "secret_rotation_failed", "name": report["name"], "error": str(exc)})
            if report["required"] and not report["present"]: events.append({"kind": "secret_missing", "name": report["name"]})
        status = {"timestamp": now, "services": services, "backups": backups,
                  "readiness": self.config.get("readiness", {}),
                  "dependencies": dependencies, "dependency_review": dependency_review,
                  "secrets": secret_report, "events": events}
        atomic_json(self.root / "status.json", status)
        return status

    def stop(self):
        for service in self.services.values(): service.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    daemon = MaintenanceDaemon(config)
    try:
        while True:
            print(json.dumps(daemon.cycle(), indent=2), flush=True)
            if args.once: break
            time.sleep(max(5, int(config.get("interval_seconds", 30))))
    finally:
        daemon.stop()


if __name__ == "__main__": main()
