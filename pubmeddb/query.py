from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


TEXT_FIELD_MAP = {
    "all": ["title^5", "abstract^2", "mesh_terms^3", "major_mesh_terms^4", "author_names^2", "journal.title", "keywords^2", "chemicals^2", "supplemental_mesh_terms^2", "gene_symbols^2", "article_ids.value", "search_text"],
    "title": ["title^5"],
    "abstract": ["abstract^2"],
    "author": ["author_names^2", "authors.full_name^2", "authors.affiliations"],
    "journal": ["journal.title", "journal.iso_abbreviation"],
    "mesh": ["mesh_terms^3", "mesh_headings.descriptor^3"],
    "major_mesh": ["major_mesh_terms^4"],
    "chemical": ["chemicals^2", "chemical_entries.name^2"],
    "keyword": ["keywords^2", "keyword_entries.term^2"],
    "gene": ["gene_symbols^2"],
    "id": ["pmid", "doi", "pmcid", "pii", "article_ids.value"],
}

FILTER_FIELD_MAP = {
    "pmid": "pmid",
    "doi": "doi",
    "pmcid": "pmcid",
    "pii": "pii",
    "language": "language",
    "publication_type": "publication_types",
    "journal": "journal.title.keyword",
    "journal_iso": "journal.iso_abbreviation",
    "mesh": "mesh_terms.keyword",
    "major_mesh": "major_mesh_terms",
    "chemical": "chemicals.keyword",
    "keyword": "keywords.keyword",
    "gene": "gene_symbols",
    "source_file": "source_file",
    "source_dataset": "source_dataset",
    "publication_status": "publication_status",
    "publication_status_normalized": "publication_status_normalized",
    "retraction_ref_type": "retraction.relationships.ref_type",
}

CASE_INSENSITIVE_FILTERS = {
    "doi",
    "pmcid",
    "pii",
    "publication_type",
    "journal",
    "journal_iso",
    "mesh",
    "major_mesh",
    "chemical",
    "keyword",
    "gene",
    "publication_status",
    "publication_status_normalized",
    "retraction_ref_type",
    "source_dataset",
}

SOURCE_FIELDS = [
    "pmid",
    "doi",
    "pmcid",
    "title",
    "vernacular_title",
    "abstract",
    "abstract_sections",
    "author_names",
    "authors",
    "journal",
    "publication_date",
    "publication_year",
    "publication_date_granularity",
    "publication_types",
    "publication_status_normalized",
    "publication_status_flags",
    "retraction",
    "mesh_terms",
    "major_mesh_terms",
    "mesh_headings",
    "chemicals",
    "chemical_entries",
    "keywords",
    "gene_symbols",
    "grants",
    "article_ids",
    "source_dataset",
    "source_index",
    "source_rank",
]

SUMMARY_SOURCE_FIELDS = [
    "pmid",
    "doi",
    "pmcid",
    "title",
    "author_names",
    "journal",
    "publication_date",
    "publication_year",
    "publication_types",
    "publication_status_normalized",
    "publication_status_flags",
    "retraction",
    "mesh_terms",
    "major_mesh_terms",
    "chemicals",
    "keywords",
    "source_dataset",
    "source_index",
]

ID_SOURCE_FIELDS = [
    "pmid",
    "doi",
    "pmcid",
    "pii",
    "article_ids",
    "title",
    "publication_year",
    "source_dataset",
    "source_index",
]

HIGHLIGHT_FIELDS = {
    "title": {},
    "abstract": {"fragment_size": 180, "number_of_fragments": 3},
    "mesh_terms": {},
    "major_mesh_terms": {},
    "chemicals": {},
    "keywords": {},
    "supplemental_mesh_terms": {},
    "gene_symbols": {},
}

PHRASE_BOOST_FIELDS = [
    "title^12",
    "abstract^4",
    "mesh_terms^6",
    "chemicals^5",
    "keywords^4",
]

PMID_RE = re.compile(r"^\d{5,12}$")
PMCID_RE = re.compile(r"^pmc\d+$", re.IGNORECASE)
DOI_RE = re.compile(r"^10\.\S+/\S+$", re.IGNORECASE)


class FieldQuery(BaseModel):
    field: str = Field(description="One of: all, title, abstract, author, journal, mesh, major_mesh, chemical, keyword, gene, id")
    query: str
    occur: Literal["must", "should", "must_not", "filter"] = "must"
    operator: Literal["and", "or"] = "and"
    match_type: Literal["match", "phrase", "exact"] = "match"
    boost: float | None = None

    @field_validator("field")
    @classmethod
    def known_field(cls, value: str) -> str:
        if value not in TEXT_FIELD_MAP and value not in FILTER_FIELD_MAP:
            raise ValueError(f"Unsupported field: {value}")
        return value


