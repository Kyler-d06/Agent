"""Conservative selector recovery using element metadata, never page instructions."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    "input_selector": ["textarea", "[contenteditable='true'][role='textbox']", "[contenteditable='true'][data-lexical-editor='true']", ".ProseMirror[contenteditable='true']", "[contenteditable='true']", "input[type='text']"],
    "submit_selector": ["button[type='submit']", "button[aria-label*='Send Message' i]", "button[aria-label*='Send' i]", "button[data-testid*='send' i]"],
    "response_selector": ["[data-message-author-role='assistant']", "[data-role='assistant']", ".font-claude-response", "article"],
    "response_text_selector": ["[data-testid*='answer' i]", "[class*='markdown']", "[class*='response']"],
    "completion_selector": ["button[aria-label*='Copy' i]", "button[data-testid*='copy' i]", "[data-complete='true']"],
    "new_chat_selector": ["button[aria-label*='New chat' i]", "a[href*='new']"],
}
TOKENS = {
    "input_selector": ("prompt", "message", "ask", "chat"),
    "submit_selector": ("send", "submit", "ask", "go"),
    "response_selector": ("assistant", "answer", "response", "message"),
    "response_text_selector": ("answer", "response", "markdown", "content"),
    "completion_selector": ("copy", "complete", "finished", "done"),
    "new_chat_selector": ("new", "chat", "conversation"),
}


def _heal_path(provider):
    configured = provider.get("selector_state_file")
    return Path(configured) if configured else Path(provider["profile_dir"]) / ".agent-selector-heals.json"


def load_heals(provider):
    try:
        value = json.loads(_heal_path(provider).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_heal(provider, key, selector):
    path = _heal_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = load_heals(provider)
    value[key] = selector
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def inventory(scope):
    """Return bounded structural metadata. Inner text and values are excluded."""
    try:
        return scope.locator("textarea,input,button,article,[role],[contenteditable]").evaluate_all("""els => els.slice(0,80).map(e => ({
          tag:e.tagName.toLowerCase(), id:e.id||'', role:e.getAttribute('role')||'',
          type:e.getAttribute('type')||'', aria:e.getAttribute('aria-label')||'',
          placeholder:e.getAttribute('placeholder')||'', testid:e.getAttribute('data-testid')||'',
          author:e.getAttribute('data-message-author-role')||'', contenteditable:e.getAttribute('contenteditable')||''
        }))""")
    except Exception:
        return []


def candidate_selectors(scope, key, configured=(), allow_empty=False):
    # A configured response/completion selector can legitimately have no match
    # before generation starts.  Generic recovery candidates cannot: accepting
    # every zero-count default made the first guessed selector "win" even when
    # it did not exist on the page.
    configured_candidates = [x for x in configured if x]
    candidates = list(configured_candidates)
    candidates.extend(DEFAULTS.get(key, ()))
    tokens = TOKENS.get(key, ())
    for element in inventory(scope):
        blob = " ".join(str(element.get(k, "")) for k in ("id", "role", "type", "aria", "placeholder", "testid", "author")).lower()
        if not any(token in blob for token in tokens):
            continue
        for attr in ("testid", "aria", "placeholder", "id", "author"):
            value = element.get(attr)
            if not value:
                continue
            css_attr = {"testid": "data-testid", "aria": "aria-label", "author": "data-message-author-role"}.get(attr, attr)
            candidates.append(f"[{css_attr}={json.dumps(value)}]")
            break
    unique = []
    for selector in candidates:
        if selector in unique:
            continue
        try:
            count = scope.locator(selector).count()
            configured_empty = allow_empty and count == 0 and selector in configured_candidates
            if count == 1 or configured_empty or (key in {"response_selector"} and count > 0):
                unique.append(selector)
        except Exception:
            continue
    return unique


def locate(scope, provider, key, *, visible=True, allow_empty=False):
    heals = load_heals(provider)
    # Prefer the current owner configuration over a previously healed selector
    # when an empty match is allowed.  A stale heal must not mask the declared
    # dynamic-response selector merely because both currently match zero nodes.
    declared = [provider.get(key), *(provider.get(key + "_fallbacks") or [])]
    configured = [*declared, heals.get(key)] if allow_empty else [heals.get(key), *declared]
    for selector in candidate_selectors(scope, key, configured, allow_empty):
        locator = scope.locator(selector)
        try:
            if allow_empty or not visible or locator.first.is_visible():
                if selector != provider.get(key):
                    save_heal(provider, key, selector)
                return locator, selector
        except Exception:
            continue
    raise RuntimeError(f"unable to locate {key}; structural candidates={inventory(scope)[:20]}")
