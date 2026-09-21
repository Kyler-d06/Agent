"""
vault.py — Obsidian vault read/write operations
Understands: Smart Connections, Tasks plugin, Dataview, Templater
"""

import os
import re
import json
import logging
from datetime import datetime
from pathlib import Path

import frontmatter
from platform_contracts import confined

logger = logging.getLogger(__name__)

DAILY_NOTES_FOLDER = os.getenv("DAILY_NOTES_FOLDER", "Daily Notes")
AGENT_LOGS_FOLDER  = os.getenv("AGENT_LOGS_FOLDER",  "Agent Logs")

# All file extensions the agent can read and search.
# Add any extension your vault contains — e.g. "rs,go,cpp"
CODE_EXTENSIONS: set[str] = set(
    e.strip().lstrip(".")
    for e in os.getenv(
        "CODE_EXTENSIONS",
        "py,js,ts,jsx,tsx,mjs,cjs,css,scss,html,json,yaml,yml,toml,sh,bat,ps1,md",
    ).split(",")
    if e.strip()
)

# Folders the agent should never touch
IGNORED_FOLDERS = {".git", ".obsidian", ".smart-env", ".trash", "node_modules", "__pycache__"}


class ObsidianVault:
    def __init__(self, vault_path: str):
        self.root = Path(vault_path).expanduser().resolve()
        if not self.root.exists():
            raise FileNotFoundError(f"Vault not found: {vault_path}")
        logger.info(f"Vault loaded: {self.root}")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _is_hidden(self, path: Path) -> bool:
        try:
            confined(self.root, str(path))
        except ValueError:
            return True
        return any(
            p.startswith(".") or p in IGNORED_FOLDERS
            for p in path.parts
        )

    def _all_notes(self):
        """Yield every .md file in the vault (skips hidden/system folders)."""
        for md in self.root.rglob("*.md"):
            if not self._is_hidden(md.relative_to(self.root)):
                yield md

    def _all_files(self):
        """
        Yield every indexed file (all CODE_EXTENSIONS including .md).
        This is what makes code files visible to the agent.
        """
        for ext in CODE_EXTENSIONS:
            for f in self.root.rglob(f"*.{ext}"):
                try:
                    rel = f.relative_to(self.root)
                except ValueError:
                    continue
                if not self._is_hidden(rel):
                    yield f

    def _resolve(self, note_path: str) -> Path:
        """Find a .md note — exact path first, then fuzzy stem match."""
        exact = confined(self.root, note_path)
        if exact.exists():
            return exact
        stem = Path(note_path).stem.lower()
        for md in self._all_notes():
            if md.stem.lower() == stem:
                return md
        return exact

    def _resolve_any(self, file_path: str) -> Path:
        """
        Find ANY file in the vault by relative path or stem.
        Tries exact match first, then searches all indexed extensions.
        """
        exact = confined(self.root, file_path)
        if exact.exists():
            return exact
        # Fuzzy: match stem across all indexed extensions
        stem = Path(file_path).stem.lower()
        suffix = Path(file_path).suffix.lower()
        for f in self._all_files():
            if f.stem.lower() == stem and (not suffix or f.suffix.lower() == suffix):
                return f
        return exact  # caller handles missing

    # ── core note operations ──────────────────────────────────────────────────

    def search_notes(self, query: str, max_results: int = 4) -> list[dict]:
        """
        Keyword relevance search across ALL vault files (notes + code).
        Returns results sorted by relevance score.
        """
        terms = [t.lower() for t in query.split() if len(t) > 2]
        if not terms:
            return []

        hits = []
        for f in self._all_files():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                lower = text.lower()
                score = sum(lower.count(t) for t in terms)
                score += sum(10 for t in terms if t in f.stem.lower())
                if score:
                    hits.append({
                        "path":    str(f.relative_to(self.root)),
                        "title":   f.name,
                        "score":   score,
                        # FIX: was 400 chars — 6 results * 400 = 2400 chars of
                        # preview flooding the model before it reads anything.
                        # 120 chars is enough to identify the right file.
                        "preview": text[:120].strip(),
                    })
            except Exception:
                continue

        hits.sort(key=lambda x: x["score"], reverse=True)
        return hits[:max_results]

    def read_note(self, note_path: str) -> str:
        """Return the full content of a .md note (fuzzy match on stem)."""
        path = self._resolve(note_path)
        if not path.exists():
            return f"Note not found: {note_path}"
        return path.read_text(encoding="utf-8", errors="replace")

    def read_file(self, file_path: str) -> str:
        """
        Read ANY file in the vault — .py, .js, .ts, .json, .md, etc.
        Use this instead of read_note when working with code files.
        """
        path = self._resolve_any(file_path)
        if not path.exists():
            return f"File not found: {file_path}\nTip: use list_code_files to see available files."
        return path.read_text(encoding="utf-8", errors="replace")

    def write_note(self, note_path: str, content: str) -> str:
        """Create or overwrite a note."""
        path = confined(self.root, note_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"✅ Written: {note_path}"

    def append_to_note(self, note_path: str, content: str) -> str:
        """Append text to a note (creates it if missing)."""
        path = confined(self.root, note_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        separator = "\n" if existing and not existing.endswith("\n") else ""
        path.write_text(existing + separator + content, encoding="utf-8")
        return f"✅ Appended to: {note_path}"

    def list_folder(self, folder: str = "") -> list[str]:
        """List all .md notes inside a vault folder."""
        base = confined(self.root, folder)
        if not base.exists():
            return [f"Folder not found: {folder}"]
        notes = sorted(
            str(md.relative_to(self.root))
            for md in base.rglob("*.md")
            if not self._is_hidden(md.relative_to(self.root))
        )
        return notes or ["(empty folder)"]

    def list_code_files(self, folder: str = "") -> list[str]:
        """
        List all non-.md code files in the vault (or a subfolder).
        This is the correct tool for finding Python, JS, etc. files.
        """
        base = confined(self.root, folder)
        if not base.exists():
            return [f"Folder not found: {folder}"]

        results = []
        non_md_exts = CODE_EXTENSIONS - {"md"}
        for ext in non_md_exts:
            for f in base.rglob(f"*.{ext}"):
                try:
                    rel = f.relative_to(self.root)
                except ValueError:
                    continue
                if not self._is_hidden(rel):
                    results.append(str(rel))

        return sorted(results) or ["(no code files found — check CODE_EXTENSIONS in .env)"]

    def search_code(self, query: str, max_results: int = 4) -> list[dict]:
        """
        Search specifically inside code files (.py, .js, .ts, etc. — not .md).
        Use this when looking for function definitions, imports, logic, etc.
        """
        terms = [t.lower() for t in query.split() if len(t) > 1]
        if not terms:
            return []

        non_md_exts = CODE_EXTENSIONS - {"md"}
        hits = []

        for ext in non_md_exts:
            for f in self.root.rglob(f"*.{ext}"):
                try:
                    rel = f.relative_to(self.root)
                except ValueError:
                    continue
                if self._is_hidden(rel):
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    lower = text.lower()
                    score = sum(lower.count(t) for t in terms)
                    score += sum(15 for t in terms if t in f.stem.lower())
                    if score:
                        hits.append({
                            "path":    str(rel),
                            "title":   f.name,
                            "score":   score,
                            "preview": text[:120].strip(),
                        })
                except Exception:
                    continue

        hits.sort(key=lambda x: x["score"], reverse=True)
        return hits[:max_results]

    # ── Tasks plugin ─────────────────────────────────────────────────────────

    TASK_RE = re.compile(r"^-\s+\[( |x|X|-|/)\]\s+(.+)$", re.MULTILINE)
    DUE_RE  = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
    TAG_RE  = re.compile(r"#([\w/-]+)")

    def get_tasks(
        self,
        filter_tag: str = None,
        include_completed: bool = False,
        due_before: str = None,
    ) -> list[dict]:
        """Collect tasks across the entire vault (Tasks plugin format)."""
        tasks = []
        for md in self._all_notes():
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue

            for m in self.TASK_RE.finditer(text):
                char = m.group(1)
                body = m.group(2).strip()
                done = char.lower() == "x"

                if done and not include_completed:
                    continue

                tags = self.TAG_RE.findall(body)
                if filter_tag and filter_tag.lstrip("#") not in tags:
                    continue

                due_m = self.DUE_RE.search(body)
                due   = due_m.group(1) if due_m else None

                if due_before and due and due > due_before:
                    continue

                tasks.append({
                    "task": re.sub(r"[📅⏫🔼🔽⏬]\s*\S*", "", body).strip(),
                    "raw":  body,
                    "done": done,
                    "tags": tags,
                    "due":  due,
                    "note": md.stem,
                    "file": str(md.relative_to(self.root)),
                })

        tasks.sort(key=lambda t: (t["done"], t["due"] or "9999-99-99"))
        return tasks

    def add_task(self, task_text: str, note_name: str = None) -> str:
        """Add a task (Tasks plugin format) to a note."""
        if not note_name:
            today = datetime.now().strftime("%Y-%m-%d")
            note_name = f"{DAILY_NOTES_FOLDER}/{today}"
        note_path = note_name if note_name.endswith(".md") else f"{note_name}.md"
        return self.append_to_note(note_path, f"- [ ] {task_text}")

    def complete_task(self, task_text_fragment: str) -> str:
        """Mark a task as complete by searching for it."""
        for md in self._all_notes():
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            if task_text_fragment.lower() in text.lower():
                today = datetime.now().strftime("%Y-%m-%d")
                updated = re.sub(
                    rf"- \[ \] (.*{re.escape(task_text_fragment)}.*)",
                    rf"- [x] \1 ✅ {today}",
                    text,
                    flags=re.IGNORECASE,
                )
                if updated != text:
                    md.write_text(updated, encoding="utf-8")
                    return f"✅ Completed task in {md.stem}"
        return "Task not found"

    # ── Daily Notes ───────────────────────────────────────────────────────────

    def get_daily_note(self, date_str: str = None) -> str:
        """Get (or create) a daily note. date_str format: YYYY-MM-DD."""
        date   = date_str or datetime.now().strftime("%Y-%m-%d")
        note_p = f"{DAILY_NOTES_FOLDER}/{date}.md"
        full   = confined(self.root, note_p)

        if full.exists():
            return full.read_text(encoding="utf-8")

        day_label = datetime.strptime(date, "%Y-%m-%d").strftime("%A, %B %d %Y")
        content = f"""---
date: {date}
type: daily
---

# {day_label}

## 🎯 Top Priorities
- [ ] 
- [ ] 
- [ ] 

## 📝 Notes


## 🧠 Agent Log

"""
        self.write_note(note_p, content)
        return content

    # ── Dataview-style queries ────────────────────────────────────────────────

    def dataview_query(self, query: str) -> list[dict]:
        """
        Basic frontmatter query.
        Supports: field = value, field != value
        """
        cond_re    = re.compile(r'(\w+)\s*(=|!=)\s*["\']?([^\s"\']+)["\']?')
        conditions = cond_re.findall(query.lower())

        results = []
        for md in self._all_notes():
            try:
                post = frontmatter.loads(md.read_text(encoding="utf-8"))
                meta = {k.lower(): str(v).lower() for k, v in post.metadata.items()}

                match = all(
                    (meta.get(f) == v) if op == "=" else (meta.get(f) != v)
                    for f, op, v in conditions
                )
                if match:
                    results.append({
                        "file":     str(md.relative_to(self.root)),
                        "title":    md.stem,
                        "metadata": dict(post.metadata),
                        "preview":  post.content[:300].strip(),
                    })
            except Exception:
                continue

        return results

    # ── Smart Connections helpers ─────────────────────────────────────────────

    def get_related_notes(self, note_path: str, max_results: int = 5) -> list[dict]:
        """Find related notes by key-term extraction (Smart Connections approx.)."""
        content = self.read_note(note_path)
        if content.startswith("Note not found"):
            return []

        stop = {"the","a","an","is","are","was","were","be","to","of","and",
                "or","in","on","at","for","with","this","that","it","as","by"}
        words = [w.lower() for w in re.findall(r'\b[a-zA-Z]{4,}\b', content)]
        key_terms = [w for w in words if w not in stop]

        from collections import Counter
        top_terms = [t for t, _ in Counter(key_terms).most_common(5)]
        if not top_terms:
            return []

        results = self.search_notes(" ".join(top_terms), max_results=max_results + 1)
        stem = Path(note_path).stem
        return [r for r in results if r["title"] != stem][:max_results]

    # ── Logging ───────────────────────────────────────────────────────────────

    def log_interaction(self, user_msg: str, agent_reply: str) -> None:
        """Append every conversation turn to the agent log for that day."""
        today     = datetime.now().strftime("%Y-%m-%d")
        timestamp = datetime.now().strftime("%H:%M")
        note_path = f"{AGENT_LOGS_FOLDER}/{today}.md"

        header = ""
        full   = confined(self.root, note_path)
        if not full.exists():
            header = f"# Agent Log — {today}\n\n"

        entry = (
            f"{header}"
            f"#### {timestamp}\n"
            f"**You:** {user_msg}\n\n"
            f"**Agent:** {agent_reply}\n\n---\n"
        )
        self.append_to_note(note_path, entry)