class TermFilter(BaseModel):
    field: str
    values: list[str]
    occur: Literal["filter", "must_not"] = "filter"

    @field_validator("field")
    @classmethod
    def known_filter(cls, value: str) -> str:
        if value not in FILTER_FIELD_MAP:
            raise ValueError(f"Unsupported filter field: {value}")
        return value


class DateRange(BaseModel):
    field: Literal["publication_date", "article_date", "completed_date", "revised_date", "pubmed_date", "medline_date", "entrez_date"] = "publication_date"
    gte: str | None = None
    lte: str | None = None


class AdvancedSearchRequest(BaseModel):
    model_config = {"populate_by_name": True}  # 允许同时用 from_ 和 from 两种方式构造

    query: str | None = None
    query_fields: list[str] = Field(default_factory=lambda: ["all"])
    operator: Literal["and", "or"] = "and"
    mode: Literal["balanced", "strict", "broad", "phrase"] = "balanced"
    minimum_should_match: str | None = None
    phrase_boost: bool = True
    field_queries: list[FieldQuery] = Field(default_factory=list)
    filters: list[TermFilter] = Field(default_factory=list)
    date_range: DateRange | None = None
    year_from: int | None = None
    year_to: int | None = None
    has_abstract: bool | None = None
    from_: int = Field(default=0, ge=0, serialization_alias="from", validation_alias="from")
    size: int = Field(default=10, ge=1, le=100)
    sort: Literal["relevance", "newest", "oldest", "pmid_desc", "pmid_asc"] = "relevance"
    source: Literal["summary", "full", "ids"] = "summary"
    highlight: bool = True
    facets: bool = False

    @field_validator("query_fields")
    @classmethod
    def known_query_fields(cls, value: list[str]) -> list[str]:
        unknown = [field for field in value if field not in TEXT_FIELD_MAP]
        if unknown:
            raise ValueError(f"Unsupported query fields: {', '.join(unknown)}")
        return value


def build_simple_request(
    q: str | None,
    field: str,
    author: str | None,
    journal: str | None,
    mesh: str | None,
    chemical: str | None,
    keyword: str | None,
    year_from: int | None,
    year_to: int | None,
    publication_type: str | None,
    language: str | None,
    has_abstract: bool | None,
    page: int,
    size: int,
    sort: str,
    mode: str = "balanced",
    source: str = "summary",
) -> AdvancedSearchRequest:
    field_queries = []
    if author:
        field_queries.append(FieldQuery(field="author", query=author))
    if journal:
        field_queries.append(FieldQuery(field="journal", query=journal))
    if mesh:
        field_queries.append(FieldQuery(field="mesh", query=mesh))
    if chemical:
        field_queries.append(FieldQuery(field="chemical", query=chemical))
    if keyword:
        field_queries.append(FieldQuery(field="keyword", query=keyword))

    filters = []
    if publication_type:
        filters.append(TermFilter(field="publication_type", values=[publication_type]))
    if language:
        filters.append(TermFilter(field="language", values=[language]))

    return AdvancedSearchRequest(
        query=q,
        query_fields=[field],
        field_queries=field_queries,
        filters=filters,
        year_from=year_from,
        year_to=year_to,
        has_abstract=has_abstract,
        from_=(page - 1) * size,
        size=size,
        sort=sort,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
    )


