#!/usr/bin/env python3
"""Lightweight execution node for the agent mesh.

Designed for Windows laptops/desktops and small Linux nodes such as a Pi 3.
It exposes only configured roots over a private mesh network. Owner commands
can browse/run anything inside those roots; the AI only sees scripts whose
sidecar metadata grants visibility.

Environment:
  NODE_NAME=laptop
  NODE_KEY=shared-secret
  NODE_PORT=5080
  NODE_ROOTS_JSON='{"code":"C:\\\\Users\\\\kyler\\\\OneDrive\\\\Documents\\\\Scripts\\\\Code"}'

Optional per-script sidecar: script.py.agent.json
{
  "description": "Summarize a YouTube URL",
  "weight": "heavy",              // light | medium | heavy
  "min_ram_gb": 8,
  "requires_gpu": false,
  "agent_permission": "private",  // private | visible | confirm | autonomous
  "ideas": ["Turn a saved playlist into Obsidian notes"]
}
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request
from mobile_runtime import is_android, mobile_status
from platform_contracts import safe_env

try:
    import psutil
except ImportError:
    psutil = None

NODE_NAME = os.environ.get("NODE_NAME", platform.node() or "node")
NODE_KEY = os.environ.get("NODE_KEY", "")
NODE_PORT = int(os.environ.get("NODE_PORT", "5080"))
MAX_OUTPUT_CHARS = int(os.environ.get("NODE_MAX_OUTPUT_CHARS", "20000"))

DEFAULT_WINDOWS_ROOT = r"C:\Users\kyler\OneDrive\Documents\Scripts\Code"
if os.name == "nt" and os.path.isdir(DEFAULT_WINDOWS_ROOT):
    default_roots = {"code": DEFAULT_WINDOWS_ROOT}
else:
    default_roots = {"scripts": str(Path.home() / "scripts")}
ROOTS = json.loads(os.environ.get("NODE_ROOTS_JSON", json.dumps(default_roots)))
ROOTS = {str(k): os.path.realpath(os.path.expanduser(str(v))) for k, v in ROOTS.items()}

SENSITIVE = (".env", ".key", ".pem", "credentials", "secrets", ".ssh", ".git", ".session")
RUNNABLE = {".py", ".ps1", ".bat", ".cmd", ".sh", ".js"}
HEAVY_HINTS = ("torch", "transformers", "faster_whisper", "whisper", "tensorflow", "playwright", "selenium", "opencv", "cv2", "yt_dlp")
MEDIUM_HINTS = ("pandas", "numpy", "beautifulsoup", "bs4", "requests", "httpx", "flask", "fastapi")

app = Flask(__name__)
jobs: dict[str, dict] = {}
jobs_lock = threading.RLock()
MAX_JOBS = max(1, int(os.environ.get("NODE_MAX_JOBS", "1" if is_android() else "2")))
MAX_JOB_SECONDS = max(1, int(os.environ.get("NODE_MAX_JOB_SECONDS", "120" if is_android() else "900")))


def _capacity():
    with jobs_lock:
        running = sum(j.get("status") in {"running", "stopping"} for j in jobs.values())
    return {"max_jobs": MAX_JOBS, "jobs_running": running, "available_slots": max(0, MAX_JOBS - running)}


def _auth():
    if not NODE_KEY:
        return jsonify({"ok": False, "error": "NODE_KEY is not configured"}), 503
    if request.headers.get("X-Node-Key") != NODE_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401


@app.before_request
def guard():
    if request.path == "/health":
        return None
    return _auth()


def _safe(root_name: str, rel: str = "") -> str:
    if root_name not in ROOTS:
        raise ValueError(f"unknown root '{root_name}'")
    base = ROOTS[root_name]
    target = os.path.realpath(os.path.join(base, (rel or "").lstrip("/\\")))
    if target != base and not target.startswith(base + os.sep):
        raise PermissionError("path escapes configured root")
    if any(part.lower().startswith(".") or any(s in part.lower() for s in SENSITIVE)
           for part in Path(target).parts[len(Path(base).parts):]):
        raise PermissionError("protected path")
    return target


def _sidecar(path: str) -> dict:
    p = Path(path + ".agent.json")
    if p.is_symlink():
        return {"metadata_error": True}
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"metadata_error": True}


def _gpu_info() -> dict:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"available": False}
    try:
        q = "name,memory.total,memory.used,temperature.gpu,utilization.gpu"
        r = subprocess.run([exe, f"--query-gpu={q}", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        vals = [x.strip() for x in r.stdout.splitlines()[0].split(",")]
        return {"available": True, "name": vals[0], "vram_total_mb": int(vals[1]), "vram_used_mb": int(vals[2]),
                "temperature_c": int(vals[3]), "util_pct": int(vals[4])}
    except Exception:
        return {"available": False}


def _profile() -> dict:
    cpu_count = os.cpu_count() or 1
    ram_gb = free_gb = cpu_pct = None
    try:
        if not psutil:
            raise RuntimeError("psutil unavailable")
        vm = psutil.virtual_memory()
        ram_gb = round(vm.total / (1024 ** 3), 1)
        free_gb = round(vm.available / (1024 ** 3), 1)
        cpu_pct = psutil.cpu_percent(interval=0.05)
    except Exception:
        # Android may restrict /proc even when psutil imports successfully.
        pass
    gpu = _gpu_info()
    # Conservative tiers: Pi-class nodes remain edge even if CPU count looks respectable.
    machine = platform.machine().lower()
    if gpu.get("available") and (gpu.get("vram_total_mb") or 0) >= 6000:
        tier = "accelerated"
    elif (ram_gb or 0) >= 12 and cpu_count >= 4:
        tier = "standard"
    else:
        tier = "edge"
    if "arm" in machine or "aarch" in machine:
        if (ram_gb or 0) <= 4:
            tier = "edge"
    mobile = mobile_status()
    if mobile["is_android"]:
        tier = "edge"
    return {"name": NODE_NAME, "hostname": platform.node(), "os": "Android" if mobile["is_android"] else platform.system(), "machine": platform.machine(),
            "cpu_count": cpu_count, "cpu_pct": cpu_pct, "ram_total_gb": ram_gb, "ram_available_gb": free_gb,
            "gpu": gpu, "tier": tier, "mobile": mobile, "capacity": _capacity(),
            "accepting_jobs": mobile["available"] and _capacity()["available_slots"] > 0}


def _estimate(path: str) -> dict:
    meta = _sidecar(path)
    if meta.get("weight") in {"light", "medium", "heavy"}:
        weight = meta["weight"]
    else:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")[:200000].lower()
            size = os.path.getsize(path)
        except Exception:
            text, size = "", 0
        if any(h in text for h in HEAVY_HINTS) or size > 500_000:
            weight = "heavy"
        elif any(h in text for h in MEDIUM_HINTS) or size > 100_000:
            weight = "medium"
        else:
            weight = "light"
    min_ram = float(meta.get("min_ram_gb", {"light": 0.5, "medium": 2, "heavy": 8}[weight]))
    requires_gpu = bool(meta.get("requires_gpu", False))
    prof = _profile()
    compatible = True
    reasons = []
    if prof.get("ram_available_gb") is not None and prof["ram_available_gb"] < min_ram:
        compatible = False; reasons.append(f"needs ~{min_ram:g} GB free RAM")
    if requires_gpu and not prof["gpu"].get("available"):
        compatible = False; reasons.append("requires NVIDIA GPU")
    if prof["tier"] == "edge" and weight == "heavy":
        compatible = False; reasons.append("heavy workload on edge node")
    if prof.get("mobile", {}).get("is_android") and weight != "light":
        reasons.append("Android nodes accept lightweight jobs only")
    reasons.extend(prof.get("mobile", {}).get("reasons", []))
    if not prof.get("capacity", _capacity())["available_slots"]:
        reasons.append("node has no free job slots")
    try:
        runner = _runner(path)
        if not shutil.which(runner[0]):
            reasons.append("required interpreter is unavailable")
    except ValueError as exc:
        reasons.append(str(exc))
    compatible = compatible and not reasons
    return {"weight": weight, "min_ram_gb": min_ram, "requires_gpu": requires_gpu,
            "compatible": compatible, "reasons": reasons, "permission": meta.get("agent_permission", "private"),
            "description": meta.get("description", ""), "ideas": meta.get("ideas", [])}


def _runner(path: str) -> list[str]:
    ext = Path(path).suffix.lower()
    if ext == ".py":
        return [os.environ.get("PYTHON_BIN", sys.executable), path]
    if os.name != "nt" and ext in {".ps1", ".bat", ".cmd"}:
        raise ValueError("Windows script on a non-Windows node")
    if ext == ".ps1": return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
    if ext in (".bat", ".cmd"): return ["cmd", "/c", path]
    if ext == ".sh": return ["bash", path]
    if ext == ".js": return ["node", path]
    raise ValueError(f"unsupported runnable type: {ext}")


@app.route("/health")
def health():
    return jsonify({"ok": True, "node": NODE_NAME})


@app.route("/manifest")
def manifest():
    return jsonify({"ok": True, "node": _profile(), "roots": list(ROOTS),
                    "runnable_extensions": sorted(RUNNABLE if os.name == "nt" else RUNNABLE - {".ps1", ".bat", ".cmd"}), **_capacity()})


@app.route("/browse")
def browse():
    try:
        root, rel = request.args.get("root", next(iter(ROOTS))), request.args.get("path", "")
        target = _safe(root, rel)
        if not os.path.isdir(target):
            return jsonify({"ok": False, "error": "not a directory"}), 404
        agent_only = request.args.get("agent_only", "0") == "1"
        entries = []
        for e in sorted(os.scandir(target), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                _safe(root, os.path.relpath(e.path, ROOTS[root]))
            except (ValueError, PermissionError):
                continue
            if e.name.startswith(".") or any(s in e.name.lower() for s in SENSITIVE) or e.name.endswith(".agent.json"):
                continue
            item = {"name": e.name, "type": "dir" if e.is_dir() else "file"}
            if e.is_file() and Path(e.name).suffix.lower() in RUNNABLE:
                est = _estimate(e.path)
                if agent_only and est["permission"] == "private":
                    continue
                item.update({"runnable": True, **est})
            else:
                item["runnable"] = False
            entries.append(item)
        return jsonify({"ok": True, "node": NODE_NAME, "root": root, "path": rel, "entries": entries})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/inspect")
def inspect_script():
    try:
        root, rel = request.args.get("root", next(iter(ROOTS))), request.args.get("path", "")
        target = _safe(root, rel)
        if not os.path.isfile(target):
            return jsonify({"ok": False, "error": "not a file"}), 404
        return jsonify({"ok": True, "node": NODE_NAME, "root": root, "path": rel, "analysis": _estimate(target)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def _kill_job(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()


def _capture_job(job_id: str, proc: subprocess.Popen):
    # Drain continuously but retain only a bounded tail in memory.
    tails = {"stdout": "", "stderr": ""}
    def drain(stream, key):
        try:
            while chunk := stream.read(4096):
                tails[key] = (tails[key] + chunk)[-MAX_OUTPUT_CHARS:]
        finally:
            stream.close()
    readers = [threading.Thread(target=drain, args=(getattr(proc, key), key), daemon=True) for key in tails]
    for reader in readers: reader.start()
    deadline = time.monotonic() + MAX_JOB_SECONDS
    failure = None
    try:
        while proc.poll() is None:
            power = mobile_status()
            if time.monotonic() >= deadline or not power["available"]:
                failure = "job timeout" if time.monotonic() >= deadline else "; ".join(power["reasons"])
                _kill_job(proc)
                break
            try: proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired: pass
        proc.wait(timeout=10)
    except Exception as exc:
        failure = str(exc)
        try: _kill_job(proc)
        except Exception: pass
    if os.name != "nt":
        # A script exiting must not leave background children outside job limits.
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    for reader in readers: reader.join(timeout=1)
    with jobs_lock:
        jobs[job_id].update({"status": "finished" if proc.returncode == 0 and not failure else "failed",
                            "returncode": proc.returncode, **tails, "error": failure, "finished_at": time.time()})


@app.route("/run", methods=["POST"])
def run_script():
    try:
        d = request.get_json(force=True)
        root, rel = d.get("root", next(iter(ROOTS))), d.get("path", "")
        target = _safe(root, rel)
        if not os.path.isfile(target) or Path(target).suffix.lower() not in RUNNABLE:
            return jsonify({"ok": False, "error": "not a runnable file"}), 400
        owner = bool(d.get("owner", False))
        analysis = _estimate(target)
        if not owner and analysis["permission"] not in ("confirm", "autonomous"):
            return jsonify({"ok": False, "error": f"agent permission is {analysis['permission']}"}), 403
        if not analysis["compatible"]:
            return jsonify({"ok": False, "error": "script is not compatible with this node", "analysis": analysis}), 409
        args = [str(x) for x in (d.get("args") or [])][:32]
        cmd = _runner(target) + args
        with jobs_lock:
            if not _capacity()["available_slots"]:
                return jsonify({"ok": False, "error": "node has no free job slots"}), 409
            proc = subprocess.Popen(cmd, cwd=os.path.dirname(target), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors="replace", shell=False, start_new_session=os.name != "nt",
                                    env=safe_env(tuple(x.strip() for x in os.environ.get("NODE_JOB_ENV", "").split(",") if x.strip()) + ("HOME", "PREFIX", "LD_LIBRARY_PATH", "ANDROID_ROOT", "ANDROID_DATA")))
            jid = uuid.uuid4().hex
            # Bound completed-job history while keeping every active job.
            completed = [k for k, v in jobs.items() if v.get("status") not in {"running", "stopping"}]
            for key in completed[:-49]: jobs.pop(key, None)
            jobs[jid] = {"id": jid, "node": NODE_NAME, "root": root, "path": rel, "pid": proc.pid,
                         "status": "running", "started_at": time.time(), "analysis": analysis}
        threading.Thread(target=_capture_job, args=(jid, proc), daemon=True).start()
        return jsonify({"ok": True, "job": jobs[jid]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/permission", methods=["POST"])
def set_permission():
    try:
        d = request.get_json(force=True)
        root, rel = d.get("root", next(iter(ROOTS))), d.get("path", "")
        permission = d.get("permission", "private")
        if permission not in ("private", "visible", "confirm", "autonomous"):
            return jsonify({"ok": False, "error": "permission must be private|visible|confirm|autonomous"}), 400
        target = _safe(root, rel)
        if not os.path.isfile(target) or Path(target).suffix.lower() not in RUNNABLE:
            return jsonify({"ok": False, "error": "not a runnable file"}), 400
        sidecar = Path(target + ".agent.json")
        if sidecar.is_symlink():
            raise PermissionError("sidecar must not be a symlink")
        meta = _sidecar(target)
        meta.pop("metadata_error", None)
        meta["agent_permission"] = permission
        sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return jsonify({"ok": True, "node": NODE_NAME, "root": root, "path": rel, "permission": permission})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/jobs")
def list_jobs():
    return jsonify({"ok": True, "jobs": list(jobs.values())[-50:]})


@app.route("/job")
def get_job():
    jid = request.args.get("id", "")
    j = jobs.get(jid)
    return jsonify({"ok": bool(j), "job": j, "error": None if j else "job not found"}), (200 if j else 404)


@app.route("/stop", methods=["POST"])
def stop_job():
    d = request.get_json(force=True)
    jid = d.get("id", "")
    j = jobs.get(jid)
    if not j or j.get("status") != "running":
        return jsonify({"ok": False, "error": "running job not found"}), 404
    try:
        j["status"] = "stopping"
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(j["pid"]), "/T", "/F"], capture_output=True)
        else:
            os.killpg(j["pid"], signal.SIGKILL)
        return jsonify({"ok": True, "job": j})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    if not NODE_KEY:
        raise SystemExit("Set NODE_KEY before starting node_agent.py")
    for name, root in ROOTS.items():
        print(f"root {name}: {root}")
    print(f"node {NODE_NAME} listening on 0.0.0.0:{NODE_PORT}")
    app.run(host="0.0.0.0", port=NODE_PORT, threaded=True)
