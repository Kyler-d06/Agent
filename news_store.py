"""Exact-news deduplication with retained source provenance and old-ID aliases.

We intentionally preserve differing numbers, negation, punctuation and short
headlines. Similar coverage is not sufficient evidence to delete a story.
"""
import hashlib
import html
import json
import re
import sqlite3


def fingerprint(text):
    normalized = " ".join(html.unescape(re.sub(r"</?[A-Za-z][A-Za-z0-9]*(?:\s+[^<>]*)?\s*/?>", " ", text or "")).split())
    return hashlib.sha256(normalized.encode()).hexdigest() if len(normalized) >= 40 else None


def source_key(item):
    eid = str(item.get("external_id") or "")
    # Preserve corrected/edited posts even when the publisher reuses an ID.
    identity = [item.get("feed", ""), eid or item.get("url", ""), item.get("title", ""), item.get("text", "")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def provenance(db, item, canonical_id):
    db.execute("INSERT OR IGNORE INTO world_item_sources(source_key,world_item_id,feed,external_id,url,record_json) VALUES(?,?,?,?,?,?)",
               (source_key(item), canonical_id, item.get("feed", ""), str(item.get("external_id") or ""), item.get("url", ""), json.dumps(item, ensure_ascii=False)))
    if item.get("id"):
        db.execute("INSERT OR REPLACE INTO world_item_aliases VALUES(?,?)", (item["id"], canonical_id))


def initialize(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS world_item_sources (
        source_key TEXT PRIMARY KEY, world_item_id TEXT NOT NULL,
        feed TEXT, external_id TEXT, url TEXT, record_json TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS world_sources_item ON world_item_sources(world_item_id);
      CREATE TABLE IF NOT EXISTS world_item_aliases (original_id TEXT PRIMARY KEY, world_item_id TEXT NOT NULL);
    """)
    # The unique index also marks completion. Migration and deletions are atomic.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='world_content_unique' AND type='index'").fetchone():
        return 0
    db.execute("BEGIN IMMEDIATE")
    try:
        if "content_hash" not in {r[1] for r in db.execute("PRAGMA table_info(world_items)")}:
            db.execute("ALTER TABLE world_items ADD COLUMN content_hash TEXT")
        cursor = db.execute("SELECT * FROM world_items ORDER BY created_at,id")
        columns = [c[0] for c in cursor.description]
        items = [dict(zip(columns, r)) for r in cursor.fetchall()]
        hashes, sources, removed = {}, {}, 0
        for item in items:
            key, content_hash = source_key(item), fingerprint(item.get("text"))
            canonical_id = sources.get(key) or (hashes.get(content_hash) if content_hash else None)
            canonical_id = canonical_id or item["id"]
            provenance(db, item, canonical_id)
            sources[key] = canonical_id
            if content_hash:
                hashes[content_hash] = canonical_id
            if canonical_id != item["id"]:
                db.execute("DELETE FROM world_items WHERE id=?", (item["id"],))
                removed += 1
            else:
                db.execute("UPDATE world_items SET content_hash=? WHERE id=?", (content_hash, item["id"]))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS world_content_unique ON world_items(content_hash) WHERE content_hash IS NOT NULL")
        db.commit()
        return removed
    except Exception:
        db.rollback()
        raise


def existing(db, item):
    row = db.execute("SELECT world_item_id FROM world_item_sources WHERE source_key=?", (source_key(item),)).fetchone()
    if not row and fingerprint(item.get("text")):
        row = db.execute("SELECT id FROM world_items WHERE content_hash=?", (fingerprint(item["text"]),)).fetchone()
    return row[0] if row else None
