#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fast, auditable local search of the *converted* 96/98-field drug catalogue.

SQLite FTS5 Unicode index + weighted BM25 + trigram typo retrieval + exact ID/
GTIN lookup. The index returns source records unchanged; results are CANDIDATES
only and do not establish pharmaceutical equivalence, safety or substitutability.
Python standard library; no runtime internet or third-party packages.
"""
from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from time import perf_counter
import unicodedata

from catalog_details import DETAIL_EXTRA_KEYS
from convert import load_schema

INDEX_VERSION = "hybrid-1.0"
MAX_QUERY_CHARS = 160
MAX_RESULTS = 50
# Words such as "mg", "tablets" add false positives to fuzzy spelling matches.
FUZZY_SKIP = frozenset(("mg", "mcg", "ml", "tab", "tabs", "tablet",
                        "tablets", "capsule", "capsules", "syrup", "cream"))
STRENGTH_RE = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)\s*(mg|mcg|ug|µg|iu|%)(?!\w)", re.I)
ARABIC_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي",
    "ؤ": "و", "ئ": "ي", "ة": "ه", "ـ": "",
})


def norm(value) -> str:
    """Normalize Arabic diacritics, Arabic-Indic numerals and Latin accents."""
    value = unicodedata.normalize("NFKD", str(value or "")).casefold().translate(ARABIC_FOLD)
    value = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c
                    for c in value if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", value, flags=re.UNICODE).strip()


def strength_key(value):
    m = STRENGTH_RE.search(str(value or "").casefold())
    if not m:
        return ""
    n = f"{float(m.group(1)):g}"
    unit = m.group(2).replace("µg", "mcg").replace("ug", "mcg")
    return n + unit


def _source_rows(path: Path):
    if path.stat().st_size > 250 * 1024 * 1024:
        raise ValueError("Converted file exceeds 250 MiB safety limit")
    with path.open(encoding="utf-8") as fh:
        rows = json.load(fh)
    if not isinstance(rows, list) or not rows:
        raise ValueError("Input must be a nonempty JSON array of converted drug records")
    return rows


def build_index(source_path, db_path):
    """Build a complete replacement SQLite index atomically, never overwrite drafts."""
    source_path, db_path = Path(source_path), Path(db_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    source_rows = _source_rows(source_path)
    allowed = {frozenset(load_schema()), frozenset(load_schema()) | frozenset(DETAIL_EXTRA_KEYS)}
    shape = frozenset(source_rows[0])
    if shape not in allowed:
        raise ValueError("Source rows must have exactly the converted 96 or 98 fields")
    ids, uuids = set(), set()
    for idx, row in enumerate(source_rows, 1):
        if not isinstance(row, dict) or frozenset(row) != shape:
            raise ValueError(f"Malformed or mixed converted records at row {idx}")
        rid, uid = row.get("id"), row.get("uuid")
        if type(rid) is not int or rid <= 0 or rid in ids:
            raise ValueError(f"Invalid/duplicate drug ID at row {idx}: {rid!r}")
        if not isinstance(uid, str) or not uid or uid in uuids:
            raise ValueError(f"Invalid/duplicate drug UUID at row {idx}: {uid!r}")
        ids.add(rid)
        uuids.add(uid)
    if db_path.resolve() == source_path.resolve():
        raise ValueError("Index path must not be the converted input JSON")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".pharmacy-index-", suffix=".sqlite3", dir=db_path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with sqlite3.connect(temp_path) as con:
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("""CREATE TABLE records(
                id INTEGER PRIMARY KEY, uuid TEXT NOT NULL UNIQUE,
                barcode TEXT, brand_key TEXT, ingredient_key TEXT,
                strength_key TEXT, doc_json TEXT NOT NULL)""")
            con.execute("CREATE INDEX barcode_lookup ON records(barcode)")
            con.execute("CREATE INDEX exact_brand_lookup ON records(brand_key)")
            con.execute("CREATE INDEX exact_ingredient_lookup ON records(ingredient_key)")
            try:
                con.execute("""CREATE VIRTUAL TABLE lex USING fts5(
                    brand, brand_ar, ingredient, manufacturer, category,
                    tokenize='unicode61 remove_diacritics 2',
                    prefix='2 3 4')""")
            except sqlite3.OperationalError as exc:
                raise RuntimeError("SQLite FTS5 is unavailable; use a Python build with FTS5") from exc
            # Trigram FTS5 is an optional typo-retrieval index, not an entire
            # database scan. If unavailable, exact and BM25 search still work.
            trigram_enabled = True
            try:
                con.execute("CREATE VIRTUAL TABLE fuzzy USING fts5(text, tokenize='trigram')")
            except sqlite3.OperationalError:
                trigram_enabled = False
            sql_records = []
            sql_lex = []
            sql_fuzzy = []
            for row in source_rows:
                rid = row["id"]
                brand = norm(row.get("trade_name_en"))
                arabic = norm(row.get("trade_name_ar"))
                ingredient = norm(row.get("scientific_name"))
                manufacturer = norm(row.get("manufacturer_name"))
                category = norm(row.get("therapeutic_class"))
                barcode = str(row.get("barcode_international") or row.get("barcode_local") or "").strip()
                if barcode and not barcode.isdecimal():
                    barcode = ""
                doc = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                sql_records.append((rid, row["uuid"], barcode, brand, ingredient,
                                    strength_key(row.get("strength_text")), doc))
                sql_lex.append((rid, brand, arabic, ingredient, manufacturer, category))
                if trigram_enabled:
                    sql_fuzzy.append((rid, " ".join([brand, arabic, ingredient]).strip()))
            con.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?)", sql_records)
            con.executemany("INSERT INTO lex(rowid,brand,brand_ar,ingredient,manufacturer,category) VALUES(?,?,?,?,?,?)", sql_lex)
            if trigram_enabled:
                con.executemany("INSERT INTO fuzzy(rowid,text) VALUES(?,?)", sql_fuzzy)
            con.execute("CREATE TABLE info(key TEXT PRIMARY KEY, val TEXT NOT NULL)")
            meta = {
                "index_version": INDEX_VERSION,
                "records": str(len(source_rows)),
                "input_fields": str(len(shape)),
                "trigram_enabled": json.dumps(trigram_enabled),
                "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            }
            con.executemany("INSERT INTO info VALUES (?,?)", meta.items())
            con.commit()
            actual_count = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            indexed_count = con.execute("SELECT COUNT(*) FROM lex").fetchone()[0]
            if actual_count != len(source_rows) or indexed_count != actual_count:
                raise ValueError("FTS5 catalogue indexing lost or duplicated records")
        os.replace(temp_path, db_path)
        return {k: (int(v) if k in ("records", "input_fields") else json.loads(v) if k == "trigram_enabled" else v)
                for k, v in meta.items()}
    finally:
        temp_path.unlink(missing_ok=True)


def _fts_query(tokens, *, relaxed=False):
    # Escaped token phrases: no raw user-supplied MATCH operators or SQL.
    clean = [t for t in tokens if t][:8]
    if not clean:
        return ""
    quoted = ['"' + t.replace('"', '""') + '"' for t in clean]
    if relaxed:
        return " OR ".join(quoted)
    if len(quoted) == 1 and len(clean[0]) >= 2:
        return quoted[0] + "*"
    return " AND ".join(quoted)


def _fuzzy_query(tokens):
    grams = []
    for token in tokens:
        if len(token) < 4 or token in FUZZY_SKIP:
            continue
        for j in range(len(token)-2):
            gram = token[j:j+3]
            if gram not in grams:
                grams.append(gram)
            if len(grams) >= 20:
                break
        if len(grams) >= 20:
            break
    return " OR ".join('"' + g.replace('"','""') + '"' for g in grams)


def search_index(index_path, query, *, limit=10, full_record=False):
    """Rank exact identifiers first, BM25 next, bounded trigram typo candidates last.

    The resulting object is NEVER a pharmacy safety decision, prescribing
    instruction or an indication that two products can be substituted.
    """
    if type(query) is not str or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"Search must be nonempty text of <= {MAX_QUERY_CHARS} characters")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValueError("limit must be 1..50")
    raw = query.strip()
    q = norm(raw)
    tokens = q.split()
    if not tokens:
        return []
    found: dict[int, tuple[float, str]] = {}

    def put(rid, score, mode):
        if rid not in found or score > found[rid][0]:
            found[rid] = (score, mode)

    # Read-only connection keeps the built index immutable during searches.
    uri = Path(index_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        # Exact lookup uses B-tree indexes even if a term has 2-3 characters.
        if raw.isdecimal():
            numeric = int(raw)
            matching_barcodes = [r["id"] for r in con.execute(
                "SELECT id FROM records WHERE barcode = ? ORDER BY id", (raw,)
            )]
            for rid in matching_barcodes:
                if len(matching_barcodes) == 1:
                    put(rid, 1200, "exact_barcode")
                else:
                    # A GTIN re-used by multiple source records is AMBIGUOUS,
                    # even if each barcode passes the check digit algorithm.
                    put(rid, 830, "shared_barcode_needs_review")
            if numeric > 0:
                for row in con.execute("SELECT id FROM records WHERE id = ?", (numeric,)):
                    put(row["id"], 1100, "exact_id")
        for row in con.execute("SELECT id FROM records WHERE brand_key = ? LIMIT 25", (q,)):
            put(row["id"], 600, "exact_brand")
        for row in con.execute("SELECT id FROM records WHERE ingredient_key = ? LIMIT 40", (q,)):
            put(row["id"], 460, "exact_ingredient")

        q_strict = _fts_query(tokens)
        if q_strict:
            for rank, row in enumerate(con.execute(
                "SELECT rowid FROM lex WHERE lex MATCH ? ORDER BY bm25(lex, 8.0, 8.0, 5.0, 2.0, 1.0) LIMIT 100",
                (q_strict,),
            )):
                put(row["rowid"], 320 - rank * 1.5, "fts5_bm25")
        # Relaxed query helps to retrieve partially typed catalog names but
        # must never be used to assert that ingredients/products match.
        if len(found) < limit:
            relaxed = _fts_query(tokens, relaxed=True)
            for rank, row in enumerate(con.execute(
                "SELECT rowid FROM lex WHERE lex MATCH ? ORDER BY bm25(lex, 8.0, 8.0, 5.0, 2.0, 1.0) LIMIT 90",
                (relaxed,),
            )):
                put(row["rowid"], 170 - rank, "fts5_partial")

        if len(found) < limit:
            fuzzy = _fuzzy_query(tokens)
            if fuzzy:
                try:
                    for rank, row in enumerate(con.execute(
                        "SELECT rowid FROM fuzzy WHERE fuzzy MATCH ? ORDER BY bm25(fuzzy) LIMIT 90",
                        (fuzzy,),
                    )):
                        put(row["rowid"], 80 - rank / 2, "trigram_candidate")
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc):
                        raise
        if not found:
            return []
        out = []
        for rid, (base, mode) in found.items():
            row = con.execute(
                "SELECT doc_json, brand_key, ingredient_key, strength_key FROM records WHERE id=?", (rid,)
            ).fetchone()
            drug = json.loads(row["doc_json"])
            texts = [row["brand_key"], norm(drug.get("trade_name_ar")), row["ingredient_key"]]
            similarity = max((SequenceMatcher(None, q, t).ratio() for t in texts if t), default=0)
            score = base + similarity * 45
            if row["brand_key"].startswith(q):
                score += 70
            elif row["ingredient_key"].startswith(q):
                score += 35
            query_strength = strength_key(raw)
            strength_conflict = bool(query_strength and row["strength_key"]
                                     and query_strength != row["strength_key"])
            if strength_conflict:
                score -= 250
            result = {
                "id": rid,
                "uuid": drug.get("uuid"),
                "trade_name_en": drug.get("trade_name_en"),
                "trade_name_ar": drug.get("trade_name_ar"),
                "scientific_name": drug.get("scientific_name"),
                "strength_text": drug.get("strength_text"),
                "dosage_form": drug.get("dosage_form"),
                "route": drug.get("route"),
                "barcode_international": drug.get("barcode_international"),
                "manufacturer_name": drug.get("manufacturer_name"),
                "match_method": mode,
                "ranking_score": round(score, 3),
                "strength_conflict": strength_conflict,
                "clinical_equivalence_verified": False,
                "barcode_uniqueness_verified": mode == "exact_barcode",
            }
            if full_record:
                result["record"] = drug
            out.append(result)
        out.sort(key=lambda x: (-x["ranking_score"], x["id"]))
        return out[:limit]


def main(argv=None):
    p = argparse.ArgumentParser(description="Search converted pharmacy drugs using BM25 + Unicode FTS5 + trigrams")
    p.add_argument("--index", required=True, help="SQLite search-index filename")
    p.add_argument("--build-from", help="Full converted drugs_96_DRAFT.json, to build an index")
    p.add_argument("--query", help="Trade name, scientific name, Arabic name, GTIN, or drug ID")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--full-record", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.build_from:
            start = perf_counter()
            meta = build_index(args.build_from, args.index)
            print(json.dumps({"build_seconds": round(perf_counter()-start, 3), **meta},
                             ensure_ascii=False), file=sys.stderr)
        if args.query:
            results = search_index(args.index, args.query, limit=args.limit, full_record=args.full_record)
            print(json.dumps({"query":args.query, "result_count":len(results),
                              "results":results, "medical_data_reviewed":False},
                             ensure_ascii=False, indent=2))
        elif not args.build_from:
            p.error("Supply --build-from and/or --query")
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print("SEARCH FAILED:", str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
