#!/usr/bin/env python3
"""Open one dedicated Playwright profile for owner-controlled manual login."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


PROTECTED_ROOT_ENVS = ("CORE_ROOT", "OBSIDIAN_VAULT", "RESEARCH_REPO")
UNSAFE_PATH_PARTS = {
    "default",
    "default profile",
    "browser profile",
    "profile",
    "public",
    "shared",
    "shared profile",
    "user data",
    "user-data",
}


def _canonical_origin(value: str, *, origin_only: bool = False) -> str:
    """Return a comparison-safe HTTP(S) origin without retaining URL details."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL/origin has an invalid port or structure") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL/origin must be an absolute HTTP or HTTPS address")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL/origin must not contain embedded credentials")
    host = parsed.hostname.rstrip(".").lower()
    if not host:
        raise ValueError("URL/origin must contain a hostname")
    if scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("non-local browser profiles require HTTPS")
    if origin_only and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("allowed origins must contain only scheme, host, and optional port")
    default_port = 443 if scheme == "https" else 80
    formatted_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{formatted_host}" + (f":{port}" if port and port != default_port else "")


def resolve_allowed_origins(url: str, configured=()) -> tuple[str, set[str]]:
    """Validate the explicit start URL and its exact allowed final origins."""
    start_origin = _canonical_origin(url)
    allowed = {_canonical_origin(value, origin_only=True) for value in configured}
    if not allowed:
        allowed = {start_origin}
    if start_origin not in allowed:
        raise ValueError("the start URL origin must be included in --allowed-origin")
    return start_origin, allowed


def validate_profile_path(value: str | Path, environ=None) -> Path:
    """Reject work data, synced storage, and obvious shared browser profiles."""
    environ = os.environ if environ is None else environ
    profile = Path(value).expanduser().resolve()
    folded_parts = {part.casefold().replace("_", "-") for part in profile.parts}
    if any(part.startswith("onedrive") for part in folded_parts):
        raise ValueError("profile directory must not be stored in OneDrive")
    normalized_parts = {part.replace("-", " ") for part in folded_parts}
    if normalized_parts & UNSAFE_PATH_PARTS:
        raise ValueError("profile directory looks shared or like a browser default profile")
    user_root_value = str(environ.get("USERPROFILE", "")).strip()
    if user_root_value and profile == Path(user_root_value).expanduser().resolve():
        raise ValueError("profile directory must not be the user profile root")
    for variable in PROTECTED_ROOT_ENVS:
        raw_root = str(environ.get(variable, "")).strip()
        if not raw_root:
            continue
        root = Path(raw_root).expanduser().resolve()
        if profile == root or root in profile.parents or profile in root.parents:
            raise ValueError(f"profile directory must be outside {variable}")
    return profile


def readiness_report(
    profile: Path,
    start_origin: str,
    final_url: str,
    allowed_origins,
    *,
    confirmed: bool,
) -> dict:
    """Build a cookie-free readiness result after owner confirmation."""
    final_origin = _canonical_origin(final_url)
    if final_origin not in allowed_origins:
        raise ValueError(f"final browser origin {final_origin!r} is not allowed")
    if not confirmed:
        raise ValueError("owner did not confirm that the login and chat composer are ready")
    return {
        "ready": True,
        "profile_dir": str(profile),
        "start_origin": start_origin,
        "final_origin": final_origin,
        "allowed_origins": sorted(allowed_origins),
        "owner_confirmed": True,
        "browser_visible": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="Dedicated profile directory; never share it between accounts")
    parser.add_argument("--url", required=True, help="Explicit HTTPS page to open for manual login")
    parser.add_argument(
        "--allowed-origin",
        "--origin",
        action="append",
        default=[],
        help="Allowed final origin (scheme/host/port only); repeat if needed. Defaults to the --url origin.",
    )
    args = parser.parse_args()
    try:
        profile = validate_profile_path(args.profile)
        start_origin, allowed_origins = resolve_allowed_origins(args.url, args.allowed_origin)
    except ValueError as exc:
        parser.error(str(exc))
    profile.mkdir(parents=True, exist_ok=True)
    # Keep Playwright optional for callers that only use the validation helpers.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=False, accept_downloads=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(args.url, wait_until="domcontentloaded")
            confirmation = input(
                "Complete login manually in the visible browser. Return to the approved chat origin, "
                "confirm its composer is visible, then type READY here: "
            ).strip()
            report = readiness_report(
                profile,
                start_origin,
                page.url,
                allowed_origins,
                confirmed=confirmation == "READY",
            )
        except ValueError as exc:
            parser.error(str(exc))
        finally:
            context.close()
    # This report intentionally contains no cookies, storage state, page text,
    # title, or credentials. Chromium persists authentication inside the profile.
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
