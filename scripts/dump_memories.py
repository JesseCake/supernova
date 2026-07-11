#!/usr/bin/env python3
"""
dump_memories.py — export Supernova's memory stores for inspection.

Run from the project root (same place main.py runs from):

    python dump_memories.py                # uses ./data/memory
    python dump_memories.py /path/to/data/memory

Writes memory_dump.json next to itself and prints a readable report
to stdout. Read-only — makes no changes to either store.
"""

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/memory")

dump = {"facts": [], "summaries": [], "stats": {}}

# ── ChromaDB: long-term facts ─────────────────────────────────────────────────
chroma_path = base / "chromadb"
if chroma_path.exists():
    import chromadb
    client     = chromadb.PersistentClient(path=str(chroma_path))
    collection = client.get_or_create_collection(name="memories")
    result     = collection.get(include=["documents", "metadatas"])

    by_user = defaultdict(list)
    for id_, doc, meta in zip(result["ids"], result["documents"], result["metadatas"]):
        meta  = meta or {}
        entry = {
            "id":      id_,
            "user_id": meta.get("user_id", "?"),
            "tags":    meta.get("tags", ""),
            "content": doc,
        }
        dump["facts"].append(entry)
        by_user[entry["user_id"]].append(entry)

    print(f"═══ ChromaDB facts: {len(dump['facts'])} total ═══")
    for user, entries in sorted(by_user.items()):
        print(f"\n── {user} ({len(entries)}) " + "─" * 40)
        for e in entries:
            tags = f"  [tags: {e['tags']}]" if e["tags"] else ""
            print(f"  • {e['content']}{tags}")
            print(f"    id={e['id']}")
    dump["stats"]["facts_by_user"] = {u: len(v) for u, v in by_user.items()}
else:
    print(f"(no ChromaDB at {chroma_path})")

# ── SQLite: session summaries ─────────────────────────────────────────────────
db_path = base / "history.db"
if db_path.exists():
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    print(f"\n═══ SQLite tables: {', '.join(tables)} ═══")

    summary_table = next((t for t in tables if "summar" in t.lower()), None)
    if summary_table:
        rows = conn.execute(
            f"SELECT * FROM {summary_table} ORDER BY rowid DESC LIMIT 50").fetchall()
        print(f"\n── last {len(rows)} rows of '{summary_table}' " + "─" * 30)
        for r in rows:
            d = dict(r)
            dump["summaries"].append(d)
            preview = {k: (str(v)[:200] + "…" if v and len(str(v)) > 200 else v)
                       for k, v in d.items()}
            print(f"  {json.dumps(preview, ensure_ascii=False)}")
    else:
        print("(no summary-like table found)")

    n_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    dump["stats"]["turn_rows"] = n_turns
    print(f"\nturns table: {n_turns} rows (not dumped — transcripts, large)")
    conn.close()
else:
    print(f"(no SQLite db at {db_path})")

out = Path(__file__).parent / "memory_dump.json"
out.write_text(json.dumps(dump, indent=2, ensure_ascii=False))
print(f"\nWrote {out} — upload this file (or paste the report above).")