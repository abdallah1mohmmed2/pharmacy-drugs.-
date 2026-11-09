#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fast, deterministic drug catalog retrieval — NOT product identity certification.

Hybrid retrieval: Unicode normalization, exact identifier/name hits, field-aware
inverted index (token postings), character-trigram fuzzy candidates, and
verification-aware re-ranking. All searches are read-only and standard-library-only.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import statistics
import time
import unicodedata
from typing import Any

from convert import load_schema
from catalog_details import DETAIL_EXTRA_KEYS

ARABIC_DIACRITICS = re.compile(r"[\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
ARABIC_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه",
    "ك": "ك", "ي": "ي", "پ": "ب", "چ": "ج", "ژ": "ز", "گ": "ك",
})
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
WORD = re.compile(r"[a-z0-9\u0621-\u064a]+")
DOSAGE_MARKER = re.compile(r"\b(\d+(?:\.\d+)?)(mg|mcg|ug|g|ml|iu|%)\b")
INDEXED = (
    ("trade_name_en", 7.0),
    ("trade_name_ar", 7.0),
    ("scientific_name", 5.0),
    ("manufacturer_name", 1.8),
    ("therapeutic_class", 1.4),
    ("pharmacologic_class", 1.4),
    ("strength_text", 2.5),
    ("dosage_form", 1.0),
    ("package_description", 0.8),
)
QUERY_FIELDS = tuple(name for name, _ in INDEXED)
MAX_CANDIDATES = 5000


