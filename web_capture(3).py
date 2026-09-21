#!/usr/bin/env python3
"""
web_capture.py — pull structured data out of a website's rendered UI (not
just raw HTML — this drives a real browser, so JS-rendered pages work) and
append it to a local capture feed, in the same {id, tag, timestamp, source,
payload} shape as the rest of your system.

This is a CONFIGURED extractor, not a blind crawler: you tell it exactly
which CSS selectors on which pages to read. It does not log in as anyone,
does not solve CAPTCHAs, does not bypass paywalls or bot protection, and
checks robots.txt before touching a site — if a site disallows the path,
it skips that recipe and tells you, rather than working around it.
For anything requiring your own login, use record_login.py first to save
YOUR OWN session (see below) — this never handles anyone else's credentials.

Before pointing this at a given site: check that site's terms of service.
robots.txt compliance is a courtesy check, not a legal green light — some
sites prohibit automated collection in their ToS even where robots.txt is
silent. When in doubt, prefer that site's official API/RSS feed instead
(cheaper, faster, and unambiguous permission).

Setup:
    pip install playwright
    python3 -m playwright install chromium

Usage:
    python3 web_capture.py --config recipes.json
    python3 web_capture.py --config recipes.json --only hn_frontpage
    python3 web_capture.py --config recipes.json --interval 3600   # poll hourly

Feed captures into core_server's shared memory (optional, in addition to
the local captures.jsonl file, which is always written regardless):
    export CORE_URL=http://127.0.0.1:5077
    export CORE_API_KEY=<same value as core_server's CORE_API_KEY>
    python3 web_capture.py --config recipes.json

Config file (recipes.json) — see EXAMPLE_CONFIG at the bottom of this file
for the exact shape.
"""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.robotparser
import uuid
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Optional: if set, every captured entry is also POSTed to core_server's
# /api/ingest, in addition to the local captures.jsonl write below (the
# JSONL write always happens regardless — this is additive, never a
# replacement, so a core_server outage never loses data, just delays it
# showing up in shared memory until you re-run against the JSONL later).
CORE_URL = os.environ.get("CORE_URL", "")
CORE_API_KEY = os.environ.get("CORE_API_KEY", "")


def ingest_to_core(entry: dict):
    if not CORE_URL:
        return
    try:
        req = urllib.request.Request(
            CORE_URL.rstrip("/") + "/api/ingest",
            data=json.dumps(entry).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": CORE_API_KEY},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  [core_server ingest failed, entry still saved locally to jsonl: {e}]")


