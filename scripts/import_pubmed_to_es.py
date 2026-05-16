#!/usr/bin/env python3
import argparse
import datetime as dt
import gzip
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx  # #4: 统一使用 httpx，替换 urllib
import orjson

try:
    # #1: 优先使用 lxml，解析速度比标准库快 3-5x；不可用时回退到标准库
    from lxml import etree as _lxml_etree
    _USE_LXML = True
except ImportError:
    _USE_LXML = False


DEFAULT_ES_URL = "http://localhost:9200"
DEFAULT_INDEX = "pubmed_updates_v1"
DEFAULT_ALIAS = "pubmed_updates"

# #8: source_rank 改为字典，新增数据集类型无需改代码
SOURCE_RANK: dict[str, int] = {
    "updatefiles": 20,
    "baseline": 10,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MONTH_MAP = {
    "jan": "01", "january": "01",
    "feb": "02", "february": "02",
    "mar": "03", "march": "03",
    "apr": "04", "april": "04",
    "may": "05",
    "jun": "06", "june": "06",
    "jul": "07", "july": "07",
    "aug": "08", "august": "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}


def text_of(elem):
    if elem is None:
        return None
    value = " ".join(part.strip() for part in elem.itertext() if part and part.strip())
    return value or None


def find_text(elem, path):
    return text_of(elem.find(path))


def first_text(elem, paths):
    for path in paths:
        value = find_text(elem, path)
        if value:
            return value
    return None


def parse_date_parts(parent):
    if parent is None:
        return None
    year = find_text(parent, "Year")
    if not year:
        medline_date = find_text(parent, "MedlineDate")
        if medline_date:
            year_from_medline = medline_date[:4] if medline_date[:4].isdigit() else None
            return drop_empty({
                "date": f"{year_from_medline}-01-01" if year_from_medline else None,
                "year": int(year_from_medline) if year_from_medline else None,
                "granularity": "year" if year_from_medline else None,
                "medline_date": medline_date,
            })
        return None
    if not year[:4].isdigit():
        return None

    year_num = int(year[:4])
    month_raw = find_text(parent, "Month")
    day_raw = find_text(parent, "Day")
    season = find_text(parent, "Season")
    medline_date = find_text(parent, "MedlineDate")

    month = parse_month(month_raw)
    day = parse_day(day_raw)
    date_value, granularity = build_es_date(year_num, month, day)

    return drop_empty({
        "date": date_value,
        "year": year_num,
        "month": int(month) if month else None,
        "day": int(day) if day else None,
        "granularity": granularity,
        "season": season,
        "medline_date": medline_date,
        "raw": " ".join(part for part in [year, month_raw, day_raw] if part) or None,
    })


def parse_date(parent):
    parsed = parse_date_parts(parent)
    return parsed.get("date") if parsed else None


def parse_month(value):
    if not value:
        return None
    value = value.strip()
    lowered = value.lower()
    if lowered in MONTH_MAP:
        return MONTH_MAP[lowered]
    if lowered[:3] in MONTH_MAP:
        return MONTH_MAP[lowered[:3]]
    if value.isdigit():
        month = int(value)
        if 1 <= month <= 12:
            return f"{month:02d}"
    return None


def parse_day(value):
    if not value or not value.isdigit():
        return None
    try:
        day = int(value)
    except ValueError:
        return None
    if 1 <= day <= 31:
        return f"{day:02d}"
    return None


def build_es_date(year: int, month: str | None, day: str | None) -> tuple[str | None, str | None]:
    """Build a valid Elasticsearch date, or return None for invalid PubMed dates."""
    month_value = int(month) if month else 1
    day_value = int(day) if day else 1
    try:
        dt.date(year, month_value, day_value)
    except ValueError:
        return None, None
    if month and day:
        return f"{year:04d}-{month}-{day}", "day"
    if month:
        return f"{year:04d}-{month}-01", "month"
    return f"{year:04d}-01-01", "year"


def parse_optional_int(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value or value.lower() in {"null", "none", "nan", "na", "n/a"}:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_authors(article):
    authors = []
    for position, author in enumerate(article.findall("./MedlineCitation/Article/AuthorList/Author"), start=1):
        collective = find_text(author, "CollectiveName")
        last_name = find_text(author, "LastName")
        fore_name = find_text(author, "ForeName")
        initials = find_text(author, "Initials")
        suffix = find_text(author, "Suffix")
        full_name = collective or " ".join(part for part in [fore_name, last_name] if part) or None
        affiliations = parse_simple_list(author, "AffiliationInfo/Affiliation")
        identifiers = [
            drop_empty({"source": identifier.attrib.get("Source"), "value": text_of(identifier)})
            for identifier in author.findall("Identifier")
            if text_of(identifier)
        ]
        if full_name:
            authors.append({
                "position": position,
                "last_name": last_name or collective,
                "fore_name": fore_name,
                "initials": initials,
                "suffix": suffix,
                "collective_name": collective,
                "full_name": full_name,
                "affiliations": affiliations,
                "affiliation": affiliations[0] if affiliations else None,
                "identifiers": identifiers,
                "valid": author.attrib.get("ValidYN"),
                "equal_contrib": author.attrib.get("EqualContrib"),
            })
    return authors


def parse_abstract(article_node):
    sections = []
    for abstract_text in article_node.findall("./Abstract/AbstractText"):
        text = text_of(abstract_text)
        if not text:
            continue
        label = abstract_text.attrib.get("Label")
        sections.append({
            "label": label,
            "nlm_category": abstract_text.attrib.get("NlmCategory"),
            "text": text,
        })
    copyright_text = find_text(article_node, "./Abstract/CopyrightInformation")
    abstract_parts = []
    for section in sections:
        prefix = f"{section['label']}: " if section.get("label") else ""
        abstract_parts.append(prefix + section["text"])
    if copyright_text:
        abstract_parts.append(copyright_text)
    return "\n".join(abstract_parts) or None, sections


def parse_mesh(article):
    mesh_terms = []
    major_terms = []
    headings = []
    for heading in article.findall("./MedlineCitation/MeshHeadingList/MeshHeading"):
        descriptor = heading.find("DescriptorName")
        descriptor_text = text_of(descriptor)
        descriptor_major = descriptor is not None and descriptor.attrib.get("MajorTopicYN") == "Y"
        qualifiers = []
        if descriptor_text:
            mesh_terms.append(descriptor_text)
            if descriptor_major:
                major_terms.append(descriptor_text)
        for qualifier in heading.findall("QualifierName"):
            qualifier_text = text_of(qualifier)
            qualifier_major = qualifier.attrib.get("MajorTopicYN") == "Y"
            if qualifier_text and descriptor_text:
                combined = f"{descriptor_text}/{qualifier_text}"
                mesh_terms.append(combined)
                if qualifier_major:
                    major_terms.append(combined)
            if qualifier_text:
                qualifiers.append(drop_empty({
                    "ui": qualifier.attrib.get("UI"),
                    "term": qualifier_text,
                    "major": qualifier_major,
                }))
        if descriptor_text:
            headings.append(drop_empty({
                "descriptor_ui": descriptor.attrib.get("UI") if descriptor is not None else None,
                "descriptor": descriptor_text,
                "major": descriptor_major,
                "qualifiers": qualifiers,
            }))
    return sorted(set(mesh_terms)), sorted(set(major_terms)), headings


def parse_chemicals(article):
    chemicals = []
    chemical_names = []
    for chemical in article.findall("./MedlineCitation/ChemicalList/Chemical"):
        name_node = chemical.find("NameOfSubstance")
        name = text_of(name_node)
        if not name:
            continue
        chemical_names.append(name)
        chemicals.append(drop_empty({
            "registry_number": find_text(chemical, "RegistryNumber"),
            "name": name,
            "ui": name_node.attrib.get("UI") if name_node is not None else None,
        }))
    return sorted(set(chemical_names)), chemicals


def parse_keywords(article):
    keyword_values = []
    keyword_entries = []
    for keyword_list in article.findall("./MedlineCitation/KeywordList"):
        owner = keyword_list.attrib.get("Owner")
        for keyword in keyword_list.findall("Keyword"):
            value = text_of(keyword)
            if not value:
                continue
            keyword_values.append(value)
            keyword_entries.append(drop_empty({
                "term": value,
                "owner": owner,
                "major": keyword.attrib.get("MajorTopicYN") == "Y",
            }))
    return sorted(set(keyword_values)), keyword_entries


def parse_publication_types(article):
    values = []
    entries = []
    for pub_type in article.findall("./MedlineCitation/Article/PublicationTypeList/PublicationType"):
        value = text_of(pub_type)
        if not value:
            continue
        values.append(value)
        entries.append(drop_empty({"ui": pub_type.attrib.get("UI"), "term": value}))
    return sorted(set(values)), entries


def parse_article_ids(article):
    entries = []
    values_by_type = {}
    for article_id in article.findall("./PubmedData/ArticleIdList/ArticleId"):
        value = text_of(article_id)
        id_type = article_id.attrib.get("IdType")
        if not value or not id_type:
            continue
        entries.append(drop_empty({"type": id_type, "value": value}))
        values_by_type.setdefault(id_type, []).append(value)

    article_node = article.find("./MedlineCitation/Article")
    if article_node is not None:
        for eid in article_node.findall("ELocationID"):
            value = text_of(eid)
            id_type = eid.attrib.get("EIdType")
            if not value or not id_type:
                continue
            entries.append(drop_empty({
                "type": id_type,
                "value": value,
                "valid": eid.attrib.get("ValidYN"),
            }))
            values_by_type.setdefault(id_type, []).append(value)

    return entries, values_by_type


def parse_history(article):
    entries = []
    by_status = {}
    for pub_date in article.findall("./PubmedData/History/PubMedPubDate"):
        status = pub_date.attrib.get("PubStatus")
        parsed = parse_date_parts(pub_date)
        if not status or not parsed:
            continue
        entry = drop_empty({
            "status": status,
            "date": parsed.get("date"),
            "year": parsed.get("year"),
            "month": parsed.get("month"),
            "day": parsed.get("day"),
            "hour": parse_optional_int(find_text(pub_date, "Hour")),
            "minute": parse_optional_int(find_text(pub_date, "Minute")),
        })
        entries.append(entry)
        if parsed.get("date"):
            by_status[status] = parsed.get("date")
    return entries, by_status


def parse_grants(article):
    grants = []
    for grant in article.findall("./MedlineCitation/Article/GrantList/Grant"):
        grants.append(drop_empty({
            "id": find_text(grant, "GrantID"),
            "acronym": find_text(grant, "Acronym"),
            "agency": find_text(grant, "Agency"),
            "country": find_text(grant, "Country"),
        }))
    return grants


def parse_comments_corrections(article):
    entries = []
    for item in article.findall("./MedlineCitation/CommentsCorrectionsList/CommentsCorrections"):
        entries.append(drop_empty({
            "ref_type": item.attrib.get("RefType"),
            "ref_source": find_text(item, "RefSource"),
            "pmid": find_text(item, "PMID"),
            "note": find_text(item, "Note"),
        }))
    return entries


def derive_publication_status_flags(publication_types, comments_corrections):
    publication_type_set = {value.lower() for value in publication_types}
    ref_type_set = {entry.get("ref_type", "").lower() for entry in comments_corrections}

    is_retracted = "retracted publication" in publication_type_set or "retractionin" in ref_type_set
    is_retraction_notice = "retraction notice" in publication_type_set or "retractionof" in ref_type_set
    has_expression_of_concern = (
        "expression of concern" in publication_type_set
        or bool(ref_type_set & {"expressionofconcernin", "expressionofconcernfor"})
    )
    has_erratum = "published erratum" in publication_type_set or bool(ref_type_set & {"erratumin", "erratumfor"})
    is_retracted_and_republished = bool(ref_type_set & {"retractedandrepublishedin", "retractedandrepublishedfrom"})
    has_retraction_relation = bool(ref_type_set & {
        "retractionin",
        "retractionof",
        "retractedandrepublishedin",
        "retractedandrepublishedfrom",
    })

    status = "active"
    if is_retracted:
        status = "retracted"
    elif is_retraction_notice:
        status = "retraction_notice"
    elif has_expression_of_concern:
        status = "expression_of_concern"

    return drop_empty({
        "publication_status_flags": {
            "is_retracted": is_retracted,
            "is_retraction_notice": is_retraction_notice,
            "has_retraction_relation": has_retraction_relation,
            "has_expression_of_concern": has_expression_of_concern,
            "has_erratum": has_erratum,
            "is_retracted_and_republished": is_retracted_and_republished,
        },
        "publication_status_normalized": status,
        "retraction": derive_retraction_relationships(comments_corrections),
    })


def derive_retraction_relationships(comments_corrections):
    relationships = []
    retracted_by_pmids = []
    retracts_pmids = []
    expression_of_concern_by_pmids = []
    expression_of_concern_for_pmids = []
    retracted_and_republished_by_pmids = []
    retracted_and_republishes_pmids = []

    for entry in comments_corrections:
        ref_type = entry.get("ref_type")
        ref_type_normalized = ref_type.lower() if ref_type else None
        target_pmid = entry.get("pmid")
        relationship = drop_empty({
            "ref_type": ref_type,
            "pmid": target_pmid,
            "ref_source": entry.get("ref_source"),
            "note": entry.get("note"),
        })
        if ref_type_normalized in {
            "retractionin",
            "retractionof",
            "expressionofconcernin",
            "expressionofconcernfor",
            "retractedandrepublishedin",
            "retractedandrepublishedfrom",
        }:
            relationships.append(relationship)
        if not target_pmid:
            continue
        if ref_type_normalized == "retractionin":
            retracted_by_pmids.append(target_pmid)
        elif ref_type_normalized == "retractionof":
            retracts_pmids.append(target_pmid)
        elif ref_type_normalized == "expressionofconcernin":
            expression_of_concern_by_pmids.append(target_pmid)
        elif ref_type_normalized == "expressionofconcernfor":
            expression_of_concern_for_pmids.append(target_pmid)
        elif ref_type_normalized == "retractedandrepublishedin":
            retracted_and_republished_by_pmids.append(target_pmid)
        elif ref_type_normalized == "retractedandrepublishedfrom":
            retracted_and_republishes_pmids.append(target_pmid)

    return drop_empty({
        "relationships": relationships,
        "retracted_by_pmids": sorted(set(retracted_by_pmids)),
        "retracts_pmids": sorted(set(retracts_pmids)),
        "expression_of_concern_by_pmids": sorted(set(expression_of_concern_by_pmids)),
        "expression_of_concern_for_pmids": sorted(set(expression_of_concern_for_pmids)),
        "retracted_and_republished_by_pmids": sorted(set(retracted_and_republished_by_pmids)),
        "retracted_and_republishes_pmids": sorted(set(retracted_and_republishes_pmids)),
    })


def parse_references(article):
    references = []
    for ref in article.findall("./PubmedData/ReferenceList/Reference"):
        article_ids = []
        for article_id in ref.findall("./ArticleIdList/ArticleId"):
            value = text_of(article_id)
            if value:
                article_ids.append(drop_empty({
                    "type": article_id.attrib.get("IdType"),
                    "value": value,
                }))
        references.append(drop_empty({
            "citation": find_text(ref, "Citation"),
            "article_ids": article_ids,
        }))
    return references


def parse_data_banks(article):
    data_banks = []
    accessions = []
    for data_bank in article.findall("./MedlineCitation/Article/DataBankList/DataBank"):
        bank_name = find_text(data_bank, "DataBankName")
        numbers = parse_simple_list(data_bank, "AccessionNumberList/AccessionNumber")
        accessions.extend(numbers)
        data_banks.append(drop_empty({
            "name": bank_name,
            "accession_numbers": numbers,
        }))
    return data_banks, sorted(set(accessions))


def parse_simple_list(article, path):
    values = []
    for elem in article.findall(path):
        value = text_of(elem)
        if value:
            values.append(value)
    return sorted(set(values))


def first_value(values_by_type, key):
    values = values_by_type.get(key) or []
    return values[0] if values else None


def json_dumps(value, ensure_ascii=True):
    option = 0 if ensure_ascii else orjson.OPT_APPEND_NEWLINE
    data = orjson.dumps(value, option=option)
    if ensure_ascii:
        return data.decode("utf-8")
    return data.decode("utf-8").rstrip("\n")


def _parse_journal(article_node, medline_journal) -> dict:
    """#7: 从 parse_doc 拆出的期刊字段解析。"""
    journal_node = article_node.find("Journal")
    issue_node = article_node.find("Journal/JournalIssue")
    journal_pub_date = parse_date_parts(
        article_node.find("Journal/JournalIssue/PubDate")
    )
    issn_node = journal_node.find("ISSN") if journal_node is not None else None
    return {
        "journal": {
            "title": find_text(journal_node, "Title") if journal_node is not None else None,
            "iso_abbreviation": find_text(journal_node, "ISOAbbreviation") if journal_node is not None else None,
            "issn": find_text(journal_node, "ISSN") if journal_node is not None else None,
            "issn_type": issn_node.attrib.get("IssnType") if issn_node is not None else None,
            "volume": find_text(issue_node, "Volume") if issue_node is not None else None,
            "issue": find_text(issue_node, "Issue") if issue_node is not None else None,
            "country": find_text(medline_journal, "Country") if medline_journal is not None else None,
            "medline_ta": find_text(medline_journal, "MedlineTA") if medline_journal is not None else None,
            "nlm_unique_id": find_text(medline_journal, "NlmUniqueID") if medline_journal is not None else None,
            "issn_linking": find_text(medline_journal, "ISSNLinking") if medline_journal is not None else None,
            "pub_date": journal_pub_date,
        },
        "journal_pub_date": journal_pub_date,
    }


def _parse_dates(article_node, medline) -> dict:
    """#7: 从 parse_doc 拆出的日期字段解析。"""
    article_date = parse_date_parts(article_node.find("ArticleDate"))
    journal_pub_date = parse_date_parts(
        article_node.find("Journal/JournalIssue/PubDate")
    )
    publication_date = (article_date or journal_pub_date or {}).get("date")
    completed_date = parse_date_parts(medline.find("DateCompleted"))
    revised_date = parse_date_parts(medline.find("DateRevised"))
    return {
        "article_date_parsed": article_date,
        "journal_pub_date": journal_pub_date,
        "publication_date": publication_date,
        "publication_year": int(publication_date[:4]) if publication_date and publication_date[:4].isdigit() else None,
        "publication_date_granularity": (article_date or journal_pub_date or {}).get("granularity"),
        "article_date": article_date.get("date") if article_date else None,
        "completed_date": completed_date.get("date") if completed_date else None,
        "revised_date": revised_date.get("date") if revised_date else None,
    }


def _parse_source_meta(source_file: str, source_dataset: str, source_index: str, imported_at: str) -> dict:
    """#7: 从 parse_doc 拆出的来源元数据字段。"""
    return {
        "source_file": os.path.basename(source_file),
        "source_dataset": source_dataset,
        "source_index": source_index,
        "source_rank": source_rank(source_dataset),
        "imported_at": imported_at,
    }


def parse_doc(article, source_file: str, imported_at: str, source_dataset: str, source_index: str) -> dict | None:
    """#7: parse_doc 主体保持清晰，细节委托给子函数。"""
    medline = article.find("./MedlineCitation")
    article_node = article.find("./MedlineCitation/Article")
    if medline is None or article_node is None:
        return None

    pmid = find_text(medline, "PMID")
    if not pmid:
        return None

    abstract, abstract_sections = parse_abstract(article_node)
    authors = parse_authors(article)
    mesh_terms, major_mesh_terms, mesh_headings = parse_mesh(article)
    chemical_names, chemicals = parse_chemicals(article)
    keywords, keyword_entries = parse_keywords(article)
    publication_types, publication_type_entries = parse_publication_types(article)
    article_ids, article_ids_by_type = parse_article_ids(article)
    pubmed_history, history_by_status = parse_history(article)
    data_banks, accession_numbers = parse_data_banks(article)
    comments_corrections = parse_comments_corrections(article)
    publication_status = derive_publication_status_flags(publication_types, comments_corrections)

    medline_journal = medline.find("MedlineJournalInfo")
    journal_data = _parse_journal(article_node, medline_journal)
    date_data = _parse_dates(article_node, medline)

    doc = {
        "pmid": pmid,
        "pmid_num": int(pmid) if pmid.isdigit() else None,
        "doi": first_value(article_ids_by_type, "doi"),
        "pmcid": first_value(article_ids_by_type, "pmc"),
        "pii": first_value(article_ids_by_type, "pii"),
        "article_ids": article_ids,
        "title": find_text(article_node, "ArticleTitle"),
        "vernacular_title": find_text(article_node, "VernacularTitle"),
        "abstract": abstract,
        "abstract_sections": abstract_sections,
        "authors": authors,
        "author_names": [a["full_name"] for a in authors if a.get("full_name")],
        "journal": journal_data["journal"],
        "pagination": find_text(article_node, "Pagination/MedlinePgn"),
        "language": find_text(article_node, "Language"),
        "publication_types": publication_types,
        "publication_type_entries": publication_type_entries,
        "mesh_terms": mesh_terms,
        "major_mesh_terms": major_mesh_terms,
        "mesh_headings": mesh_headings,
        "chemicals": chemical_names,
        "chemical_entries": chemicals,
        "keywords": keywords,
        "keyword_entries": keyword_entries,
        "supplemental_mesh_terms": parse_simple_list(article, "./MedlineCitation/SupplMeshList/SupplMeshName"),
        "gene_symbols": parse_simple_list(article, "./MedlineCitation/GeneSymbolList/GeneSymbol"),
        "grants": parse_grants(article),
        "data_banks": data_banks,
        "accession_numbers": accession_numbers,
        "comments_corrections": comments_corrections,
        **publication_status,
        "references": parse_references(article),
        "publication_date": date_data["publication_date"],
        "publication_year": date_data["publication_year"],
        "publication_date_granularity": date_data["publication_date_granularity"],
        "article_date": date_data["article_date"],
        "completed_date": date_data["completed_date"],
        "revised_date": date_data["revised_date"],
        "pubmed_history": pubmed_history,
        "pubmed_date": history_by_status.get("pubmed"),
        "medline_date": history_by_status.get("medline"),
        "entrez_date": history_by_status.get("entrez"),
        "publication_status": find_text(article, "./PubmedData/PublicationStatus"),
        "status": medline.attrib.get("Status"),
        "indexing_method": medline.attrib.get("IndexingMethod"),
        "owner": medline.attrib.get("Owner"),
        "citation_subsets": parse_simple_list(article, "./MedlineCitation/CitationSubset"),
        **_parse_source_meta(source_file, source_dataset, source_index, imported_at),
    }
    return drop_empty(doc)


def source_rank(source_dataset: str) -> int:
    """#8: 从字典查找，新增数据集类型只需更新 SOURCE_RANK，无需改逻辑。"""
    return SOURCE_RANK.get(source_dataset, 0)


def drop_empty(value):
    if isinstance(value, dict):
        cleaned = {key: drop_empty(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        return [drop_empty(item) for item in value if item not in (None, "", [], {})]
    return value


def request_json(method: str, url: str, body: object = None) -> dict:
    """#4: 使用 httpx 替换 urllib，错误信息更清晰。"""
    kwargs: dict = {"timeout": 120.0}
    if body is not None:
        kwargs["content"] = orjson.dumps(body)
        kwargs["headers"] = {"Content-Type": "application/json", "Accept": "application/json"}
    else:
        kwargs["headers"] = {"Accept": "application/json"}
    try:
        response = httpx.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"{method} {url} failed: HTTP {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc


def request_ndjson(url: str, lines: list[str]) -> dict:
    """#4: 使用 httpx 发送 NDJSON bulk 请求。"""
    body = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        response = httpx.post(
            url,
            content=body,
            headers={"Content-Type": "application/x-ndjson"},
            timeout=180.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"POST {url} failed: HTTP {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"POST {url} failed: {exc}") from exc


def wait_for_es(es_url: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            return request_json("GET", f"{es_url}/_cluster/health?wait_for_status=yellow&timeout=1s")
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"Elasticsearch is not ready at {es_url}: {last_error}")


def ensure_index(es_url, index, alias, mapping_path, recreate=False, alias_mode="switch", read_aliases=None, reset_read_aliases=False):
    if recreate:
        try:
            request_json("DELETE", f"{es_url}/{index}")
            print(f"Deleted existing index {index}")
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
    with open(mapping_path, "r", encoding="utf-8") as fh:
        mapping = json.load(fh)
    try:
        request_json("HEAD", f"{es_url}/{index}")
        exists = True
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        exists = False
    if not exists:
        request_json("PUT", f"{es_url}/{index}", mapping)
        print(f"Created index {index}")
    if alias and alias_mode == "switch":
        switch_alias(es_url, index, alias)
    elif alias:
        add_alias(es_url, index, alias)
    for read_alias in read_aliases or []:
        if reset_read_aliases:
            remove_alias_from_all(es_url, read_alias)
        add_alias(es_url, index, read_alias)


def switch_alias(es_url, index, alias):
    actions = []
    try:
        existing = request_json("GET", f"{es_url}/_alias/{alias}")
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        existing = {}
    for existing_index in existing:
        if existing_index != index:
            actions.append({"remove": {"index": existing_index, "alias": alias}})
    actions.append({"add": {"index": index, "alias": alias}})
    request_json("POST", f"{es_url}/_aliases", {"actions": actions})
    print(f"Alias {alias} -> {index}")


def add_alias(es_url, index, alias):
    request_json("POST", f"{es_url}/_aliases", {"actions": [{"add": {"index": index, "alias": alias}}]})
    print(f"Alias {alias} includes {index}")


def remove_alias_from_all(es_url, alias):
    try:
        existing = request_json("GET", f"{es_url}/_alias/{alias}")
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        return
    actions = [{"remove": {"index": existing_index, "alias": alias}} for existing_index in existing]
    if actions:
        request_json("POST", f"{es_url}/_aliases", {"actions": actions})
        print(f"Removed alias {alias} from {len(actions)} index(es)")


def iter_pubmed_articles(path: Path):
    """#1: lxml 可用时使用 lxml.etree.iterparse，速度比标准库快 3-5x。
    两者接口兼容，回退逻辑透明。
    """
    with gzip.open(path, "rb") as fh:
        if _USE_LXML:
            context = _lxml_etree.iterparse(fh, events=("end",), tag="PubmedArticle")
            for _, elem in context:
                # lxml 元素需要转换为标准库兼容的 ET.Element 才能用 find/findall
                # 直接用 lxml 元素即可，接口完全兼容
                yield elem
                elem.clear()
                # 清理已处理的前驱节点，防止内存累积
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
        else:
            context = ET.iterparse(fh, events=("end",))
            for _, elem in context:
                if elem.tag == "PubmedArticle":
                    yield elem
                    elem.clear()


def _progress_file(index: str) -> Path:
    """#5: 进度文件路径，按索引名区分，存放在当前工作目录。"""
    return Path(f".import_progress_{index}.json")


def _load_progress(index: str) -> set[str]:
    """#5: 读取已完成的文件名集合。"""
    pf = _progress_file(index)
    if not pf.exists():
        return set()
    try:
        data = json.loads(pf.read_text(encoding="utf-8"))
        return set(data.get("completed", []))
    except Exception:
        return set()


def _save_progress(index: str, completed: set[str]) -> None:
    """#5: 持久化已完成的文件名集合。"""
    pf = _progress_file(index)
    pf.write_text(
        json.dumps({"index": index, "completed": sorted(completed)}, indent=2),
        encoding="utf-8",
    )


def _open_error_log(index: str) -> tuple[object, Path]:
    """#6: 打开错误日志文件，返回 (file_handle, path)。"""
    log_path = Path(f".import_errors_{index}_{int(time.time())}.jsonl")
    return open(log_path, "w", encoding="utf-8"), log_path  # noqa: SIM115


def bulk_import(
    es_url: str,
    index: str,
    files: list[Path],
    batch_size: int,
    limit: int | None = None,
    source_dataset: str = "updatefiles",
    resume: bool = True,
) -> tuple[int, int]:
    """
    #5: 支持断点续传（resume=True 时跳过已完成文件）。
    #6: 所有 bulk 错误写入独立 .jsonl 日志文件，不再只打印前5条。
    """
    imported_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    total = 0
    failures = 0
    started = time.time()
    lines: list[str] = []

    # #5: 加载进度
    completed_files: set[str] = _load_progress(index) if resume else set()
    if completed_files:
        logger.info("Resuming: %d file(s) already completed, skipping.", len(completed_files))

    # #6: 打开错误日志
    error_log, error_log_path = _open_error_log(index)
    error_count = 0

    try:
        for file_path in files:
            file_key = file_path.name

            # #5: 跳过已完成文件
            if resume and file_key in completed_files:
                logger.info("Skipping (already imported): %s", file_key)
                continue

            file_count = 0
            for article in iter_pubmed_articles(file_path):
                doc = parse_doc(article, str(file_path), imported_at, source_dataset, index)
                if not doc:
                    continue
                lines.append(json_dumps({"index": {"_index": index, "_id": doc["pmid"]}}))
                lines.append(json_dumps(doc, ensure_ascii=False))
                file_count += 1
                total += 1
                if len(lines) >= batch_size * 2:
                    batch_failures = flush_bulk(es_url, lines, error_log)
                    failures += batch_failures
                    error_count += batch_failures
                    lines = []
                    print_progress(total, failures, started)
                if limit and total >= limit:
                    break

            logger.info("Parsed %s: %d articles", file_path, file_count)

            # #5: 文件处理完成后记录进度
            if not (limit and total >= limit):
                completed_files.add(file_key)
                _save_progress(index, completed_files)

            if limit and total >= limit:
                break

        if lines:
            batch_failures = flush_bulk(es_url, lines, error_log)
            failures += batch_failures
            error_count += batch_failures
            print_progress(total, failures, started)

    finally:
        error_log.close()
        if error_count == 0:
            # 无错误时删除空日志文件
            error_log_path.unlink(missing_ok=True)
        else:
            logger.warning("Bulk errors: %d item(s) failed. See %s", error_count, error_log_path)

    request_json("POST", f"{es_url}/{index}/_refresh")
    logger.info("Imported %d docs into %s; bulk item failures: %d", total, index, failures)
    return total, failures


def flush_bulk(es_url: str, lines: list[str], error_log) -> int:
    """#6: 所有失败条目写入 error_log（JSONL），不再只打印前5条。"""
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


def print_progress(total, failures, started):
    elapsed = max(time.time() - started, 0.001)
    rate = total / elapsed
    print(f"\rImported {total} docs ({rate:.0f}/s), failures={failures}", end="", flush=True)


def expand_files(input_path):
    path = Path(input_path)
    if path.is_file():
        return [path]
    return sorted(path.glob("*.xml.gz"))


def main():
    parser = argparse.ArgumentParser(description="Import PubMed XML gzip files into a local Elasticsearch index.")
    parser.add_argument("--es-url", default=DEFAULT_ES_URL)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--alias", default=DEFAULT_ALIAS)
    parser.add_argument("--alias-mode", choices=["switch", "add"], default="switch", help="Use switch for a source write alias, add for a shared read alias.")
    parser.add_argument("--read-alias", action="append", default=[], help="Additional multi-index read alias to add this index to. Repeatable.")
    parser.add_argument("--reset-read-alias", action="store_true", help="Remove each --read-alias from all existing indices before adding this index.")
    parser.add_argument("--mapping", default="config/pubmed_index.json")
    parser.add_argument("--input", default="data/updatefiles")
    parser.add_argument("--source-dataset", choices=["baseline", "updatefiles", "custom"], default="updatefiles")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--limit", type=int, help="Import only the first N records for testing.")
    parser.add_argument("--recreate", action="store_true", help="Delete and recreate the target index before importing.")
    parser.add_argument("--wait-timeout", type=int, default=180)
    parser.add_argument("--no-resume", action="store_true", help="Ignore progress file and re-import all files.")  # #5
    args = parser.parse_args()

    files = expand_files(args.input)
    if not files:
        parser.error(f"No .xml.gz files found under {args.input}")

    wait_for_es(args.es_url, args.wait_timeout)
    ensure_index(
        args.es_url,
        args.index,
        args.alias,
        args.mapping,
        recreate=args.recreate,
        alias_mode=args.alias_mode,
        read_aliases=args.read_alias,
        reset_read_aliases=args.reset_read_alias,
    )
    total, failures = bulk_import(
        args.es_url,
        args.index,
        files,
        args.batch_size,
        args.limit,
        args.source_dataset,
        resume=not args.no_resume,  # #5
    )
    if failures:
        sys.exit(2)
    if total == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
