#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import tarfile
import time
from pathlib import Path
from typing import Any

from lxml import etree

from import_pubmed_to_es import (
    DEFAULT_ES_URL,
    build_es_date,
    drop_empty,
    json_dumps,
    parse_month,
    request_json,
    request_ndjson,
    wait_for_es,
)


DEFAULT_INDEX = "pmc_articles_v1"
DEFAULT_ALIAS = "pmc_articles"
DEFAULT_INPUT = "data/pmc/oa_bulk"
DEFAULT_MAPPING = "config/pmc_index.json"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# 413 时每次减半，最小不低于此值
MIN_BATCH_SIZE = 10


def text_of(elem) -> str | None:
    if elem is None:
        return None
    value = " ".join(part.strip() for part in elem.itertext() if part and part.strip())
    return value or None


def find_text(elem, path: str) -> str | None:
    return text_of(elem.find(path)) if elem is not None else None


def parse_date(parent) -> dict[str, Any] | None:
    if parent is None:
        return None
    iso_date = parent.attrib.get("iso-8601-date")
    if iso_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso_date):
        year, month, day = iso_date.split("-")
        date_value, granularity = build_es_date(int(year), month, day)
        if date_value:
            return {"date": date_value, "year": int(year), "month": int(month), "day": int(day), "granularity": granularity}

    year_raw = find_text(parent, "year")
    if not year_raw or not year_raw[:4].isdigit():
        return None
    year = int(year_raw[:4])
    month = parse_month(find_text(parent, "month"))
    day = parse_day(find_text(parent, "day"))
    date_value, granularity = build_es_date(year, month, day)
    return drop_empty({
        "date": date_value,
        "year": year,
        "month": int(month) if month else None,
        "day": int(day) if day else None,
        "granularity": granularity,
    })


def parse_day(value: str | None) -> str | None:
    if not value or not value.strip().isdigit():
        return None
    day = int(value.strip())
    if 1 <= day <= 31:
        return f"{day:02d}"
    return None


def best_pub_date(article_meta) -> tuple[str | None, int | None, str | None]:
    candidates = article_meta.findall("pub-date") if article_meta is not None else []
    preferred = ["epub", "ppub", "collection", "pmc-release"]
    for date_type in preferred:
        for pub_date in candidates:
            if pub_date.attrib.get("pub-type") == date_type:
                parsed = parse_date(pub_date)
                if parsed:
                    return parsed.get("date"), parsed.get("year"), date_type
    for pub_date in candidates:
        parsed = parse_date(pub_date)
        if parsed:
            return parsed.get("date"), parsed.get("year"), pub_date.attrib.get("pub-type")
    return None, None, None


def article_ids(article_meta) -> tuple[list[dict[str, str]], dict[str, str]]:
    entries = []
    by_type = {}
    if article_meta is None:
        return entries, by_type
    for node in article_meta.findall("article-id"):
        value = text_of(node)
        id_type = node.attrib.get("pub-id-type")
        if not value or not id_type:
            continue
        entries.append({"type": id_type, "value": value})
        by_type.setdefault(id_type, value)
    return entries, by_type


def parse_authors(article_meta) -> tuple[list[dict[str, Any]], list[str]]:
    authors = []
    if article_meta is None:
        return authors, []
    for position, contrib in enumerate(article_meta.findall("contrib-group/contrib"), start=1):
        if contrib.attrib.get("contrib-type") != "author":
            continue
        surname = find_text(contrib, "name/surname")
        given_names = find_text(contrib, "name/given-names")
        full_name = " ".join(part for part in [given_names, surname] if part) or find_text(contrib, "collab")
        if not full_name:
            continue
        authors.append(drop_empty({
            "position": position,
            "surname": surname,
            "given_names": given_names,
            "full_name": full_name,
            "orcid": find_text(contrib, "contrib-id[@contrib-id-type='orcid']"),
            "corresponding": contrib.attrib.get("corresp") == "yes",
        }))
    return authors, [author["full_name"] for author in authors if author.get("full_name")]


def parse_journal(front) -> dict[str, Any]:
    journal_meta = front.find("journal-meta") if front is not None else None
    if journal_meta is None:
        return {}
    ids = {
        node.attrib.get("journal-id-type"): text_of(node)
        for node in journal_meta.findall("journal-id")
        if node.attrib.get("journal-id-type") and text_of(node)
    }
    return drop_empty({
        "title": find_text(journal_meta, "journal-title-group/journal-title"),
        "nlm_ta": ids.get("nlm-ta"),
        "iso_abbreviation": ids.get("iso-abbrev"),
        "publisher": find_text(journal_meta, "publisher/publisher-name"),
        "issn_print": find_text(journal_meta, "issn[@pub-type='ppub']"),
        "issn_electronic": find_text(journal_meta, "issn[@pub-type='epub']"),
    })


