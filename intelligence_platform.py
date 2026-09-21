#!/usr/bin/env python3
"""
intelligence_platform.py — world-feed and knowledge-ingestion compatibility layer.

Owns exactly what core_server.py already assumes exists: the world_items /
knowledge_items / source_feeds / models tables (queried directly in
/api/state and /api/search), plus the routes world_sources.py and
web_capture.py already call. Model routing, structured memory, independent
evaluations and versioned skills live in universal_platform.py and its modules.
"""
from __future__ import annotations
import json, sqlite3, uuid
import news_store

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_feeds (
    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, source_type TEXT, locator TEXT,
    topics_json TEXT DEFAULT '[]', region TEXT, base_reliability REAL DEFAULT 0.5,
    bias_notes TEXT, config_json TEXT DEFAULT '{}', enabled INTEGER DEFAULT 1,
    last_cursor TEXT, last_seen_at TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS world_items (
    id TEXT PRIMARY KEY, feed TEXT, source_type TEXT, external_id TEXT, ts TEXT,
    title TEXT, text TEXT, url TEXT, author TEXT, topics_json TEXT DEFAULT '[]',
    geo_json TEXT DEFAULT '{}', metadata_json TEXT DEFAULT '{}', created_at TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_items (
    id TEXT PRIMARY KEY, source_type TEXT, title TEXT, source_url TEXT, artifact_path TEXT,
    topics_json TEXT DEFAULT '[]', text TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS models (
    name TEXT PRIMARY KEY, provider TEXT, enabled INTEGER DEFAULT 1,
    success_rate REAL DEFAULT 0.0, runs INTEGER DEFAULT 0, latency_ema_ms REAL DEFAULT 0.0,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_world_items_feed ON world_items(feed);
"""

INTELLIGENCE_TOOLS = [
    {"name": "world_item_sources", "description": "Get retained source provenance for a canonical or deduplicated news item ID.",
     "method": "GET", "path": "/api/world/item/sources", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "list_world_feeds", "description": "List registered world-context feeds (RSS/Telegram/X/web_capture) and whether each is enabled.",
     "method": "GET", "path": "/api/world/feeds", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "list_models", "description": "List enabled model routes and their empirical success rate / latency.",
     "method": "GET", "path": "/api/models", "input_schema": {"type": "object", "properties": {}, "required": []}},
]


class IntelligencePlatform:
    def __init__(self, *, app, db_path, get_db, ok, err, logged_tool, now_iso, publish_event=None):
        self.db_path, self.get_db, self.ok, self.err, self.now_iso = db_path, get_db, ok, err, now_iso
        self.publish_event = publish_event
        db = sqlite3.connect(db_path)
        db.executescript(SCHEMA)
        db.commit()
        self.duplicates_removed = news_store.initialize(db)
        db.close()
        self._register_routes(app, logged_tool)

    def _register_routes(self, app, logged_tool):
        from flask import request
        db = self.get_db

        @app.route("/api/world/feeds/register", methods=["POST"])
        @logged_tool("register_world_feed")
        def register_feed():
            d = request.get_json(force=True)
            name = (d.get("name") or "").strip()
            if not name:
                return self.err("name required")
            now = self.now_iso()
            existing = db().execute("SELECT id FROM source_feeds WHERE name=?", (name,)).fetchone()
            if existing:
                db().execute(
                    "UPDATE source_feeds SET source_type=?,locator=?,topics_json=?,region=?,base_reliability=?,bias_notes=?,config_json=?,enabled=?,updated_at=? WHERE name=?",
                    (d.get("source_type", ""), d.get("locator", ""), json.dumps(d.get("topics") or []),
                     d.get("region", ""), d.get("base_reliability", 0.5), d.get("bias_notes", ""),
                     json.dumps(d.get("config") or {}), int(d.get("enabled", True)), now, name))
            else:
                db().execute(
                    "INSERT INTO source_feeds (id,name,source_type,locator,topics_json,region,base_reliability,bias_notes,config_json,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), name, d.get("source_type", ""), d.get("locator", ""),
                     json.dumps(d.get("topics") or []), d.get("region", ""), d.get("base_reliability", 0.5),
                     d.get("bias_notes", ""), json.dumps(d.get("config") or {}), int(d.get("enabled", True)), now, now))
            db().commit()
            return self.ok({"name": name})

        @app.route("/api/world/ingest", methods=["POST"])
        @logged_tool("ingest_world_item")
        def ingest_world():
            d = request.get_json(force=True)
            feed = d.get("feed", "")
            external_id = str(d.get("external_id", ""))
            d["id"] = str(uuid.uuid4())
            # Serialize lookup + insert across collectors to prevent duplicate races.
            db().execute("BEGIN IMMEDIATE")
            dup = news_store.existing(db(), d)
            if dup:
                d.pop("id")  # No public ID was issued; avoid an alias per repeated poll.
                news_store.provenance(db(), d, dup)
                db().execute("UPDATE source_feeds SET last_seen_at=? WHERE name=?", (self.now_iso(), feed))
                db().commit()
                return self.ok({"duplicate": True, "id": dup})
            iid = d["id"]
            db().execute(
                "INSERT INTO world_items (id,feed,source_type,external_id,ts,title,text,url,author,topics_json,geo_json,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, feed, d.get("source_type", ""), external_id, d.get("timestamp", self.now_iso()),
                 d.get("title", ""), d.get("text", ""), d.get("url", ""), d.get("author", ""),
                 json.dumps(d.get("topics") or []), json.dumps(d.get("geo") or {}),
                 json.dumps(d.get("metadata") or {}), self.now_iso()))
            db().execute("UPDATE world_items SET content_hash=? WHERE id=?", (news_store.fingerprint(d.get("text")), iid))
            news_store.provenance(db(), d, iid)
            db().execute("UPDATE source_feeds SET last_seen_at=? WHERE name=?", (self.now_iso(), feed))
            db().commit()
            if self.publish_event:
                self.publish_event("world.ingested", tag="world", source={"type": "world_context", "feed": feed},
                                    payload={"world_item_id": iid, "feed": feed, "title": d.get("title", "")})
            return self.ok({"id": iid})

        @app.route("/api/world/item/sources")
        @logged_tool("world_item_sources")
        def item_sources():
            iid = request.args.get("id", "")
            alias = db().execute("SELECT world_item_id FROM world_item_aliases WHERE original_id=?", (iid,)).fetchone()
            iid = alias[0] if alias else iid
            rows = db().execute("SELECT feed,external_id,url,record_json FROM world_item_sources WHERE world_item_id=?", (iid,)).fetchall()
            return self.ok({"id": iid, "sources": [dict(r) for r in rows]})

        # Runtime-only endpoints for the collector scripts' cursor bookkeeping.
        # Not advertised as LLM tools, same convention as mesh_platform.py's /api/owner/* routes.
        @app.route("/api/runtime/world/feed")
        def runtime_feed_state():
            name = request.args.get("name", "")
            row = db().execute("SELECT last_cursor,last_seen_at FROM source_feeds WHERE name=?", (name,)).fetchone()
            return self.ok(dict(row) if row else {})

        @app.route("/api/runtime/world/feed/cursor", methods=["POST"])
        def runtime_set_cursor():
            d = request.get_json(force=True)
            db().execute("UPDATE source_feeds SET last_cursor=?,last_seen_at=? WHERE name=?",
                         (str(d.get("cursor", "")), d.get("last_seen_at") or self.now_iso(), d.get("feed", "")))
            db().commit()
            return self.ok({"feed": d.get("feed")})

        @app.route("/api/world/feeds")
        @logged_tool("list_world_feeds")
        def list_feeds():
            rows = db().execute("SELECT name,source_type,enabled,last_seen_at FROM source_feeds ORDER BY name").fetchall()
            return self.ok([dict(r) for r in rows])

        @app.route("/api/models")
        @logged_tool("list_models")
        def list_models():
            rows = db().execute("SELECT * FROM models WHERE enabled=1 ORDER BY runs DESC").fetchall()
            return self.ok([dict(r) for r in rows])


__all__ = ["IntelligencePlatform", "INTELLIGENCE_TOOLS", "SCHEMA"]
