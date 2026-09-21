"""Workspace-scoped deliverables, deterministic verification and sandboxed tests."""
from __future__ import annotations

import csv
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import tokenize
import uuid
import threading
from pathlib import Path

from platform_contracts import canonical, confined, object_schema, tool


TEXT = {"type": "string"}
WORK_TOOLS = [
    tool("workspace_list", "List project files to plan coding or document work.", {"path": TEXT}),
    tool("workspace_read", "Read a text source file or document in the workspace.", {"path": TEXT}, ["path"]),
    tool("workspace_write", "Create or update a deliverable in the workspace. Use expected_sha256 when replacing a file.",
         {"path": TEXT, "content": TEXT, "expected_sha256": TEXT}, ["path", "content"], effect="write"),
    tool("workspace_patch", "Replace one exact text fragment in a workspace file and return the new hash. The old text must occur exactly once.",
         {"path": TEXT, "old_str": TEXT, "new_str": TEXT, "expected_sha256": TEXT}, ["path", "old_str", "new_str"], effect="write"),
    tool("source_bug_scan", "Statically scan a source-code copy or repository for syntax errors and high-signal Python bug patterns without executing code.",
         {"path": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}),
    tool("source_copy_create", "Create a private, isolated Git-backed copy of source code under self_improvement_copies for autonomous repair work. The original is never modified.",
         {"path": TEXT, "label": TEXT}, effect="write"),
    tool("workspace_test", "Run Python unittest or pytest in a restricted Docker container and record actual test results.",
         {"path": TEXT, "runner": {"type": "string", "enum": ["unittest", "pytest"]}}, effect="execute"),
    tool("workspace_diff", "Inspect the current Git patch without committing or pushing.", {"path": TEXT}),
    tool("workspace_fingerprint", "Hash the current non-private source tree to check whether tested files changed.", {"path": TEXT}),
    tool("worktree_create", "Create an isolated Git worktree under the work root for a coding task. Returns its relative workspace path.",
         {"path": TEXT, "branch": TEXT}, effect="execute"),
    tool("export_patch", "Export tracked changes and new text files as a patch artifact without committing or pushing.",
         {"path": TEXT, "output": TEXT, "job_id": TEXT, "summary": TEXT}, ["output"], effect="write"),
    tool("register_artifact", "Record a produced file and its content hash as a task deliverable.",
         {"path": TEXT, "kind": TEXT, "job_id": TEXT, "metadata": {"type": "object"}}, ["path", "kind"], effect="write"),
    tool("verify_artifact", "Verify that a registered deliverable still exists and matches its recorded hash.", {"id": TEXT}, ["id"]),
    tool("write_research_report", "Write a Markdown research report with explicit source links and register the resulting artifact.",
         {"path": TEXT, "title": TEXT, "body": TEXT, "sources": {"type": "array", "minItems": 1, "items": object_schema({"title": TEXT, "url": TEXT}, ["title", "url"])}, "job_id": TEXT},
         ["path", "title", "body", "sources"], effect="write"),
    tool("csv_summary", "Inspect a CSV, count records and summarize numeric columns without loading office software.", {"path": TEXT}, ["path"]),
    tool("list_work_templates", "Get reusable coding, research, forecasting, impact, office, browser, and operations task procedures."),
]

WORK_TEMPLATES = {
    "coding": {"steps": ["inspect files and requirements", "create an isolated worktree or source copy", "scan and reproduce the defect", "maintain a visible structured plan", "make minimal patch-style edits", "run sandbox tests", "inspect diff and export patch", "register changed deliverables"], "verification": ["plan is complete", "tests passed on current source", "patch artifact hashes match"]},
    "research": {"steps": ["define question", "retrieve prior knowledge", "search and read original sources", "seek contradictory evidence", "write cited report", "register report"], "verification": ["source URLs recorded", "report artifact verified"], "limitation": "source links and hashes do not establish factual correctness"},
    "forecast": {"steps": ["define a resolvable question and horizon", "build ontology context", "assess whether the context and base-rate evidence are sufficient; if not, return a missing-context checklist without creating a forecast", "retrieve base rates and current evidence", "seek disconfirming evidence", "define mutually exclusive outcomes", "assign probabilities that sum to one", "record assumptions, unknowns, context references, and resolution criterion", "create the immutable forecast"], "verification": ["forecast has sourced context", "outcomes sum to one", "resolution criterion and due date recorded"], "limitation": "a probability expresses bounded uncertainty, not certainty or prophecy"},
    "impact": {"steps": ["identify a supported discovery, forecast, or hypothesis", "estimate probability of success, impact, effort, and safety risk", "define an artifact and measurable acceptance criteria", "produce the deliverable with the appropriate domain tools", "test or inspect it", "register and verify the artifact", "record observed outcome when evidence exists"], "verification": ["source research linked", "acceptance criteria checked", "artifact verified"], "limitation": "expected impact is an estimate; deployment and external side effects still require owner authorization"},
    "office": {"steps": ["inspect input files", "perform calculations or transformations", "create deliverable", "check totals and format", "register artifact"], "verification": ["input/output checks", "artifact verified"]},
    "browser": {"steps": ["inspect configured application", "perform one authorized action", "read resulting state", "record evidence"], "verification": ["observed application state matches expected result"]},
    "operations": {"steps": ["read system and mesh health", "diagnose using evidence", "select authorized recovery script", "execute", "read health again"], "verification": ["post-action health meets requirement"]},
}


