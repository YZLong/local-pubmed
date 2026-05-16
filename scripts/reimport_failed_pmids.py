#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from pathlib import Path

from import_pubmed_to_es import (
    DEFAULT_ES_URL,
    find_text,
    iter_pubmed_articles,
    json_dumps,
    parse_doc,
    request_json,
    request_ndjson,
)


ERROR_LOG_RE = re.compile(r"^\.?import_errors_(?P<index>.+)_\d+\.jsonl$")
PMID_RE = re.compile(r"document with id '([^']+)'")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read .import error logs, find failed PubMed PMIDs in local XML files, "
            "and optionally re-index only those missing documents."
        )
    )
    parser.add_argument("--es-url", default=DEFAULT_ES_URL)
    parser.add_argument("--error-log", action="append", default=[], help="Error JSONL file. Defaults to .import_errors_*.jsonl.")
    parser.add_argument("--baseline-input", action="append", default=[], help="Baseline XML directory. Repeatable.")
    parser.add_argument("--updatefiles-input", action="append", default=[], help="Updatefiles XML directory. Repeatable.")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--manifest-out", help="Optional path to write the failed-PMID manifest JSON.")
    parser.add_argument("--scan", action="store_true", help="Dry-run scan XML files and report which failed PMIDs can be found locally.")
    parser.add_argument("--execute", action="store_true", help="Actually write missing PMIDs to Elasticsearch. Without this, no ES writes happen.")
    parser.add_argument("--overwrite", action="store_true", help="Write failed PMIDs even if they already exist in Elasticsearch.")
    return parser.parse_args()


def default_logs() -> list[Path]:
    return sorted(Path(".").glob(".import_errors_*.jsonl"))


def index_from_log(path: Path) -> str:
    match = ERROR_LOG_RE.match(path.name)
    if not match:
        raise ValueError(f"Cannot infer index name from error log: {path}")
    return match.group("index")


def source_dataset_for_index(index: str) -> str:
    lowered = index.lower()
    if "baseline" in lowered:
        return "baseline"
    if "update" in lowered:
        return "updatefiles"
    return "custom"


def candidate_dirs(args: argparse.Namespace, source_dataset: str) -> list[Path]:
    if source_dataset == "baseline":
        values = args.baseline_input or ["data/pubmed/baseline", "data/baseline"]
    elif source_dataset == "updatefiles":
        values = args.updatefiles_input or ["data/pubmed/updatefiles", "data/updatefiles"]
    else:
        values = [*args.baseline_input, *args.updatefiles_input]
    return [Path(value) for value in values]


def expand_xml_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file() and path.name.endswith(".xml.gz"):
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.glob("*.xml.gz")))
    return sorted(dict.fromkeys(files))


def load_failed_pmids(log_paths: list[Path]) -> dict[str, set[str]]:
    failed: dict[str, set[str]] = {}
    for path in log_paths:
        index = index_from_log(path)
        failed.setdefault(index, set())
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                match = PMID_RE.search(line)
                if match:
                    failed[index].add(match.group(1))
    return failed


def existing_pmids(es_url: str, index: str, pmids: set[str]) -> set[str]:
    if not pmids:
        return set()
    result = request_json("POST", f"{es_url}/{index}/_mget", {"ids": sorted(pmids)})
    return {doc["_id"] for doc in result.get("docs", []) if doc.get("found")}


def find_failed_docs(files: list[Path], wanted_pmids: set[str], source_dataset: str, index: str) -> tuple[dict[str, dict], set[str]]:
    docs: dict[str, dict] = {}
    remaining = set(wanted_pmids)
    imported_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()

    for file_path in files:
        if not remaining:
            break
        for article in iter_pubmed_articles(file_path):
            pmid = find_text(article, "./MedlineCitation/PMID")
            if pmid not in remaining:
                continue
            doc = parse_doc(article, str(file_path), imported_at, source_dataset, index)
            if doc:
                docs[pmid] = doc
            remaining.discard(pmid)
            if not remaining:
                break

    return docs, remaining


def bulk_index(es_url: str, index: str, docs: dict[str, dict], batch_size: int) -> int:
    failures = 0
    lines: list[str] = []
    error_path = Path(f".import_repair_errors_{index}_{int(time.time())}.jsonl")
    with error_path.open("w", encoding="utf-8") as error_log:
        for doc in docs.values():
            lines.append(json_dumps({"index": {"_index": index, "_id": doc["pmid"]}}))
            lines.append(json_dumps(doc, ensure_ascii=False))
            if len(lines) >= batch_size * 2:
                failures += flush_repair_bulk(es_url, lines, error_log)
                lines = []
        if lines:
            failures += flush_repair_bulk(es_url, lines, error_log)
    if failures == 0:
        error_path.unlink(missing_ok=True)
    return failures


def flush_repair_bulk(es_url: str, lines: list[str], error_log) -> int:
    result = request_ndjson(f"{es_url}/_bulk", lines)
    if not result.get("errors"):
        return 0
    failures = 0
    for item in result.get("items", []):
        error = item.get("index", {}).get("error")
        if error:
            failures += 1
            error_log.write(json_dumps(error, ensure_ascii=False) + "\n")
    return failures


def main() -> int:
    args = parse_args()
    log_paths = [Path(path) for path in args.error_log] or default_logs()
    if not log_paths:
        raise SystemExit("No .import_errors_*.jsonl files found.")

    failed_by_index = load_failed_pmids(log_paths)
    manifest: dict[str, dict] = {}
    total_written = 0
    total_failures = 0

    for index, failed_pmids in sorted(failed_by_index.items()):
        source_dataset = source_dataset_for_index(index)
        target_pmids = set(failed_pmids)
        existing: set[str] = set()

        if args.execute and not args.overwrite:
            existing = existing_pmids(args.es_url, index, failed_pmids)
            target_pmids -= existing

        files = expand_xml_files(candidate_dirs(args, source_dataset))
        docs: dict[str, dict] = {}
        not_found_in_xml: set[str] = set()
        if args.scan or args.execute:
            docs, not_found_in_xml = find_failed_docs(files, target_pmids, source_dataset, index)

        if args.execute and docs:
            failures = bulk_index(args.es_url, index, docs, args.batch_size)
            request_json("POST", f"{args.es_url}/{index}/_refresh")
            total_written += len(docs)
            total_failures += failures
        else:
            failures = 0

        manifest[index] = {
            "source_dataset": source_dataset,
            "candidate_failed_pmids": sorted(failed_pmids),
            "candidate_count": len(failed_pmids),
            "existing_skipped_pmids": sorted(existing),
            "to_repair_pmids": sorted(target_pmids),
            "to_repair_count": len(target_pmids),
            "xml_files_scanned": len(files) if (args.scan or args.execute) else 0,
            "found_in_xml_pmids": sorted(docs),
            "not_found_in_xml_pmids": sorted(not_found_in_xml),
            "written": len(docs) if args.execute else 0,
            "bulk_failures": failures,
        }

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if args.manifest_out:
        Path(args.manifest_out).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if total_failures:
        return 2
    return 0 if (not args.execute or total_written >= 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
