"""
#3: 单元测试 —— query.py（ES DSL 构建、LRU 缓存）
"""
from __future__ import annotations

import time

import pytest

from pubmeddb.api import LRUCache
from pubmeddb.query import (
    AdvancedSearchRequest,
    FieldQuery,
    TermFilter,
    build_es_query,
    build_simple_request,
    format_aggs,
    normalize_filter_values,
    source_fields,
)


# ---------------------------------------------------------------------------
# LRUCache
# ---------------------------------------------------------------------------

class TestLRUCache:
    def test_basic_set_get(self):
        cache = LRUCache(max_size=10, ttl=60)
        cache.set("k", {"v": 1})
        assert cache.get("k") == {"v": 1}

    def test_miss_returns_none(self):
        cache = LRUCache(max_size=10, ttl=60)
        assert cache.get("missing") is None

    def test_ttl_expiry(self):
        cache = LRUCache(max_size=10, ttl=1)
        cache.set("k", "value")
        assert cache.get("k") == "value"
        time.sleep(1.1)
        assert cache.get("k") is None

    def test_lru_eviction(self):
        """容量满时淘汰最久未使用的条目。"""
        cache = LRUCache(max_size=3, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        # 访问 a，使 b 成为最久未使用
        cache.get("a")
        cache.set("d", 4)  # 应淘汰 b
        assert cache.get("b") is None
        assert cache.get("a") == 1
        assert cache.get("c") == 3
        assert cache.get("d") == 4

    def test_ttl_zero_disables_cache(self):
        cache = LRUCache(max_size=10, ttl=0)
        cache.set("k", "v")
        assert cache.get("k") is None

    def test_overwrite_moves_to_end(self):
        """覆写已有 key 时应移到末尾（最近使用）。"""
        cache = LRUCache(max_size=2, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("a", 99)  # 覆写 a，a 变为最近使用
        cache.set("c", 3)   # 应淘汰 b（最久未使用）
        assert cache.get("b") is None
        assert cache.get("a") == 99


# ---------------------------------------------------------------------------
# source_fields
# ---------------------------------------------------------------------------

class TestSourceFields:
    def test_summary_fields(self):
        fields = source_fields("summary")
        assert "pmid" in fields
        assert "title" in fields
        # summary 不包含 abstract_sections
        assert "abstract_sections" not in fields

    def test_full_fields(self):
        fields = source_fields("full")
        assert "abstract_sections" in fields
        assert "authors" in fields

    def test_ids_fields(self):
        fields = source_fields("ids")
        assert "pmid" in fields
        assert "doi" in fields
        # ids profile 不含 abstract
        assert "abstract" not in fields


# ---------------------------------------------------------------------------
# normalize_filter_values
# ---------------------------------------------------------------------------

class TestNormalizeFilterValues:
    def test_case_insensitive_field(self):
        result = normalize_filter_values("mesh", ["Esophageal Neoplasms"])
        assert result == ["esophageal neoplasms"]

    def test_case_sensitive_field(self):
        # pmid 不在 CASE_INSENSITIVE_FILTERS 中
        result = normalize_filter_values("pmid", ["12345678"])
        assert result == ["12345678"]


# ---------------------------------------------------------------------------
# build_es_query
# ---------------------------------------------------------------------------

class TestBuildEsQuery:
    def _req(self, **kwargs) -> AdvancedSearchRequest:
        defaults = dict(query="esophageal cancer", mode="balanced", size=10)
        defaults.update(kwargs)
        return AdvancedSearchRequest(**defaults)

    def test_basic_query_structure(self):
        body = build_es_query(self._req())
        assert "query" in body
        assert "bool" in body["query"]
        assert body["size"] == 10
        assert "collapse" in body

    def test_match_all_when_no_query(self):
        req = AdvancedSearchRequest(size=10)
        body = build_es_query(req)
        assert body["query"] == {"match_all": {}}

    def test_year_range_filter(self):
        body = build_es_query(self._req(year_from=2010, year_to=2020))
        filters = body["query"]["bool"]["filter"]
        year_filter = next(f for f in filters if "range" in f and "publication_year" in f["range"])
        assert year_filter["range"]["publication_year"]["gte"] == 2010
        assert year_filter["range"]["publication_year"]["lte"] == 2020

    def test_has_abstract_filter(self):
        body = build_es_query(self._req(has_abstract=True))
        filters = body["query"]["bool"]["filter"]
        assert any("exists" in f and f["exists"]["field"] == "abstract" for f in filters)

    def test_has_no_abstract_must_not(self):
        body = build_es_query(self._req(has_abstract=False))
        must_not = body["query"]["bool"]["must_not"]
        assert any("exists" in f and f["exists"]["field"] == "abstract" for f in must_not)

    def test_term_filter(self):
        req = self._req(filters=[TermFilter(field="language", values=["eng"])])
        body = build_es_query(req)
        filters = body["query"]["bool"]["filter"]
        assert any("terms" in f and "language" in f["terms"] for f in filters)

    def test_facets_included_when_requested(self):
        body = build_es_query(self._req(facets=True))
        assert "aggs" in body

    def test_facets_excluded_by_default(self):
        body = build_es_query(self._req(facets=False))
        assert "aggs" not in body

    def test_highlight_included_by_default(self):
        body = build_es_query(self._req(highlight=True))
        assert "highlight" in body

    def test_phrase_mode(self):
        body = build_es_query(self._req(mode="phrase"))
        must = body["query"]["bool"]["must"]
        assert any(
            "multi_match" in c and c["multi_match"].get("type") == "phrase"
            for c in must
        )

    def test_broad_mode_uses_or(self):
        body = build_es_query(self._req(mode="broad"))
        must = body["query"]["bool"]["must"]
        main = must[0]["multi_match"]
        assert main["operator"] == "or"

    def test_sort_newest(self):
        body = build_es_query(self._req(sort="newest"))
        assert body["sort"][0] == {
            "publication_date": {"order": "desc", "missing": "_last", "unmapped_type": "date"}
        }

    def test_sort_pmid_asc(self):
        body = build_es_query(self._req(sort="pmid_asc"))
        assert body["sort"][0] == {"pmid_num": {"order": "asc", "unmapped_type": "long"}}

    def test_field_query_must(self):
        req = self._req(field_queries=[FieldQuery(field="author", query="Smith", occur="must")])
        body = build_es_query(req)
        must = body["query"]["bool"]["must"]
        assert len(must) >= 2  # main query + field query

    def test_field_query_must_not(self):
        req = self._req(field_queries=[FieldQuery(field="author", query="Smith", occur="must_not")])
        body = build_es_query(req)
        must_not = body["query"]["bool"]["must_not"]
        assert len(must_not) >= 1


# ---------------------------------------------------------------------------
# build_simple_request
# ---------------------------------------------------------------------------

class TestBuildSimpleRequest:
    def test_basic(self):
        req = build_simple_request(
            q="cancer", field="all", author=None, journal=None,
            mesh=None, chemical=None, keyword=None,
            year_from=2010, year_to=2020,
            publication_type=None, language="eng",
            has_abstract=None, page=1, size=10,
            sort="relevance", mode="balanced", source="summary",
        )
        assert req.query == "cancer"
        assert req.year_from == 2010
        assert req.year_to == 2020
        assert any(f.field == "language" for f in req.filters)

    def test_pagination_offset(self):
        req = build_simple_request(
            q="x", field="all", author=None, journal=None,
            mesh=None, chemical=None, keyword=None,
            year_from=None, year_to=None,
            publication_type=None, language=None,
            has_abstract=None, page=3, size=20,
            sort="relevance", mode="balanced", source="summary",
        )
        assert req.from_ == 40  # (3-1) * 20

    def test_author_becomes_field_query(self):
        req = build_simple_request(
            q=None, field="all", author="Smith", journal=None,
            mesh=None, chemical=None, keyword=None,
            year_from=None, year_to=None,
            publication_type=None, language=None,
            has_abstract=None, page=1, size=10,
            sort="relevance", mode="balanced", source="summary",
        )
        assert any(fq.field == "author" and fq.query == "Smith" for fq in req.field_queries)


# ---------------------------------------------------------------------------
# format_aggs
# ---------------------------------------------------------------------------

class TestFormatAggs:
    def test_basic(self):
        aggs = {
            "years": {"buckets": [{"key": 2020, "doc_count": 100}, {"key": 2019, "doc_count": 80}]}
        }
        result = format_aggs(aggs)
        assert result["years"][0] == {"key": 2020, "count": 100}

    def test_empty(self):
        assert format_aggs({}) == {}