def build_es_query(request: AdvancedSearchRequest) -> dict[str, Any]:
    must: list[dict[str, Any]] = []
    should: list[dict[str, Any]] = []
    filters: list[dict[str, Any]] = []
    must_not: list[dict[str, Any]] = []

    if request.query:
        main_clause, boost_clauses = build_main_query_clause(request)
        must.append(main_clause)
        should.extend(boost_clauses)

    for field_query in request.field_queries:
        clause = build_field_clause(field_query)
        target = {"must": must, "should": should, "filter": filters, "must_not": must_not}[field_query.occur]
        target.append(clause)

    for term_filter in request.filters:
        clause = {"terms": {FILTER_FIELD_MAP[term_filter.field]: normalize_filter_values(term_filter.field, term_filter.values)}}
        if term_filter.occur == "must_not":
            must_not.append(clause)
        else:
            filters.append(clause)

    if request.year_from is not None or request.year_to is not None:
        year_range: dict[str, int] = {}
        if request.year_from is not None:
            year_range["gte"] = request.year_from
        if request.year_to is not None:
            year_range["lte"] = request.year_to
        filters.append({"range": {"publication_year": year_range}})

    if request.date_range and (request.date_range.gte or request.date_range.lte):
        date_range: dict[str, str] = {}
        if request.date_range.gte:
            date_range["gte"] = request.date_range.gte
        if request.date_range.lte:
            date_range["lte"] = request.date_range.lte
        filters.append({"range": {request.date_range.field: date_range}})

    if request.has_abstract is True:
        filters.append({"exists": {"field": "abstract"}})
    elif request.has_abstract is False:
        must_not.append({"exists": {"field": "abstract"}})

    bool_query: dict[str, Any] = {}
    if must:
        bool_query["must"] = must
    if should:
        bool_query["should"] = should
        if not must:
            bool_query["minimum_should_match"] = 1
    if filters:
        bool_query["filter"] = filters
    if must_not:
        bool_query["must_not"] = must_not

    body: dict[str, Any] = {
        "from": request.from_,
        "size": request.size,
        "track_total_hits": True,
        "query": {"bool": bool_query} if bool_query else {"match_all": {}},
        "_source": source_fields(request.source),
        "collapse": {"field": "pmid"},
    }

    sort_clause = build_sort(request.sort)
    body["sort"] = sort_clause

    if request.highlight:
        body["highlight"] = {
            "require_field_match": False,
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "fields": HIGHLIGHT_FIELDS,
        }

    if request.facets:
        body["aggs"] = build_facets()

    return body


def expand_text_fields(fields: list[str]) -> list[str]:
    expanded: list[str] = []
    for field in fields:
        expanded.extend(TEXT_FIELD_MAP[field])
    return list(dict.fromkeys(expanded))


def build_field_clause(field_query: FieldQuery) -> dict[str, Any]:
    if field_query.match_type == "exact" and field_query.field in FILTER_FIELD_MAP:
        value: str | list[str] = normalize_filter_values(field_query.field, [field_query.query])[0]
        return {"term": {FILTER_FIELD_MAP[field_query.field]: value}}

    fields = TEXT_FIELD_MAP.get(field_query.field)
    if not fields and field_query.field in FILTER_FIELD_MAP:
        fields = [FILTER_FIELD_MAP[field_query.field]]

    if field_query.match_type == "phrase":
        return {
            "multi_match": {
                "query": field_query.query,
                "fields": apply_boost(fields or [], field_query.boost),
                "type": "phrase",
            }
        }

    return {
        "multi_match": {
            "query": field_query.query,
            "fields": apply_boost(fields or [], field_query.boost),
            "type": "best_fields",
            "operator": field_query.operator,
            "tie_breaker": 0.2,
        }
    }


def build_main_query_clause(request: AdvancedSearchRequest) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    query = request.query or ""
    fields = expand_text_fields(request.query_fields)

    if request.mode == "phrase":
        main_clause: dict[str, Any] = {
            "multi_match": {
                "query": query,
                "fields": fields,
                "type": "phrase",
            }
        }
    else:
        operator = "or" if request.mode == "broad" else request.operator
        main_clause = {
            "multi_match": {
                "query": query,
                "fields": fields,
                "type": "best_fields",
                "operator": operator,
                "tie_breaker": 0.2,
            }
        }
        if request.mode == "broad":
            main_clause["multi_match"]["minimum_should_match"] = request.minimum_should_match or "2<75%"

    boost_clauses = build_identifier_boosts(query)
    if request.phrase_boost and request.mode in {"balanced", "broad"}:
        boost_clauses.extend([
            {
                "multi_match": {
                    "query": query,
                    "fields": PHRASE_BOOST_FIELDS,
                    "type": "phrase",
                    "boost": 2.5,
                }
            },
            {
                "multi_match": {
                    "query": query,
                    "fields": ["title^6", "abstract^2"],
                    "type": "phrase",
                    "slop": 3,
                    "boost": 1.5,
                }
            },
        ])

    return main_clause, boost_clauses


