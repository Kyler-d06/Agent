"""
tools.py — External tool implementations for the NEXUS v2 agent

New in v2:
  • apply_diff        — apply unified diff patch to a vault file (safer than full replace)
  • str_replace_file  — surgical single-occurrence string swap in any vault file
  • view_file_lines   — read file with 1-indexed line numbers (OpenHands-style)
  • run_tests         — run pytest/unittest, return structured pass/fail
  • browser_fetch     — static HTTPS fetch without browser automation

Existing tools kept intact (web_search, fetch_url, get_weather, calculate,
datetime_util, apply_template, sandbox helpers, Windows extras).
"""

import re
import ast
import math
import json
import logging
import operator
import os
import shlex
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import requests
from web_safety import safe_get, validate_public_url

logger = logging.getLogger(__name__)

DAILY_NOTES_FOLDER = os.getenv("DAILY_NOTES_FOLDER", "Daily Notes")


# ════════════════════════════════════════════════════════════════════════════════
# NEW v2 TOOLS
# ════════════════════════════════════════════════════════════════════════════════

# ── apply_diff ────────────────────────────────────────────────────────────────

def apply_diff(vault_root: str, file_path: str, diff_patch: str) -> str:
    """
    Apply a unified diff patch to a file inside the vault.

    Runs `patch --dry-run` first to validate; only applies if validation passes.
    Supports both --- a/path and --- path header formats.

    Args:
        vault_root:  Absolute path to the vault root directory.
        file_path:   Vault-relative path to the target file, e.g. 'src/agent.py'.
        diff_patch:  Unified diff string (output of `diff -u` or LLM-generated).

    Returns a success message or a descriptive error — never raises.
    """
    root = Path(vault_root).expanduser().resolve()
    target = root / file_path

    if not target.exists():
        return (
            f"❌ File not found: {file_path}\n"
            f"Tip: use list_code_files or view_file_lines to confirm the path."
        )

    # Normalise the diff: strip 'a/' 'b/' prefixes so `patch -p1` works on
    # both '--- a/src/agent.py' and '--- src/agent.py' style headers.
    normalised = _normalise_diff_headers(diff_patch, file_path)

    # Write patch to a temp file so we can run `patch` against it
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8"
        ) as pf:
            pf.write(normalised)
            patch_path = pf.name

        # Dry-run first
        dry = subprocess.run(
            ["patch", "--dry-run", "-p1", str(target)],
            input=normalised,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if dry.returncode != 0:
            return (
                f"❌ Patch validation failed — file NOT modified.\n"
                f"patch stderr:\n{dry.stderr.strip()[:800]}\n\n"
                f"Common causes:\n"
                f"  • Context lines don't match — use view_file_lines to get exact text\n"
                f"  • Wrong --- / +++ header paths\n"
                f"  • Hunk offset mismatch (@@ line numbers)"
            )

        # Apply for real
        real = subprocess.run(
            ["patch", "-p1", str(target)],
            input=normalised,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if real.returncode == 0:
            lines_changed = _count_diff_lines(diff_patch)
            return (
                f"✅ Patch applied to {file_path} "
                f"(+{lines_changed['added']} -{lines_changed['removed']} lines)\n"
                f"Next: run_tests to verify nothing broke, then git_commit."
            )
        return f"❌ Patch apply failed:\n{real.stderr.strip()[:800]}"

    except FileNotFoundError:
        # `patch` binary not found — fall back to Python implementation
        return _python_apply_diff(target, file_path, diff_patch)
    except subprocess.TimeoutExpired:
        return "❌ patch command timed out"
    except Exception as e:
        return f"❌ apply_diff error: {e}"
    finally:
        try:
            os.unlink(patch_path)
        except Exception:
            pass


def _normalise_diff_headers(patch: str, file_path: str) -> str:
    """Strip 'a/' / 'b/' prefixes from diff headers if present."""
    lines = patch.splitlines()
    out   = []
    for line in lines:
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            line = line[:4] + line[6:]
        out.append(line)
    return "\n".join(out) + "\n"


def _count_diff_lines(patch: str) -> dict:
    added = removed = 0
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"added": added, "removed": removed}


def _python_apply_diff(target: Path, file_path: str, patch_text: str) -> str:
    """
    Pure-Python unified diff applier — used when the `patch` binary is absent
    (e.g. Windows without Git Bash).  Handles single-hunk diffs reliably;
    multi-hunk diffs are applied sequentially.
    """
    try:
        original = target.read_text(encoding="utf-8", errors="replace")
        orig_lines = original.splitlines(keepends=True)
        result_lines = list(orig_lines)
        offset = 0  # accumulated line-number shift from previous hunks

        hunks = _parse_hunks(patch_text)
        if not hunks:
            return "❌ No valid hunks found in patch. Check diff format."

        for hunk in hunks:
            start_line = hunk["old_start"] - 1 + offset  # 0-indexed
            old_block  = hunk["old_lines"]
            new_block  = hunk["new_lines"]

            # Verify context matches
            actual = result_lines[start_line: start_line + len(old_block)]
            actual_text = "".join(actual).rstrip("\n")
            expected    = "".join(old_block).rstrip("\n")
            if actual_text != expected:
                return (
                    f"❌ Hunk context mismatch at line {hunk['old_start']}.\n"
                    f"Expected:\n{''.join(old_block[:3])}\n"
                    f"Got:\n{''.join(actual[:3])}\n"
                    f"Use view_file_lines to confirm exact content."
                )
            # Apply the hunk
            result_lines[start_line: start_line + len(old_block)] = [
                l if l.endswith("\n") else l + "\n" for l in new_block
            ]
            offset += len(new_block) - len(old_block)

        target.write_text("".join(result_lines), encoding="utf-8")
        lc = _count_diff_lines(patch_text)
        return (
            f"✅ Patch applied (Python fallback) to {file_path} "
            f"(+{lc['added']} -{lc['removed']} lines)\n"
            f"Next: run_tests to verify, then git_commit."
        )
    except Exception as e:
        return f"❌ Python diff apply error: {e}"


def _parse_hunks(patch_text: str) -> list[dict]:
    """Extract hunk metadata and line lists from a unified diff."""
    hunks = []
    hunk  = None
    for line in patch_text.splitlines(keepends=True):
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            if hunk:
                hunks.append(hunk)
            hunk = {
                "old_start": int(m.group(1)),
                "new_start": int(m.group(2)),
                "old_lines": [],
                "new_lines": [],
            }
        elif hunk is not None:
            if line.startswith("-"):
                hunk["old_lines"].append(line[1:])
            elif line.startswith("+"):
                hunk["new_lines"].append(line[1:])
            elif line.startswith(" "):
                hunk["old_lines"].append(line[1:])
                hunk["new_lines"].append(line[1:])
    if hunk:
        hunks.append(hunk)
    return hunks


# ── str_replace_file ──────────────────────────────────────────────────────────

def str_replace_file(
    vault_root: str,
    file_path:  str,
    old_str:    str,
    new_str:    str,
) -> str:
    """
    Replace an exact string in a vault file.

    old_str MUST appear EXACTLY ONCE.  If it appears 0 times the file is
    unchanged and an error is returned.  If it appears 2+ times the tool
    refuses — use apply_diff for ambiguous replacements.

    Args:
        vault_root: Absolute path to the vault root.
        file_path:  Vault-relative path, e.g. 'src/agent.py'.
        old_str:    Exact string to find (including whitespace/indentation).
        new_str:    Replacement string (empty string = delete old_str).

    Returns a success message or an error — never raises.
    """
    root   = Path(vault_root).expanduser().resolve()
    target = root / file_path

    if not target.exists():
        return (
            f"❌ File not found: {file_path}\n"
            f"Tip: use view_file_lines to confirm the exact path."
        )
    if not old_str:
        return "❌ old_str cannot be empty."

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        count   = content.count(old_str)

        if count == 0:
            # Helpful debugging: find the nearest matching lines
            needle = old_str.strip()[:40]
            hits   = [i+1 for i, l in enumerate(content.splitlines()) if needle.lower() in l.lower()]
            hint   = f"  Lines containing '{needle}': {hits[:5]}" if hits else ""
            return (
                f"❌ old_str not found in {file_path} (0 occurrences).\n"
                f"  Make sure whitespace and indentation match exactly.\n"
                f"{hint}\n"
                f"  Use view_file_lines to copy the exact text."
            )

        if count > 1:
            # Show which line numbers contain the string so caller can be more specific
            lines_with_match = [
                i + 1 for i, l in enumerate(content.splitlines())
                if old_str.split("\n")[0].strip() in l
            ]
            return (
                f"❌ old_str appears {count} times in {file_path} — refusing to replace.\n"
                f"  Matching region starts near line(s): {lines_with_match[:8]}\n"
                f"  Add more surrounding context to old_str to make it unique,\n"
                f"  or use apply_diff instead."
            )

        new_content = content.replace(old_str, new_str, 1)
        target.write_text(new_content, encoding="utf-8")

        # Brief summary of what changed
        old_lines = old_str.count("\n") + 1
        new_lines = new_str.count("\n") + 1 if new_str else 0
        delta     = new_lines - old_lines
        delta_str = f"+{delta}" if delta >= 0 else str(delta)
        return (
            f"✅ str_replace_file: replaced 1 occurrence in {file_path} "
            f"({old_lines}→{new_lines} lines, Δ{delta_str})\n"
            f"Next: run_tests to verify, then git_commit."
        )
    except Exception as e:
        return f"❌ str_replace_file error: {e}"


# ── view_file_lines ───────────────────────────────────────────────────────────

def view_file_lines(
    vault_root: str,
    file_path:  str,
    start_line: int         = 1,
    end_line:   int | None  = None,
) -> str:
    """
    Read a file from the vault with 1-indexed line numbers prefixed.

    Each output line is formatted as:
        {line_number:>6}\\t{content}

    This lets the agent reference exact lines when constructing diffs or
    str_replace_file calls.

    Args:
        vault_root:  Absolute path to the vault root.
        file_path:   Vault-relative path, e.g. 'src/agent.py'.
        start_line:  First line to return (1-indexed, inclusive).  Default 1.
        end_line:    Last line to return (inclusive).  None = end of file.

    Returns numbered content or an error string.
    """
    root   = Path(vault_root).expanduser().resolve()
    target = root / file_path

    # Fuzzy resolution: try relative first, then stem match
    if not target.exists():
        stem   = Path(file_path).stem.lower()
        suffix = Path(file_path).suffix.lower()
        for f in root.rglob(f"*{suffix}"):
            if f.stem.lower() == stem:
                target = f
                break

    if not target.exists():
        return (
            f"File not found: {file_path}\n"
            f"Tip: use list_code_files to confirm the path."
        )

    try:
        all_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        total     = len(all_lines)

        # Clamp to valid range
        s = max(1, start_line) - 1             # 0-indexed slice start
        e = min(total, end_line) if end_line else total  # 0-indexed slice end

        selected  = all_lines[s:e]
        numbered  = [f"{s + i + 1:>6}\t{line}" for i, line in enumerate(selected)]

        header = f"# {file_path}  (lines {s+1}–{s+len(selected)} of {total})\n"
        body   = "\n".join(numbered)

        # Hard cap: avoid flooding the context window
        MAX_OUTPUT = 12_000  # ~3000 tokens
        if len(body) > MAX_OUTPUT:
            body = body[:MAX_OUTPUT] + f"\n…[truncated — showing lines {s+1}–{s+len(selected[:MAX_OUTPUT//50])}]"

        return header + body

    except Exception as e:
        return f"❌ view_file_lines error: {e}"


# ── run_tests ─────────────────────────────────────────────────────────────────

def run_tests(
    vault_root:   str,
    path:         str,
    test_command: str = "",
    timeout:      int = 60,
) -> str:
    """
    Run pytest (or a custom command) for a file or directory in the vault.

    Returns structured output:
      • Pass / fail / error counts
      • Names of failed tests with their short error
      • Full stdout truncated to 3000 chars

    Args:
        vault_root:   Absolute path to the vault root.
        path:         Vault-relative path to test file or directory.
        test_command: Full command to run, e.g. 'pytest -x -q src/'. Default: pytest {path}.
        timeout:      Max seconds before kill. Default 60.

    Returns a formatted summary string — never raises.
    """
    root   = Path(vault_root).expanduser().resolve()
    target = root / path if path else root

    if path and not target.exists():
        return f"❌ Path not found: {path}"

    # Build command
    if test_command:
        cmd = shlex.split(test_command)
    else:
        cmd = ["pytest", str(target), "-v", "--tb=short", "--no-header", "-q"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(root),
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        rc     = result.returncode

        # Parse pytest summary line: "3 passed, 1 failed, 0 errors in 2.34s"
        summary_match = re.search(
            r"(\d+)\s+passed|(\d+)\s+failed|(\d+)\s+error", stdout
        )
        passed  = int(re.search(r"(\d+)\s+passed",  stdout).group(1)) if re.search(r"(\d+)\s+passed",  stdout) else 0
        failed  = int(re.search(r"(\d+)\s+failed",  stdout).group(1)) if re.search(r"(\d+)\s+failed",  stdout) else 0
        errors  = int(re.search(r"(\d+)\s+error",   stdout).group(1)) if re.search(r"(\d+)\s+error",   stdout) else 0

        icon   = "✅" if rc == 0 else "❌"
        header = f"{icon} Tests: {passed} passed, {failed} failed, {errors} errors (exit {rc})"

        # Extract FAILED test names
        failed_names = re.findall(r"^FAILED (.+?)$", stdout, re.MULTILINE)
        fail_section = ""
        if failed_names:
            fail_section = "\n\nFailed tests:\n" + "\n".join(f"  • {n}" for n in failed_names[:10])

        # Full output (truncated)
        full = stdout[:2500] + (f"\n…[{len(stdout)-2500} chars omitted]" if len(stdout) > 2500 else "")
        if stderr:
            full += f"\n\nSTDERR:\n{stderr[:500]}"

        return f"{header}{fail_section}\n\n```\n{full}\n```"

    except subprocess.TimeoutExpired:
        return f"❌ Tests timed out after {timeout}s. Check for infinite loops or missing fixtures."
    except FileNotFoundError:
        return (
            "❌ pytest not found. Install it: pip install pytest\n"
            "Or pass a custom test_command parameter."
        )
    except Exception as e:
        return f"❌ run_tests error: {e}"


# ── browser_fetch ─────────────────────────────────────────────────────────────

def browser_fetch(
    url:       str,
    wait_for:  str = "networkidle",
    max_chars: int = 5000,
) -> str:
    """
    Fetch a static/server-rendered page over HTTPS without browser automation.

    Args:
        url:       Full URL to fetch.
        wait_for:  Retained for backwards-compatible tool calls; ignored.
        max_chars: Max characters of extracted text to return. Default 5000.

    Returns: Clean text content of the page.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    validate_public_url(url)
    return fetch_url(url, max_chars)


# ════════════════════════════════════════════════════════════════════════════════
# EXISTING TOOLS (unchanged from v1 — kept intact)
# ════════════════════════════════════════════════════════════════════════════════

# ── Web Search ────────────────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo. Returns top results as JSON."""
    try:
        r = safe_get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10,
            headers={"User-Agent": "VaultAgent/2.0"},
        )
        r.raise_for_status()
        data    = r.json()
        results = []

        if data.get("AbstractText"):
            results.append({
                "type": "summary", "title": data.get("Heading",""),
                "snippet": data["AbstractText"][:500], "url": data.get("AbstractURL",""),
            })
        for topic in data.get("RelatedTopics",[])[:max_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({"type":"result","snippet":topic["Text"][:300],"url":topic.get("FirstURL","")})
        if data.get("Answer"):
            results.insert(0, {"type":"direct_answer","answer":data["Answer"],"answer_type":data.get("AnswerType","")})
        if results:
            return json.dumps(results, indent=2)
        return _ddg_html_search(query, max_results)

    except Exception as e:
        logger.error(f"Web search error: {e}")
        return f"Search error: {e}"


def _ddg_html_search(query: str, max_results: int = 5) -> str:
    try:
        r = safe_get(
            "https://lite.duckduckgo.com/lite/", params={"q": query}, timeout=10,
            headers={"User-Agent":"Mozilla/5.0 (compatible; VaultAgent/2.0)"},
        )
        from html.parser import HTMLParser

        class _P(HTMLParser):
            def __init__(self):
                super().__init__(); self.results=[]; self._cur={}; self._cap=None
            def handle_starttag(self, tag, attrs):
                attrs=dict(attrs)
                if tag=="a" and "uddg" in attrs.get("href",""):
                    self._cur={"url":attrs["href"],"title":""}; self._cap="title"
                elif tag=="td" and attrs.get("class")=="result-snippet":
                    self._cap="snippet"; self._cur.setdefault("snippet","")
            def handle_data(self, data):
                if self._cap and data.strip():
                    self._cur[self._cap]=(self._cur.get(self._cap,"")+data).strip()
            def handle_endtag(self, tag):
                if tag=="a" and self._cap=="title": self._cap=None
                elif tag=="td" and self._cap=="snippet":
                    self._cap=None
                    if self._cur.get("url"): self.results.append(dict(self._cur)); self._cur={}

        p=_P(); p.feed(r.text)
        results=p.results[:max_results]
        return json.dumps(results, indent=2) if results else "No search results found."
    except Exception as e:
        return f"Search fallback error: {e}"


# ── URL Fetch ──────────────────────────────────────────────────────────────────

def fetch_url(url: str, max_chars: int = 3000) -> str:
    """Fetch a URL and return its text content (stripped of HTML)."""
    try:
        if not url.startswith(("http://","https://")):
            url = "https://" + url
        r = safe_get(
            url, timeout=15,
            headers={"User-Agent":"Mozilla/5.0 (compatible; VaultAgent/2.0)",
                     "Accept":"text/html,application/xhtml+xml"},
        )
        r.raise_for_status()
        text = _strip_html(r.text)
        return text[:max_chars] + ("…" if len(text) > max_chars else "")
    except requests.exceptions.Timeout:   return "Request timed out."
    except requests.exceptions.HTTPError as e: return f"HTTP error: {e}"
    except Exception as e:
        logger.error(f"Fetch URL error: {e}"); return f"Fetch error: {e}"


def _strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    import re as _re
    text = _re.sub(r"<style[^>]*>.*?</style>",  " ", html, flags=_re.DOTALL)
    text = _re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=_re.DOTALL)
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+",     " ", text).strip()
    return text


# ── Weather ────────────────────────────────────────────────────────────────────

def get_weather(location: str, units: str = "imperial") -> str:
    """Get current weather using wttr.in (no API key)."""
    try:
        unit_flag = "u" if units == "imperial" else "m"
        r = safe_get(
            f"https://wttr.in/{quote_plus(location)}",
            params={"format":"j1", unit_flag:""},
            timeout=10, headers={"User-Agent":"VaultAgent/2.0"},
        )
        r.raise_for_status()
        data    = r.json()
        current = data["current_condition"][0]
        area    = data["nearest_area"][0]
        city    = area["areaName"][0]["value"]
        country = area["country"][0]["value"]

        forecasts = []
        for day in data.get("weather",[])[:3]:
            forecasts.append(
                f"  {day['date']}: {day['hourly'][4]['weatherDesc'][0]['value']}, "
                f"{day['mintempF']}–{day['maxtempF']}°F ({day['mintempC']}–{day['maxtempC']}°C)"
            )
        return (
            f"📍 {city}, {country}\n"
            f"🌡 {current['temp_F']}°F ({current['temp_C']}°C) | "
            f"Feels like {current['FeelsLikeF']}°F\n"
            f"🌤 {current['weatherDesc'][0]['value']}\n"
            f"💧 Humidity: {current['humidity']}%\n"
            f"💨 Wind: {current['windspeedMiles']} mph\n\n"
            f"3-Day Forecast:\n" + "\n".join(forecasts)
        )
    except Exception as e:
        logger.error(f"Weather error: {e}"); return f"Weather error: {e}"


# ── Calculator ─────────────────────────────────────────────────────────────────

_SAFE_NAMES = {k: v for k, v in math.__dict__.items() if not k.startswith("_")}
_SAFE_NAMES.update({"abs": abs, "round": round, "min": min, "max": max})

def calculate(expression: str) -> str:
    """Safely evaluate a math expression using AST (no exec)."""
    from calculator import calculate as bounded_calculate
    return bounded_calculate(expression)

def _eval_node(node):
    _ops = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod,
        ast.FloorDiv: operator.floordiv,
        ast.USub: operator.neg, ast.UAdd: operator.pos,
    }
    if isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.BinOp):
        op = _ops.get(type(node.op))
        if not op: raise ValueError(f"Unsupported op: {node.op}")
        return op(_eval_node(node.left), _eval_node(node.right))
    elif isinstance(node, ast.UnaryOp):
        return _ops[type(node.op)](_eval_node(node.operand))
    elif isinstance(node, ast.Call):
        fn = node.func.id if isinstance(node.func, ast.Name) else None
        if fn not in _SAFE_NAMES: raise ValueError(f"Function not allowed: {fn}")
        return _SAFE_NAMES[fn](*[_eval_node(a) for a in node.args])
    elif isinstance(node, ast.Name):
        if node.id in _SAFE_NAMES: return _SAFE_NAMES[node.id]
        raise ValueError(f"Name not allowed: {node.id}")
    else:
        raise ValueError(f"Unsupported node: {type(node)}")