def check_robots_allowed(url: str, user_agent: str = "web_capture/1.0") -> bool:
    """Best-effort robots.txt check. file:// and unreachable robots.txt both
    default to allowed (with a note) rather than blocking silently."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=8) as resp:
            rp.parse(resp.read().decode(errors="replace").splitlines())
    except Exception:
        print(f"  [robots.txt unreachable at {robots_url}, proceeding cautiously]")
        return True
    allowed = rp.can_fetch(user_agent, url)
    if not allowed:
        print(f"  [robots.txt disallows {url} — skipping this recipe]")
    return allowed


def _extract_value(scope, spec, timeout_ms=5000):
    """Field spec can be a CSS string (text) or {selector, attr}."""
    selector = spec if isinstance(spec, str) else (spec or {}).get("selector", "")
    attr = None if isinstance(spec, str) else (spec or {}).get("attr")
    if not selector:
        return None
    try:
        loc = scope.locator(selector).first
        if not loc.count():
            return None
        if attr:
            return loc.get_attribute(attr, timeout=timeout_ms)
        value = loc.text_content(timeout=timeout_ms)
        return value.strip() if value else None
    except PWTimeout:
        return None


def extract_single(page, fields: dict) -> dict:
    return {key: _extract_value(page, spec, 5000) for key, spec in fields.items()}


def extract_list(page, item_selector: str, fields: dict, limit: int = 30) -> list:
    items = page.locator(item_selector)
    n = min(items.count(), limit)
    results = []
    for i in range(n):
        item = items.nth(i)
        results.append({key: _extract_value(item, spec, 2000) for key, spec in fields.items()})
    return results


def _post_core(path: str, body: dict):
    if not CORE_URL:
        return None
    req = urllib.request.Request(
        CORE_URL.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": CORE_API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode() or "{}")


def register_world_recipe(recipe: dict):
    if not CORE_URL or not recipe.get("world_context"):
        return
    try:
        _post_core("/api/world/feeds/register", {
            "name": recipe.get("world_feed") or recipe["name"],
            "source_type": recipe.get("source_type", "web"),
            "locator": recipe.get("url", ""),
            "topics": recipe.get("topics") or [recipe.get("tag", recipe["name"])],
            "region": recipe.get("region", ""),
            "base_reliability": recipe.get("base_reliability", 0.5),
            "bias_notes": recipe.get("bias_notes", ""),
            "config": {"collector": "web_capture", "recipe": recipe["name"]},
        })
    except Exception as e:
        print(f"  [world feed registration failed: {e}]")


def ingest_world_payload(recipe: dict, payload: dict, captured_at: str):
    if not CORE_URL or not recipe.get("world_context"):
        return
    mapping = recipe.get("world_map") or {}
    def val(logical, default=""):
        key = mapping.get(logical, logical)
        return payload.get(key, default) if key else default
    text = val("text") or val("summary") or val("title") or json.dumps(payload, ensure_ascii=False)
    try:
        _post_core("/api/world/ingest", {
            "feed": recipe.get("world_feed") or recipe["name"],
            "source_type": recipe.get("source_type", "web"),
            "external_id": str(val("external_id") or val("id") or uuid.uuid4()),
            "timestamp": val("timestamp") or captured_at,
            "title": val("title"),
            "text": text,
            "url": val("url") or recipe.get("url", ""),
            "author": val("author"),
            "topics": recipe.get("topics") or [recipe.get("tag", recipe["name"])],
            "geo": val("geo", {}) if isinstance(val("geo", {}), dict) else {},
            "metadata": {"collector": "web_capture", "recipe": recipe["name"], "raw": payload},
        })
    except Exception as e:
        print(f"  [world-context ingest failed; ordinary event still saved: {e}]")


def run_recipe(browser, recipe: dict, out_path: Path):
    name = recipe["name"]
    url = recipe["url"]
    tag = recipe.get("tag", name)
    mode = recipe.get("mode", "single")
    wait_for = recipe.get("wait_for")
    storage_state = recipe.get("storage_state")

    print(f"[{name}] {url}")
    if not check_robots_allowed(url):
        return 0
    register_world_recipe(recipe)

    context = browser.new_context(storage_state=storage_state) if storage_state else browser.new_context()
    page = context.new_page()
    try:
        page.goto(url, timeout=20000, wait_until="domcontentloaded")
        if wait_for:
            page.wait_for_selector(wait_for, timeout=10000)

        if mode == "single":
            payload = extract_single(page, recipe["fields"])
            payloads = [payload]
        elif mode == "list":
            payloads = extract_list(page, recipe["item_selector"], recipe["fields"],
                                     limit=recipe.get("limit", 30))
        else:
            raise ValueError(f"unknown mode: {mode}")

        now = datetime.now(timezone.utc).isoformat()
        with open(out_path, "a") as f:
            for payload in payloads:
                entry = {
                    "id": str(uuid.uuid4()),
                    "tag": tag,
                    "timestamp": now,
                    "source": {"recipe": name, "url": url},
                    "payload": payload,
                }
                f.write(json.dumps(entry) + "\n")
                ingest_to_core(entry)
                ingest_world_payload(recipe, payload, now)
        print(f"  captured {len(payloads)} entr{'y' if len(payloads)==1 else 'ies'}" +
              (f", pushed to {CORE_URL}" if CORE_URL else ""))
        return len(payloads)
    finally:
        context.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="path to recipes.json")
    ap.add_argument("--out", default="captures.jsonl", help="append-only output file")
    ap.add_argument("--only", help="run a single recipe by name")
    ap.add_argument("--interval", type=int, default=0,
                     help="if set, re-run every N seconds instead of once")
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text())
    recipes = config["recipes"]
    if args.only:
        recipes = [r for r in recipes if r["name"] == args.only]
        if not recipes:
            raise SystemExit(f"no recipe named '{args.only}' in {args.config}")

    out_path = Path(args.out)

    def run_all():
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            total = 0
            for recipe in recipes:
                try:
                    total += run_recipe(browser, recipe, out_path)
                except Exception as e:
                    print(f"[{recipe['name']}] failed: {e}")
            browser.close()
        return total

    if args.interval > 0:
        while True:
            run_all()
            print(f"sleeping {args.interval}s...")
            time.sleep(args.interval)
    else:
        run_all()


EXAMPLE_CONFIG = {
    "recipes": [
        {
            "name": "hn_frontpage",
            "url": "https://news.ycombinator.com/",
            "tag": "hn",
            "mode": "list",
            "item_selector": ".athing",
            "fields": {"title": ".titleline > a", "url": {"selector": ".titleline > a", "attr": "href"}},
            "limit": 20,
            "world_context": False
        },
        {
            "name": "my_dashboard_status",
            "url": "https://example-internal-tool.your-tailnet.ts.net/status",
            "tag": "infra",
            "mode": "single",
            "wait_for": "#status",
            "fields": {"status": "#status", "last_updated": "#last-updated"}
        }
    ]
}

if __name__ == "__main__":
    main()