def parse_license(article_meta) -> dict[str, Any]:
    license_node = article_meta.find("permissions/license") if article_meta is not None else None
    if license_node is None:
        return {}
    return drop_empty({
        "href": license_node.attrib.get(XLINK_HREF) or find_text(license_node, ".//license_ref"),
        "text": text_of(license_node),
    })


def parse_sections(body) -> tuple[list[dict[str, str]], list[str], str | None]:
    sections = []
    if body is None:
        return sections, [], None
    for sec in body.findall(".//sec"):
        title = find_text(sec, "title")
        text = text_of(sec)
        if title and text and text.startswith(title):
            text = text[len(title):].strip()
        if text:
            sections.append(drop_empty({
                "id": sec.attrib.get("id"),
                "type": sec.attrib.get("sec-type"),
                "title": title,
                "text": text,
            }))
    section_titles = sorted({section["title"] for section in sections if section.get("title")})
    body_text = text_of(body)
    return sections, section_titles, body_text


def parse_doc(root, archive_path: Path, member_name: str, imported_at: str, source_dataset: str) -> dict[str, Any] | None:
    front = root.find("front")
    article_meta = front.find("article-meta") if front is not None else None
    ids, ids_by_type = article_ids(article_meta)
    pmcid = ids_by_type.get("pmc")
    if not pmcid:
        stem = Path(member_name).stem
        pmcid = stem if stem.upper().startswith("PMC") else None
    if not pmcid:
        return None

    authors, author_names = parse_authors(article_meta)
    abstract = text_of(article_meta.find("abstract")) if article_meta is not None else None
    sections, section_titles, body_text = parse_sections(root.find("body"))
    publication_date, publication_year, publication_date_type = best_pub_date(article_meta)
    title = find_text(article_meta, "title-group/article-title")
    full_text = "\n\n".join(part for part in [title, abstract, body_text] if part)

    return drop_empty({
        "pmcid": pmcid,
        "pmc_num": int(pmcid[3:]) if pmcid.upper().startswith("PMC") and pmcid[3:].isdigit() else None,
        "pmid": ids_by_type.get("pmid"),
        "doi": ids_by_type.get("doi"),
        "article_ids": ids,
        "article_type": root.attrib.get("article-type"),
        "language": root.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") or root.attrib.get("xml:lang"),
        "title": title,
        "abstract": abstract,
        "body_text": body_text,
        "full_text": full_text,
        "sections": sections,
        "section_titles": section_titles,
        "authors": authors,
        "author_names": author_names,
        "journal": parse_journal(front),
        "publication_date": publication_date,
        "publication_year": publication_year,
        "publication_date_type": publication_date_type,
        "volume": find_text(article_meta, "volume"),
        "issue": find_text(article_meta, "issue"),
        "fpage": find_text(article_meta, "fpage"),
        "lpage": find_text(article_meta, "lpage"),
        "elocation_id": find_text(article_meta, "elocation-id"),
        "subjects": sorted({text_of(subject) for subject in article_meta.findall(".//subject") if text_of(subject)}) if article_meta is not None else [],
        "license": parse_license(article_meta),
        "source_archive": archive_path.name,
        "source_member": member_name,
        "source_file": Path(member_name).name,
        "source_dataset": source_dataset,
        "imported_at": imported_at,
    })


def iter_archive_docs(archive_path: Path, imported_at: str, source_dataset: str):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, recover=True, huge_tree=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".xml"):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            try:
                root = etree.parse(io.BytesIO(data), parser).getroot()
            except etree.XMLSyntaxError:
                continue
            doc = parse_doc(root, archive_path, member.name, imported_at, source_dataset)
            if doc:
                yield doc


def expand_archives(input_path: str) -> list[Path]:
    path = Path(input_path)
    if path.is_file():
        return [path]
    return sorted(path.glob("*.tar.gz"))


def ensure_index(es_url: str, index: str, alias: str, mapping_path: str, recreate: bool) -> None:
    if recreate:
        try:
            request_json("DELETE", f"{es_url}/{index}")
            print(f"Deleted existing index {index}")
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
    try:
        request_json("HEAD", f"{es_url}/{index}")
        exists = True
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        exists = False
    if not exists:
        with open(mapping_path, encoding="utf-8") as fh:
            request_json("PUT", f"{es_url}/{index}", json.load(fh))
        print(f"Created index {index}")
    if alias:
        request_json("POST", f"{es_url}/_aliases", {"actions": [{"add": {"index": index, "alias": alias}}]})
        print(f"Alias {alias} includes {index}")