def normalize(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold().translate(DIGITS)
    text = ARABIC_DIACRITICS.sub("", text).translate(ARABIC_FOLD)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = text.replace("µg", "mcg").replace("μg", "mcg")
    text = re.sub(r"(?<=\d)\s+(?=(?:mg|mcg|ug|g|ml|iu)\b)", "", text)
    return " ".join(WORD.findall(text))


def tokens(text: str) -> frozenset[str]:
    return frozenset(WORD.findall(text))


def trigrams(text: str) -> frozenset[str]:
    compact = text.replace(" ", "")
    if len(compact) < 3:
        return frozenset()
    return frozenset(compact[i:i+3] for i in range(len(compact)-2))


def match_strength(text: str) -> str:
    return next(iter(DOSAGE_MARKER.findall(text)), ())


class DrugSearchIndex:
    """Static catalog index; keep product documents intact, no merge across IDs."""

    def __init__(self, records: list[dict]):
        if not isinstance(records, list) or not records:
            raise ValueError("Expected nonempty converted JSON array")
        base = set(load_schema())
        extended = base | set(DETAIL_EXTRA_KEYS)
        self.records = records
        self.postings: dict[str, set[int]] = defaultdict(set)
        self.grams: dict[str, set[int]] = defaultdict(set)
        self.exact_names: dict[str, set[int]] = defaultdict(set)
        self.barcodes: dict[str, set[int]] = defaultdict(set)
        self.id_to_position: dict[int, int] = {}
        self.normalized = []
        for pos, row in enumerate(records):
            if not isinstance(row, dict) or set(row) not in (base, extended):
                raise ValueError(f"Record {pos} is not the converted 96/98-field JSON schema")
            rid = row.get("id")
            if type(rid) is not int or rid <= 0 or rid in self.id_to_position:
                raise ValueError(f"Invalid or duplicate source id {rid}")
            self.id_to_position[rid] = pos
            fields = {field: normalize(row.get(field, "")) for field in QUERY_FIELDS}
            self.normalized.append(fields)
            all_tokens = frozenset().union(*(tokens(text) for text in fields.values()))
            for token in all_tokens:
                self.postings[token].add(pos)
            for name in ("trade_name_en", "trade_name_ar", "scientific_name"):
                name_text = fields[name]
                if name_text:
                    self.exact_names[name_text].add(pos)
            names = " ".join(fields[n] for n in ("trade_name_en", "trade_name_ar", "scientific_name"))
            for gram in trigrams(names):
                self.grams[gram].add(pos)
            barcode = str(row.get("barcode_international") or "").strip()
            if barcode:
                self.barcodes[barcode].add(pos)

    def _candidates(self, query: str, qtokens: frozenset[str]) -> set[int]:
        if query.isdecimal() and query in self.barcodes:
            return set(self.barcodes[query])
        candidates = set(self.exact_names.get(query, ()))
        postings = [(len(self.postings[t]), t) for t in qtokens if t in self.postings]
        postings.sort()
        # Rare-first postings: selects meaningful words even for long queries.
        for _, token in postings:
            candidates.update(self.postings[token])
            if len(candidates) > MAX_CANDIDATES:
                break
        # Fuzzy fallback for misspellings, only if at least three characters.
        if len(candidates) < 60 and len(query.replace(" ", "")) >= 4:
            grams = trigrams(query)
            if grams:
                counts = defaultdict(int)
                for gram in grams:
                    for pos in self.grams.get(gram, ()):
                        counts[pos] += 1
                threshold = max(2, len(grams) // 5)
                ranked = sorted(
                    ((count, pos) for pos, count in counts.items() if count >= threshold),
                    key=lambda pair: (-pair[0], pair[1]),
                )
                candidates.update(pos for _, pos in ranked[:600])
        return candidates

    def search(self, query: str, *, limit: int = 10, offset: int = 0) -> list[dict]:
        q = normalize(query)
        if not q:
            return []
        if not 1 <= limit <= 100 or not 0 <= offset <= 1000:
            raise ValueError("limit must be 1..100; offset must be 0..1000")
        qtokens = tokens(q)
        candidates = self._candidates(q, qtokens)
        qstrengths = {t for t in qtokens if DOSAGE_MARKER.fullmatch(t)}
        results = []
        for pos in candidates:
            row = self.records[pos]
            fields = self.normalized[pos]
            reasons = []
            score = 0.0
            barcode = str(row.get("barcode_international") or "")
            if q == barcode and barcode:
                score += 10000
                reasons.append("exact_barcode")
            if q.isdecimal() and str(row["id"]) == q:
                score += 9000
                reasons.append("exact_source_id")
            for field, weight in INDEXED:
                value = fields[field]
                if not value:
                    continue
                if q == value:
                    score += 190 * weight
                    reasons.append("exact_" + field)
                elif len(q) >= 3 and value.startswith(q):
                    score += 55 * weight
                    reasons.append("prefix_" + field)
                elif len(q) >= 3 and q in value:
                    score += 22 * weight
                    reasons.append("contains_" + field)
                overlap = qtokens & tokens(value)
                if overlap:
                    score += 17 * weight * (len(overlap) / max(len(qtokens), 1))
                    if not any(r.endswith(field) for r in reasons):
                        reasons.append("tokens_" + field)
            if score == 0 and len(q) >= 4:
                names = (fields["trade_name_en"], fields["trade_name_ar"], fields["scientific_name"])
                similarity = max(SequenceMatcher(None, q, n).ratio() for n in names)
                if similarity >= 0.52:
                    score = similarity * 120
                    reasons.append("fuzzy_name")
            if qstrengths:
                known_strengths = set(tokens(fields["strength_text"]))
                # A dose/strength mismatch excludes the record from exact-product confidence.
                if known_strengths and not qstrengths.intersection(known_strengths):
                    score -= 200
                    reasons.append("different_strength")
            if score <= 0:
                continue
            results.append((round(score, 2), pos, reasons))
        results.sort(key=lambda item: (-item[0], self.records[item[1]]["id"]))
        answer = []
        for score, pos, reasons in results[offset:offset+limit]:
            row = self.records[pos]
            answer.append({
                "id": row["id"], "uuid": row["uuid"],
                "trade_name_en": row.get("trade_name_en", ""),
                "trade_name_ar": row.get("trade_name_ar", ""),
                "scientific_name": row.get("scientific_name", ""),
                "strength_text": row.get("strength_text", ""),
                "dosage_form": row.get("dosage_form", ""),
                "route": row.get("route", ""),
                "manufacturer_name": row.get("manufacturer_name", ""),
                "score": score, "match_reasons": reasons,
                "source": "converted_catalog",
                "clinical_product_equivalence_verified": False,
            })
        return answer


def load_index(source_path: Path):
    with source_path.open(encoding="utf-8") as stream:
        return DrugSearchIndex(json.load(stream))


def benchmark(index: DrugSearchIndex, queries: list[str]) -> dict:
    latencies = []
    found = 0
    start = time.perf_counter()
    for query in queries:
        t0 = time.perf_counter()
        result = index.search(query, limit=10)
        latencies.append((time.perf_counter()-t0)*1000)
        found += bool(result)
    total = time.perf_counter() - start
    sorted_times = sorted(latencies)
    return {
        "queries": len(queries),
        "returned_results_for": found,
        "total_search_seconds": round(total, 3),
        "queries_per_second": round(len(queries)/max(total,1e-9), 1),
        "p50_latency_ms": round(statistics.median(latencies), 2),
        "p95_latency_ms": round(sorted_times[min(len(latencies)-1, int(0.95*len(latencies)))],2),
        "note": "Hardware-dependent local retrieval benchmark, not external API speed",
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Fast multilingual hybrid search over the CONVERTED drug JSON")
    p.add_argument("--input", required=True)
    p.add_argument("--query", default="")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--benchmark", action="store_true")
    p.add_argument("--benchmark-count", type=int, default=200)
    p.add_argument("--output", default="", help="Optional new JSON output path")
    args = p.parse_args(argv)
    if not args.query and not args.benchmark:
        p.error("Provide --query or --benchmark")
    t0 = time.perf_counter()
    index = load_index(Path(args.input))
    report = {"indexed_drugs": len(index.records), "index_build_seconds": round(time.perf_counter()-t0,3)}
    if args.query:
        report["query"] = args.query
        report["matches"] = index.search(args.query, limit=args.limit)
    if args.benchmark:
        if not 1 <= args.benchmark_count <= 10000:
            p.error("benchmark-count must be 1..10000")
        steps = max(len(index.records)//args.benchmark_count, 1)
        queries = []
        for row in index.records[::steps]:
            q = row.get("trade_name_en") or row.get("trade_name_ar")
            if q:
                queries.append(str(q))
            if len(queries) >= args.benchmark_count:
                break
        if not queries:
            p.error("No names for benchmark")
        report["benchmark"] = benchmark(index, queries)
    if args.output:
        out = Path(args.output)
        if out.exists():
            p.error("Output exists; refusing overwrite")
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