def sandbox_command(image, mount, command, *, writable=False, network=False):
    return ["docker", "run", "--rm", "--name", "agent-" + uuid.uuid4().hex,
            "--network", "bridge" if network else "none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--memory=512m", "--cpus=1", "--pids-limit=128", "--user=65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-v", f"{Path(mount).resolve()}:/app:{'rw' if writable else 'ro'}", "-w", "/app", image, *command]


def run_container(command, timeout=120, input_text=None):
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required for code execution; host execution fallback is disabled")
    try:
        # Disk-backed output prevents a noisy child from exhausting core RAM.
        import tempfile
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.run(command, input=input_text.encode() if input_text is not None else None,
                                  stdout=stdout, stderr=stderr, timeout=timeout)
            stdout.seek(0)
            stderr.seek(0)
            return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                    "stdout": stdout.read(100000).decode(errors="replace"), "stderr": stderr.read(100000).decode(errors="replace")}
    except subprocess.TimeoutExpired:
        name = command[command.index("--name") + 1]
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "container exceeded time budget"}


class WorkTools:
    def __init__(self, root, store):
        self.root, self.store = Path(root).resolve(), store
        self._write_lock = threading.Lock()

    def invoke(self, name, args):
        fn = getattr(self, name)
        return fn(**args)

    def workspace_list(self, path=""):
        root = confined(self.root, path)
        out = []
        for p in sorted(root.iterdir()):
            try:
                confined(self.root, p.relative_to(self.root))
            except ValueError:
                continue
            out.append({"path": str(p.relative_to(self.root)), "directory": p.is_dir(), "bytes": p.stat().st_size if p.is_file() else None})
            if len(out) >= 300:
                break
        return out

    def workspace_read(self, path):
        p = confined(self.root, path)
        if p.stat().st_size > 1_000_000:
            raise ValueError("text file exceeds 1 MB")
        data = p.read_bytes()
        return {"path": path, "content": data.decode("utf-8"), "sha256": hashlib.sha256(data).hexdigest()}

    def workspace_write(self, path, content, expected_sha256=None):
        with self._write_lock:
            return self._write_file(path, content, expected_sha256)

    def workspace_patch(self, path, old_str, new_str, expected_sha256=None):
        if not old_str:
            raise ValueError("old_str must not be empty")
        with self._write_lock:
            current = self.workspace_read(path)
            if expected_sha256 and current["sha256"] != expected_sha256:
                raise ValueError("existing file does not match expected_sha256")
            count = current["content"].count(old_str)
            if count != 1:
                raise ValueError(f"old_str must match exactly once; found {count}")
            return self._write_file(path, current["content"].replace(old_str, new_str, 1), current["sha256"])

    def source_bug_scan(self, path="", limit=200):
        root = confined(self.root, path)
        if not root.is_dir():
            raise ValueError("scan path must be a directory")
        findings, scanned, total_bytes = [], 0, 0

        def add(kind, file_path, line, message, severity="warning"):
            if len(findings) < int(limit):
                findings.append({"kind": kind, "severity": severity, "path": file_path,
                                 "line": int(line or 1), "message": message})

        for candidate in sorted(root.rglob("*.py")):
            relative = candidate.relative_to(self.root)
            source_relative = candidate.relative_to(root)
            if any(part.startswith(".") or part in {"__pycache__", "node_modules", "self_improvement_copies"}
                   for part in source_relative.parts):
                continue
            try:
                confined(self.root, relative)
                size = candidate.stat().st_size
                if size > 1_000_000 or total_bytes + size > 25_000_000 or scanned >= 2000:
                    continue
                source = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            scanned += 1
            total_bytes += size
            rel_text = relative.as_posix()
            try:
                tree = ast.parse(source, filename=rel_text)
            except SyntaxError as exc:
                add("syntax_error", rel_text, exc.lineno, exc.msg, "error")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    add("bare_except", rel_text, node.lineno, "bare except can hide interrupts and unrelated failures")
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defaults = [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]
                    if any(isinstance(d, (ast.List, ast.Dict, ast.Set)) for d in defaults):
                        add("mutable_default", rel_text, node.lineno, f"{node.name} has a mutable default argument")
            # Only comments can be unfinished-work markers. Searching raw lines
            # reports false positives for tests containing example strings and
            # for this scanner's own regular expression.
            try:
                comments = (token for token in tokenize.generate_tokens(io.StringIO(source).readline)
                            if token.type == tokenize.COMMENT)
                for token in comments:
                    if re.search(r"\b(?:TODO|FIXME|XXX)\b", token.string, re.I):
                        add("unfinished_marker", rel_text, token.start[0], token.string.strip()[:240], "info")
            except (IndentationError, tokenize.TokenError):
                # ast.parse already accepted the file; tokenization failure is
                # non-fatal and must not invent a finding.
                pass
        return {"path": path, "files_scanned": scanned, "bytes_scanned": total_bytes,
                "findings": findings, "truncated": len(findings) >= int(limit),
                "summary": {"errors": sum(f["severity"] == "error" for f in findings),
                            "warnings": sum(f["severity"] == "warning" for f in findings),
                            "info": sum(f["severity"] == "info" for f in findings)}}

    def source_copy_create(self, path="", label="repair"):
        import re
        import tempfile
        source = confined(self.root, path)
        if not source.is_dir():
            raise ValueError("source path must be a directory")
        safe_label = re.sub(r"[^a-z0-9-]+", "-", str(label).lower()).strip("-")[:32] or "repair"
        identity = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        relative_destination = Path("self_improvement_copies") / f"{identity}-{safe_label}"
        destination = confined(self.root, relative_destination.as_posix())
        if destination.exists():
            raise ValueError("copy destination already exists")
        copied_files, copied_bytes = 0, 0
        with tempfile.TemporaryDirectory(prefix="agent-source-copy-") as temp_name:
            staging = Path(temp_name) / "source"
            staging.mkdir()
            for candidate in sorted(source.rglob("*")):
                try:
                    source_relative = candidate.relative_to(source)
                    workspace_relative = candidate.relative_to(self.root)
                    if any(part.startswith(".") or part in {"__pycache__", "node_modules", "self_improvement_copies"}
                           for part in source_relative.parts):
                        continue
                    confined(self.root, workspace_relative)
                    if candidate.is_symlink() or candidate.is_dir():
                        continue
                    size = candidate.stat().st_size
                    if size > 1_000_000:
                        continue
                    copied_files += 1
                    copied_bytes += size
                    if copied_files > 10000 or copied_bytes > 100_000_000:
                        raise ValueError("source copy exceeds 100 MB or 10000 files")
                    target = staging / source_relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(candidate, target)
                except ValueError:
                    continue
                except OSError:
                    raise
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging, destination)
        hooks = self.root / ".platform" / "empty-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        commands = [
            ["git", "-c", "core.hooksPath=" + str(hooks), "-C", str(destination), "init"],
            ["git", "-c", "core.hooksPath=" + str(hooks), "-C", str(destination), "add", "-A"],
            ["git", "-c", "core.hooksPath=" + str(hooks), "-c", "user.name=Universal Assistant",
             "-c", "user.email=assistant@localhost.invalid", "-C", str(destination), "commit", "--allow-empty", "-m", "Isolated source baseline"],
        ]
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout)[:2000])
        head = subprocess.run(["git", "-C", str(destination), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20)
        if head.returncode:
            raise RuntimeError(head.stderr[:2000])
        return {"path": relative_destination.as_posix(), "source": path, "files": copied_files,
                "bytes": copied_bytes, "baseline_commit": head.stdout.strip(), "original_modified": False}

    def _write_file(self, path, content, expected_sha256=None):
        p = confined(self.root, path)
        data = content.encode("utf-8")
        if len(data) > 1_000_000:
            raise ValueError("file exceeds 1 MB")
        if p.exists() and (not expected_sha256 or hashlib.sha256(p.read_bytes()).hexdigest() != expected_sha256):
            raise ValueError("existing file requires its current expected_sha256")
        if expected_sha256 and not p.exists():
            raise ValueError("expected file no longer exists")
        p.parent.mkdir(parents=True, exist_ok=True)
        temp = p.with_name(p.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, p)
        finally:
            temp.unlink(missing_ok=True)
        return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def workspace_test(self, path="", runner="unittest"):
        p = confined(self.root, path)
        if not p.is_dir():
            raise ValueError("test path must be a directory")
        image = os.environ.get("WORKSPACE_TEST_IMAGE", "python:3.12-slim")
        args = ["python", "-m", "unittest", "discover", "-v"] if runner == "unittest" else ["python", "-m", "pytest", "-p", "no:cacheprovider"]
        import tempfile
        # Only a filtered snapshot is exposed to generated tests. Private runtime
        # files and browser sessions are never mounted into the code container.
        with tempfile.TemporaryDirectory(prefix="agent-tests-") as tmp:
            fingerprint = self._snapshot(p, Path(tmp))
            result = run_container(sandbox_command(image, tmp, args), timeout=180)
        # unittest's zero-test exit status is 0, but does not verify a change.
        import re
        if runner == "unittest" and not re.search(r"Ran [1-9][0-9]* tests?", result["stderr"] + result["stdout"]):
            result["ok"] = False
            result["stderr"] += "\nNo executed tests detected."
        result["verified"] = result["ok"]
        result["source_sha256"] = fingerprint
        result["path"] = path
        return result

    def _snapshot(self, root, destination=None):
        hashes = []
        total = 0
        for base, dirs, files in os.walk(root, followlinks=False):
            allowed = []
            for directory in sorted(dirs):
                try:
                    p = Path(base) / directory
                    confined(self.root, p.relative_to(self.root))
                    if not p.is_symlink():
                        allowed.append(directory)
                except ValueError:
                    pass
            dirs[:] = allowed
            for filename in sorted(files):
                p = Path(base) / filename
                try:
                    confined(self.root, p.relative_to(self.root))
                    if p.is_symlink():
                        continue
                except ValueError:
                    continue
                total += p.stat().st_size
                if total > 100_000_000 or len(hashes) >= 10000:
                    raise ValueError("source snapshot exceeds 100 MB or 10000 files")
                data = p.read_bytes()
                relative = p.relative_to(root)
                hashes.append((relative.as_posix(), hashlib.sha256(data).hexdigest()))
                if destination:
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
        return hashlib.sha256(canonical(hashes).encode()).hexdigest()

    def workspace_fingerprint(self, path=""):
        return {"path": path, "sha256": self._snapshot(confined(self.root, path))}

    def workspace_diff(self, path=""):
        p = confined(self.root, path)
        # Small models sometimes supply a file inside the copy rather than the
        # copy root. Resolve the containing Git worktree deterministically so a
        # harmless argument mistake does not derail an unattended repair.
        start = p.parent if p.is_file() else p
        root_result = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                                     capture_output=True, text=True, timeout=20)
        if root_result.returncode:
            return {"ok": False, "patch": "", "stderr": root_result.stderr[:2000]}
        git_root = Path(root_result.stdout.strip()).resolve()
        confined(self.root, git_root.relative_to(self.root))
        listing = subprocess.run(["git", "-C", str(git_root), "diff", "--name-only", "-z"], capture_output=True, text=True, timeout=20)
        if listing.returncode:
            return {"ok": False, "stderr": listing.stderr[:2000]}
        files = []
        for name in listing.stdout.split("\0"):
            if name:
                try:
                    confined(git_root, name)
                    files.append(name)
                except ValueError:
                    pass
        if not files:
            return {"ok": True, "patch": "", "stderr": ""}
        proc = subprocess.run(["git", "-C", str(git_root), "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--", *files], capture_output=True, text=True, timeout=20)
        return {"ok": proc.returncode == 0, "patch": proc.stdout[:100000], "truncated": len(proc.stdout) > 100000, "stderr": proc.stderr[:2000]}

    def worktree_create(self, path="", branch=None):
        import re
        repository = confined(self.root, path)
        identity = uuid.uuid4().hex[:12]
        branch = branch or "codex/agent-" + identity
        if not re.fullmatch(r"codex/[A-Za-z0-9][A-Za-z0-9/_-]{0,80}", branch):
            raise ValueError("branch must be a valid codex/ name")
        destination = confined(self.root, "worktrees/" + identity)
        destination.parent.mkdir(parents=True, exist_ok=True)
        hooks = self.root / ".platform" / "empty-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["git", "-c", "core.hooksPath=" + str(hooks), "-C", str(repository), "worktree", "add", "-b", branch, str(destination), "HEAD"],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr[:2000])
        return {"path": str(destination.relative_to(self.root)), "branch": branch, "base": "HEAD", "includes_uncommitted_changes": False}

    def export_patch(self, output, path="", job_id=None, summary=""):
        import difflib
        if Path(output).suffix.lower() != ".patch":
            raise ValueError("patch output must end in .patch")
        repository = confined(self.root, path)
        result = self.workspace_diff(path)
        if not result["ok"]:
            raise ValueError(result["stderr"])
        if result.get("truncated"):
            raise ValueError("tracked diff exceeds the 100 KB export limit; split the change into smaller patches")
        patch = result["patch"]
        listing = subprocess.run(["git", "-C", str(repository), "ls-files", "--others", "--exclude-standard", "-z"], capture_output=True, text=True, timeout=20)
        if listing.returncode:
            raise RuntimeError(listing.stderr[:2000])
        for name in listing.stdout.split("\0"):
            if not name:
                continue
            try:
                p = confined(repository, name)
                if p.stat().st_size > 1_000_000:
                    raise ValueError("untracked file exceeds text patch limit")
                contents = p.read_text(encoding="utf-8").splitlines(keepends=True)
            except (ValueError, UnicodeDecodeError):
                continue
            patch += "diff --git a/" + name + " b/" + name + "\nnew file mode 100644\n"
            patch += "".join(difflib.unified_diff([], contents, fromfile="/dev/null", tofile="b/" + name))
            if contents and not contents[-1].endswith("\n"):
                patch += "\n\\ No newline at end of file\n"
        if not patch.strip():
            raise ValueError("no text changes to export")
        self.workspace_write(output, patch)
        return self.register_artifact(output, "git_patch", job_id, {"worktree": path, "binary_files": "not included",
                                                                      "summary": str(summary)[:4000]})

    def register_artifact(self, path, kind, job_id=None, metadata=None):
        p = confined(self.root, path)
        if not p.is_file() or p.stat().st_size > 100_000_000:
            raise ValueError("artifact must be an existing file under 100 MB")
        with p.open("rb") as f:
            sha = hashlib.file_digest(f, "sha256").hexdigest()
        aid = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)", (aid, job_id, path, sha, kind, canonical(metadata or {}), time.time()))
        return {"id": aid, "path": path, "sha256": sha}

    def verify_artifact(self, id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (id,)).fetchone()
        if not row:
            raise ValueError("unknown artifact")
        p = confined(self.root, row["path"])
        with p.open("rb") as f:
            current = hashlib.file_digest(f, "sha256").hexdigest()
        return {"id": id, "verified": current == row["sha256"], "path": row["path"], "sha256": current}

    def write_research_report(self, path, title, body, sources, job_id=None):
        if Path(path).suffix.lower() != ".md":
            raise ValueError("research reports must use a .md filename")
        if any(not s["url"].startswith(("https://", "http://")) for s in sources):
            raise ValueError("sources require HTTP(S) URLs")
        text = "# " + title + "\n\n" + body + "\n\n## Sources\n\n" + "\n".join(f"- [{s['title']}]({s['url']})" for s in sources) + "\n"
        self.workspace_write(path, text)
        return self.register_artifact(path, "research_report", job_id, {"sources": sources, "factual_accuracy": "requires evidence review"})

    def csv_summary(self, path):
        text = self.workspace_read(path)["content"]
        reader = csv.DictReader(io.StringIO(text))
        stats = {c: {"numeric_count": 0, "sum": 0.0} for c in reader.fieldnames or []}
        count = 0
        for row in reader:
            count += 1
            for key, value in row.items():
                if key not in stats:
                    continue
                try:
                    number = float(value)
                    import math
                    if not math.isfinite(number):
                        continue
                except (ValueError, TypeError):
                    continue
                stats[key]["numeric_count"] += 1
                stats[key]["sum"] += number
        return {"rows": count, "columns": stats}

    def list_work_templates(self):
        return WORK_TEMPLATES