# ── DateTime Utils ─────────────────────────────────────────────────────────────

def datetime_util(operation: str, value: str = "", fmt: str = "") -> str:
    try:
        if operation == "now":
            now = datetime.now()
            return (f"Date: {now.strftime('%A, %B %d %Y')}\n"
                    f"Time: {now.strftime('%H:%M:%S')}\nISO:  {now.isoformat()}")
        elif operation == "add_days":
            return (datetime.fromisoformat(value.strip()) + timedelta(days=int(fmt))).strftime("%Y-%m-%d")
        elif operation == "diff":
            parts = [p.strip() for p in value.split(",")]
            diff  = abs((datetime.fromisoformat(parts[1]) - datetime.fromisoformat(parts[0])).days)
            return f"{diff} days between {parts[0]} and {parts[1]}"
        elif operation == "weekday":
            return datetime.fromisoformat(value.strip()).strftime("%A")
        elif operation == "format":
            return datetime.fromisoformat(value.strip()).strftime(fmt or "%B %d, %Y")
        else:
            return f"Unknown datetime operation: {operation}"
    except Exception as e:
        return f"DateTime error: {e}"


# ── Templater ──────────────────────────────────────────────────────────────────

def apply_template(vault_root: str, template_name: str, new_note_path: str,
                   variables: dict = None) -> str:
    try:
        root = Path(vault_root).expanduser().resolve()
        templates_folder = root / "Templates"
        template_path    = None
        for candidate in [
            templates_folder / template_name,
            templates_folder / f"{template_name}.md",
            root / template_name,
            root / f"{template_name}.md",
        ]:
            if candidate.exists():
                template_path = candidate; break

        if not template_path:
            available = [f.stem for f in templates_folder.glob("*.md")] if templates_folder.exists() else []
            return (f"Template '{template_name}' not found. "
                    f"Available: {', '.join(available) if available else 'none in Templates/'}")

        content = template_path.read_text(encoding="utf-8")
        now     = datetime.now()
        subs    = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"),
                   "datetime": now.isoformat(), "day": now.strftime("%A")}
        if variables:
            subs.update(variables)
        for key, val in subs.items():
            content = content.replace(f"{{{{{key}}}}}", str(val))
        content = content.replace("<% tp.date.now() %>", now.strftime("%Y-%m-%d"))

        dest = root / (new_note_path if new_note_path.endswith(".md") else f"{new_note_path}.md")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return f"✅ Created '{dest.name}' from template '{template_path.stem}'"
    except Exception as e:
        logger.error(f"Templater error: {e}"); return f"Template error: {e}"


