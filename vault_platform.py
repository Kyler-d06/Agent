"""Instance-scoped Obsidian and utility tools, exposed through the core gateway."""
import inspect
import os
from pathlib import Path

from vault import ObsidianVault
import tools as utilities

VAULT_METHODS = (
    "search_notes", "read_note", "read_file", "write_note", "append_to_note",
    "list_folder", "list_code_files", "search_code", "get_tasks", "add_task",
    "complete_task", "get_daily_note", "dataview_query", "get_related_notes",
)
WRITES = {"write_note", "append_to_note", "add_task", "complete_task", "get_daily_note"}
UTILITY_FNS = {"utility_web_search": utilities.web_search, "fetch_url": utilities.fetch_url,
               "get_weather": utilities.get_weather, "calculate": utilities.calculate,
               "datetime_util": utilities.datetime_util}


def _spec(name, fn, effect):
    props, required = {}, []
    for arg, p in inspect.signature(fn).parameters.items():
        if arg == "self":
            continue
        typ = {str: "string", int: "integer", float: "number", bool: "boolean"}.get(p.annotation, "string")
        props[arg] = {"type": [typ, "null"] if p.default is None else typ}
        if p.default is inspect.Parameter.empty:
            required.append(arg)
    return {"name": name, "description": (fn.__doc__ or name).strip().splitlines()[0],
            "method": "POST", "path": f"/api/vault/{name}", "effect": effect,
            "requires_confirmation": effect == "write",
            "input_schema": {"type": "object", "properties": props,
                             "required": required, "additionalProperties": False}}


# Prefix vault operations to avoid collisions with core read_file/web_search.
# Network utilities retrieve information; missing sandbox stubs are not advertised.
VAULT_TOOLS = [_spec("vault_" + name, getattr(ObsidianVault, name), "write" if name in WRITES else "read")
               for name in VAULT_METHODS] + [_spec(name, fn, "read") for name, fn in UTILITY_FNS.items()]


class VaultPlatform:
    def __init__(self, app, ok, err, logged_tool, vault_path=None):
        from flask import request
        from platform_contracts import validate
        root = Path(vault_path or os.environ.get("OBSIDIAN_VAULT", str(Path(os.environ.get("CORE_ROOT", ".")) / "obsidian")))
        root.mkdir(parents=True, exist_ok=True)
        self.vault = ObsidianVault(str(root))
        functions = {"vault_" + n: getattr(self.vault, n) for n in VAULT_METHODS} | UTILITY_FNS
        for spec in VAULT_TOOLS:
            name = spec["name"]
            def view(fn=functions[name], schema=spec["input_schema"]):
                try:
                    args = request.get_json(silent=True) or {}
                    validate(schema, args)
                    return ok(fn(**args))
                except Exception as exc:
                    return err(str(exc), 400)
            view.__name__ = name
            app.add_url_rule(spec["path"], view_func=logged_tool(name)(view), methods=["POST"])