def build_identifier_boosts(query: str) -> list[dict[str, Any]]:
    normalized = query.strip().lower()
    clauses = []
    if PMID_RE.match(normalized):
        clauses.append(boosted_term("pmid", normalized, 300))
    if PMCID_RE.match(normalized):
        clauses.append(boosted_term("pmcid", normalized, 250))
    if DOI_RE.match(normalized):
        clauses.append(boosted_term("doi", normalized, 250))
    if normalized:
        clauses.append(boosted_term("article_ids.value", normalized, 120))
    return clauses


def boosted_term(field: str, value: str, boost: float) -> dict[str, Any]:
    return {
        "constant_score": {
            "filter": {"term": {field: value}},
            "boost": boost,
        }
    }


def apply_boost(fields: list[str], boost: float | None) -> list[str]:
    if boost is None:
        return fields
    return [field if "^" in field else f"{field}^{boost}" for field in fields]


def normalize_filter_values(field: str, values: list[str]) -> list[str]:
    if field not in CASE_INSENSITIVE_FILTERS:
        return values
    return [value.lower() for value in values]


def source_fields(source: str) -> list[str]:
    if source == "full":
        return SOURCE_FIELDS
    if source == "ids":
        return ID_SOURCE_FIELDS
    return SUMMARY_SOURCE_FIELDS


def build_sort(sort: str) -> list[dict[str, Any]] | None:
    if sort == "relevance":
        return [{"_score": {"order": "desc"}}, source_rank_sort()]
    if sort == "newest":
        return [{"publication_date": {"order": "desc", "missing": "_last", "unmapped_type": "date"}}, source_rank_sort(), {"_score": "desc"}]
    if sort == "oldest":
        return [{"publication_date": {"order": "asc", "missing": "_last", "unmapped_type": "date"}}, source_rank_sort(), {"_score": "desc"}]
    if sort == "pmid_desc":
        return [{"pmid_num": {"order": "desc", "unmapped_type": "long"}}, source_rank_sort()]
    if sort == "pmid_asc":
        return [{"pmid_num": {"order": "asc", "unmapped_type": "long"}}, source_rank_sort()]
    return [{"_score": {"order": "desc"}}, source_rank_sort()]


def source_rank_sort() -> dict[str, Any]:
    return {"source_rank": {"order": "desc", "missing": "_last", "unmapped_type": "integer"}}


def build_facets() -> dict[str, Any]:
    return {
        "years": {"terms": {"field": "publication_year", "size": 20, "order": {"_key": "desc"}}},
        "languages": {"terms": {"field": "language", "size": 20}},
        "publication_types": {"terms": {"field": "publication_types", "size": 20}},
        "publication_status_normalized": {"terms": {"field": "publication_status_normalized", "size": 10}},
        "major_mesh": {"terms": {"field": "major_mesh_terms", "size": 20}},
        "mesh": {"terms": {"field": "mesh_terms.keyword", "size": 20}},
        "chemicals": {"terms": {"field": "chemicals.keyword", "size": 20}},
        "publication_status": {"terms": {"field": "publication_status", "size": 20}},
        "source_dataset": {"terms": {"field": "source_dataset", "size": 10}},
        "journals": {"terms": {"field": "journal.iso_abbreviation", "size": 20}},
    }


def format_search_response(result: dict[str, Any], request: AdvancedSearchRequest) -> dict[str, Any]:
    """#12: 直接透传 ES 已按 _source 过滤好的字段，不再手动逐字段重组。
    只做两件事：
    1. 注入 score 和 highlight（ES 响应的顶层字段，不在 _source 里）。
    2. 保持 authors 别名兼容（_source 里是 author_names，前端期望 authors）。
    """
    hits = result.get("hits", {})
    total = hits.get("total", {})
    items = []
    for hit in hits.get("hits", []):
        source = hit.get("_source", {})
        # author_names → authors 别名，保持前端兼容
        if "author_names" in source and "authors" not in source:
            source = {**source, "authors": source["author_names"]}
        item = drop_empty({
            "score": hit.get("_score"),
            **source,
            "highlight": hit.get("highlight") or {},
        })
        items.append(item)

    return {
        "total": total.get("value", 0),
        "total_relation": total.get("relation", "eq"),
        "from": request.from_,
        "size": request.size,
        "mode": request.mode,
        "source": request.source,
        "items": items,
        "facets": format_aggs(result.get("aggregations", {})),
    }


def format_aggs(aggs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        name: [{"key": bucket.get("key"), "count": bucket.get("doc_count")} for bucket in agg.get("buckets", [])]
        for name, agg in aggs.items()
    }


def drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: drop_empty(item)
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [drop_empty(item) for item in value if item not in (None, "", [], {})]
    return value