def flush_bulk(es_url: str, lines: list[str], batch_size: int) -> tuple[int, int]:
    """
    发送 bulk 请求，遇到 413 时自动减半重试，直到 MIN_BATCH_SIZE。

    返回 (failures, effective_batch_size)。
    effective_batch_size 可能比传入的 batch_size 小，调用方应据此调整后续批次大小。
    """
    current_size = batch_size
    while True:
        # 按 current_size 切分（lines 是 action+doc 对，每对占 2 行）
        chunk_lines = lines[: current_size * 2]
        try:
            result = request_ndjson(f"{es_url}/_bulk", chunk_lines)
        except RuntimeError as exc:
            if "HTTP 413" in str(exc):
                new_size = max(current_size // 2, MIN_BATCH_SIZE)
                if new_size == current_size:
                    # 已经到最小值还是 413，说明单篇文档本身超限，跳过
                    print(f"\n  [WARN] 413 at batch_size={current_size}, skipping {len(chunk_lines) // 2} docs")
                    return len(chunk_lines) // 2, current_size
                print(f"\n  [WARN] 413, reducing batch_size {current_size} → {new_size}")
                current_size = new_size
                continue
            raise

        failures = 0
        if result.get("errors"):
            for item in result.get("items", []):
                if item.get("index", {}).get("error"):
                    failures += 1
        return failures, current_size


def source_dataset_for_archive(path: Path) -> str:
    name = path.name
    if ".baseline." in name:
        return "baseline"
    if ".incr." in name:
        return "incremental"
    return "custom"


def load_progress(progress_file: Path) -> set[str]:
    """读取已完成的 archive 文件名集合。"""
    if not progress_file.exists():
        return set()
    try:
        with open(progress_file, encoding="utf-8") as fh:
            data = json.load(fh)
        return set(data.get("completed", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_progress(progress_file: Path, completed: set[str]) -> None:
    """持久化已完成的 archive 文件名。"""
    tmp = progress_file.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"completed": sorted(completed)}, fh, indent=2)
    tmp.replace(progress_file)


def import_archives(
    es_url: str,
    index: str,
    archives: list[Path],
    batch_size: int,
    limit: int | None,
    progress_file: Path | None = None,
    no_resume: bool = False,
) -> tuple[int, int]:
    imported_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    lines: list[str] = []
    total = 0
    failures = 0
    started = time.time()
    current_batch_size = batch_size

    # 断点续传：读取已完成的 archive 列表
    completed: set[str] = set()
    if progress_file and not no_resume:
        completed = load_progress(progress_file)
        if completed:
            print(f"Resuming: {len(completed)} archive(s) already completed, skipping them.")

    for archive in archives:
        if archive.name in completed:
            print(f"Skipping already-completed archive: {archive.name}")
            continue

        archive_count = 0
        source_dataset = source_dataset_for_archive(archive)
        for doc in iter_archive_docs(archive, imported_at, source_dataset):
            lines.append(json_dumps({"index": {"_index": index, "_id": doc["pmcid"]}}))
            lines.append(json_dumps(doc, ensure_ascii=False))
            total += 1
            archive_count += 1
            if len(lines) >= current_batch_size * 2:
                f, current_batch_size = flush_bulk(es_url, lines, current_batch_size)
                failures += f
                lines = []
                print_progress(total, failures, started)
            if limit and total >= limit:
                break

        # 当前 archive 处理完毕，刷新剩余数据
        if lines:
            f, current_batch_size = flush_bulk(es_url, lines, current_batch_size)
            failures += f
            lines = []
            print_progress(total, failures, started)

        print(f"\nParsed {archive}: {archive_count} XML articles")

        # 记录进度
        if progress_file:
            completed.add(archive.name)
            save_progress(progress_file, completed)

        if limit and total >= limit:
            break

    request_json("POST", f"{es_url}/{index}/_refresh")
    print(f"\nImported {total} PMC docs into {index}; bulk item failures: {failures}")
    return total, failures


def print_progress(total: int, failures: int, started: float) -> None:
    elapsed = max(time.time() - started, 0.001)
    print(f"\rImported {total} docs ({total / elapsed:.0f}/s), failures={failures}", end="", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import PMC OA Bulk JATS XML tar.gz archives into Elasticsearch.")
    parser.add_argument("--es-url", default=DEFAULT_ES_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--alias", default=DEFAULT_ALIAS)
    parser.add_argument("--mapping", default=DEFAULT_MAPPING)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Initial bulk batch size (docs). Auto-reduces on 413. Default: 100")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--wait-timeout", type=int, default=180)
    parser.add_argument("--progress-file", default=None,
                        help="JSON file to track completed archives for resume support. "
                             "Defaults to .import_progress_pmc_<index>.json in the current directory.")
    parser.add_argument("--no-resume", action="store_true",
                        help="Ignore existing progress and start from scratch (does not delete the index).")
    args = parser.parse_args()

    archives = expand_archives(args.input)
    if not archives:
        parser.error(f"No .tar.gz files found under {args.input}")

    # 进度文件路径
    if args.progress_file:
        progress_file = Path(args.progress_file)
    else:
        progress_file = Path(f".import_progress_pmc_{args.index}.json")

    wait_for_es(args.es_url, args.wait_timeout)
    ensure_index(args.es_url, args.index, args.alias, args.mapping, args.recreate)
    _, failures = import_archives(
        args.es_url,
        args.index,
        archives,
        args.batch_size,
        args.limit,
        progress_file=progress_file,
        no_resume=args.no_resume,
    )
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