# ════════════════════════════════════════════════════════════════════════════════
# WINDOWS / SYSTEM TOOLS (unchanged from v1 — kept for backward compat)
# ════════════════════════════════════════════════════════════════════════════════

import shutil, socket, zipfile, smtplib, ctypes
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def _try_import(name):
    try:
        import importlib; return importlib.import_module(name)
    except ImportError: return None

psutil    = _try_import("psutil")
PIL       = _try_import("PIL")
pyperclip = _try_import("pyperclip")
winotify  = _try_import("winotify")

try:
    import winreg
    _WINREG_AVAILABLE = True
except ImportError:
    _WINREG_AVAILABLE = False

# System info, process management, filesystem tools, clipboard, screenshot,
# network, Windows-specific, and communication tools are preserved verbatim
# from v1 (tools.py lines 215-end).  They are omitted here only to keep this
# diff readable — merge them from v1 tools.py below this comment.
#
# Functions preserved:
#   system_info, list_processes, kill_process, run_script, run_powershell,
#   fs_list, fs_copy, fs_move, fs_delete, fs_zip, fs_unzip,
#   fs_read_text, fs_write_text,
#   clipboard_read, clipboard_write, screenshot,
#   ping, port_scan, http_request, dns_lookup,
#   windows_notify, registry_read, scheduled_tasks,
#   list_windows, focus_window, is_admin,
#   send_email, discord_webhook,
#   sandbox_build_image, sandbox_exec, sandbox_run_file, sandbox_status
#
# These are stable — no changes needed for v2.
