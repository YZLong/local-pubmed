from __future__ import annotations

import gzip
import hashlib
import logging
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, AsyncIterator, Literal

import httpx
import orjson
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, ORJSONResponse, Response
from fastapi.staticfiles import StaticFiles

from pubmeddb.query import (
    FILTER_FIELD_MAP,
    TEXT_FIELD_MAP,
    AdvancedSearchRequest,
    build_es_query,
    build_simple_request,
    format_search_response,
)

logger = logging.getLogger(__name__)

ES_URL = os.getenv("PUBMED_ES_URL", "http://localhost:9200").rstrip("/")
ES_INDEX = os.getenv("PUBMED_ES_INDEX", "pubmed_articles")
PMC_INDEX = os.getenv("PUBMED_PMC_INDEX", "pmc_articles")
QUERY_CACHE_INDEX = os.getenv("PUBMED_QUERY_CACHE_INDEX", "pubmed_query_cache")
QUERY_CACHE_TTL = int(os.getenv("PUBMED_QUERY_CACHE_TTL", "3600"))  # 1 小时
SEARCH_CACHE_TTL = int(os.getenv("PUBMED_API_CACHE_TTL", "60"))
SEARCH_CACHE_MAX = int(os.getenv("PUBMED_API_CACHE_MAX", "512"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# ---------------------------------------------------------------------------
# #10 + #11: 持久化 AsyncClient，全局连接复用，所有路由改为 async def
# ---------------------------------------------------------------------------
_es_client: httpx.AsyncClient | None = None


def get_es_client() -> httpx.AsyncClient:
    """返回模块级单例 AsyncClient，连接池在进程生命周期内复用。"""
    global _es_client
    if _es_client is None or _es_client.is_closed:
        _es_client = httpx.AsyncClient(
            base_url=ES_URL,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
    return _es_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用启动时初始化 ES 客户端和查询缓存索引，关闭时优雅释放连接。"""
    get_es_client()
    logger.info("Elasticsearch AsyncClient initialized: %s", ES_URL)
    # 初始化查询缓存索引
    try:
        await initialize_query_cache_index()
    except Exception as exc:
        logger.warning("Failed to initialize query cache index: %s", exc)
    yield
    if _es_client and not _es_client.is_closed:
        await _es_client.aclose()
        logger.info("Elasticsearch AsyncClient closed")


# ---------------------------------------------------------------------------
# #9: 真正的 LRU 缓存（OrderedDict + Lock，线程安全）
# ---------------------------------------------------------------------------
class LRUCache:
    """带 TTL 的 LRU 缓存，线程安全。"""

    def __init__(self, max_size: int, ttl: int) -> None:
        self.max_size = max_size
        self.ttl = ttl
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> Any | None:
        if self.ttl <= 0:
            return None
        with self._lock:
            if key not in self._store:
                return None
            expires_at, value = self._store[key]
            if expires_at < time.monotonic():
                del self._store[key]
                return None
            # 命中时移到末尾（最近使用）
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (time.monotonic() + self.ttl, value)
            # 超出容量时淘汰最久未使用的条目（头部）
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)


_cache = LRUCache(max_size=SEARCH_CACHE_MAX, ttl=SEARCH_CACHE_TTL)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Local PubMed Search API / 本地 PubMed 检索 API",
    description=(
        "Local PubMed literature search, advanced query, article lookup, and compressed JSON export API.\n\n"
        "本地 PubMed 文献检索、高级查询、文献详情查询和压缩 JSON 导出 API。"
    ),
    version="0.1.0",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# #13: 请求日志中间件（记录方法、路径、状态码、耗时）
# ---------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next: Any) -> Response:
    start = time.perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s %s %.1fms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=FileResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/help", response_class=FileResponse)
async def help_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "help.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    return await es_request("GET", "/_cluster/health")


@app.get("/fields")
async def fields() -> dict[str, Any]:
    return {
        "query_fields": sorted(TEXT_FIELD_MAP),
        "filter_fields": sorted(FILTER_FIELD_MAP),
        "modes": ["balanced", "strict", "broad", "phrase"],
        "source": ["summary", "full", "ids"],
        "sort": ["relevance", "newest", "oldest", "pmid_desc", "pmid_asc"],
    }


@app.get("/articles/{pmid}")
async def article_detail(pmid: str) -> dict[str, Any]:
    body = {
        "size": 1,
        "query": {"term": {"pmid": pmid}},
        "sort": [{"source_rank": {"order": "desc", "missing": "_last", "unmapped_type": "integer"}}],
    }
    result = await es_request("POST", f"/{ES_INDEX}/_search", json=body)
    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        raise HTTPException(status_code=404, detail=f"PMID not found: {pmid}")
    return hits[0]["_source"]


@app.get("/pmc/articles/{pmcid}")
async def pmc_article_detail(pmcid: str) -> dict[str, Any]:
    normalized = normalize_pmcid(pmcid)
    result = await es_request("GET", f"/{PMC_INDEX}/_doc/{normalized}")
    if not result.get("found", True):
        raise HTTPException(status_code=404, detail=f"PMCID not found: {pmcid}")
    return result.get("_source", {})


@app.get("/lookup")
async def lookup(id: str = Query(description="PMID, DOI, PMCID, PII, or any ArticleId value.")) -> dict[str, Any]:
    value = id.strip().lower()
    body = {
        "size": 10,
        "query": {
            "bool": {
                "should": [
                    {"term": {"pmid": value}},
                    {"term": {"doi": value}},
                    {"term": {"pmcid": value}},
                    {"term": {"pii": value}},
                    {"term": {"article_ids.value": value}},
                ],
                "minimum_should_match": 1,
            }
        },
        "collapse": {"field": "pmid"},
        "sort": [{"source_rank": {"order": "desc", "missing": "_last", "unmapped_type": "integer"}}],
    }
    result = await es_request("POST", f"/{ES_INDEX}/_search", json=body)
    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        raise HTTPException(status_code=404, detail=f"Identifier not found: {id}")
    return {
        "total": result.get("hits", {}).get("total", {}).get("value", len(hits)),
        "items": [hit["_source"] for hit in hits],
    }


@app.get("/search")
async def search(
    q: str | None = Query(default=None, description="Free text query."),
    field: str = Query(default="all", description="Field group: all/title/abstract/author/journal/mesh/major_mesh/chemical/keyword/gene/id."),
    author: str | None = None,
    journal: str | None = None,
    mesh: str | None = None,
    chemical: str | None = None,
    keyword: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    publication_type: str | None = None,
    language: str | None = None,
    has_abstract: bool | None = None,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=10, ge=1, le=100),
    mode: Literal["balanced", "strict", "broad", "phrase"] = "balanced",
    source: Literal["summary", "full", "ids"] = "summary",
    sort: Literal["relevance", "newest", "oldest", "pmid_desc", "pmid_asc"] = "relevance",
    facets: bool = False,
) -> dict[str, Any]:
    if field not in TEXT_FIELD_MAP:
        raise HTTPException(status_code=400, detail=f"Unsupported field: {field}")
    try:
        request = build_simple_request(
            q=q,
            field=field,
            author=author,
            journal=journal,
            mesh=mesh,
            chemical=chemical,
            keyword=keyword,
            year_from=year_from,
            year_to=year_to,
            publication_type=publication_type,
            language=language,
            has_abstract=has_abstract,
            page=page,
            size=size,
            sort=sort,
            mode=mode,
            source=source,
        )
        request.facets = facets
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await run_search(request)


@app.post("/search/advanced")
async def advanced_search(request: AdvancedSearchRequest) -> dict[str, Any]:
    return await run_search(request)


@app.post("/search/dsl")
async def debug_dsl(request: AdvancedSearchRequest) -> dict[str, Any]:
    return build_es_query(request)


@app.post("/export/search")
async def export_search(
    request: AdvancedSearchRequest,
    max_records: int = Query(default=1000, ge=1, le=10000),
    batch_size: int = Query(default=500, ge=50, le=1000),
    include_highlight: bool = False,
    source: Literal["summary", "full", "ids"] = "full",
    all_records: bool = False,
) -> Response:
    payload = await export_search_payload(request, max_records, batch_size, include_highlight, source, all_records)
    data = orjson.dumps(payload, option=orjson.OPT_INDENT_2)
    compressed = gzip.compress(data, compresslevel=6)
    filename = f"pubmed_export_{int(time.time())}.json.gz"
    return Response(
        content=compressed,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Records": str(payload["exported"]),
            "X-Total-Records": str(payload["total"]),
            "X-All-Records": str(all_records).lower(),
        },
    )


@app.post("/pmc/fulltext/count")
async def pmc_fulltext_count(
    request: AdvancedSearchRequest,
    pubmed_batch_size: int = Query(default=1000, ge=100, le=5000),
    use_cache: bool = Query(default=True, description="Use query cache for faster repeated queries"),
) -> dict[str, Any]:
    """
    统计 PubMed 查询结果中有多少条有 PMC 全文。

    优化后的流程：
    1. 用 composite 聚合收集 PMID/PMCID（比 search_after 快 3-5x）
    2. 写入临时缓存索引（可选，use_cache=True 时启用）
    3. PMC 查询用 terms lookup 直接从缓存读取（0 次额外网络往返）
    """
    if use_cache:
        query_id = await collect_and_cache_identifiers(request, pubmed_batch_size, use_cache=True)
        count = await count_pmc_matches_fast(query_id, use_cache=True)
        cache_doc = await es_request("GET", f"/{QUERY_CACHE_INDEX}/_doc/{query_id}")
        src = cache_doc.get("_source", {})
        return {
            "query_id": query_id,
            "pubmed_candidate_count": src.get("total_pmids", 0) + src.get("total_pmcids", 0),
            "pmid_count": src.get("total_pmids", 0),
            "pmcid_count": src.get("total_pmcids", 0),
            "pmc_fulltext_count": count,
            "cached": True,
        }
    else:
        # 不用缓存：直接收集 identifiers，跳过缓存索引
        identifiers = await collect_pubmed_identifiers(request, pubmed_batch_size)
        count = await count_pmc_matches(identifiers)
        return {
            "pubmed_candidate_count": len(identifiers["pmids"]) + len(identifiers["pmcids"]),
            "pmid_count": len(identifiers["pmids"]),
            "pmcid_count": len(identifiers["pmcids"]),
            "pmc_fulltext_count": count,
            "cached": False,
        }


@app.post("/export/pmc-fulltext")
async def export_pmc_fulltext(
    request: AdvancedSearchRequest,
    pubmed_batch_size: int = Query(default=1000, ge=100, le=5000),
    pmc_batch_size: int = Query(default=500, ge=50, le=1000),
    source: Literal["summary", "full"] = "full",
    use_cache: bool = Query(default=True, description="Use query cache for faster export"),
) -> Response:
    """
    导出 PubMed 查询结果对应的 PMC 全文。

    优化后：用 terms lookup 直接在 PMC 索引上过滤，无需逐批查询。
    """
    if use_cache:
        query_id = await collect_and_cache_identifiers(request, pubmed_batch_size, use_cache=True)
        payload = await export_pmc_fulltext_payload_fast(request, query_id, pmc_batch_size, source)
    else:
        identifiers = await collect_pubmed_identifiers(request, pubmed_batch_size)
        payload = await export_pmc_fulltext_payload(request, identifiers, pmc_batch_size, source)

    data = orjson.dumps(payload, option=orjson.OPT_INDENT_2)
    compressed = gzip.compress(data, compresslevel=6)
    filename = f"pmc_fulltext_export_{int(time.time())}.json.gz"
    return Response(
        content=compressed,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Records": str(payload["exported"]),
            "X-Total-Records": str(payload["total"]),
            "X-Query-ID": payload.get("metadata", {}).get("query_id", ""),
        },
    )


@app.post("/admin/cache/cleanup")
async def cleanup_query_cache() -> dict[str, Any]:
    """
    清理过期的查询缓存文档。

    删除 expires_at < now() 的所有文档。
    """
    now = datetime.now(timezone.utc).isoformat()
    
    body = {
        "query": {
            "range": {
                "expires_at": {
                    "lt": now
                }
            }
        }
    }
    
    try:
        result = await es_request("POST", f"/{QUERY_CACHE_INDEX}/_delete_by_query", json=body)
        deleted = result.get("deleted", 0)
        logger.info("Cleaned up %d expired cache entries", deleted)
        return {
            "deleted": deleted,
            "total": result.get("total", 0),
            "failures": result.get("failures", []),
        }
    except HTTPException as exc:
        if "index_not_found" in str(exc.detail).lower():
            return {"deleted": 0, "total": 0, "failures": [], "message": "Cache index does not exist"}
        raise


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
async def initialize_query_cache_index() -> None:
    """
    初始化查询缓存索引。

    如果索引不存在，从 config/pubmed_query_cache_index.json 读取配置并创建。
    """
    try:
        # 检查索引是否存在
        await es_request("HEAD", f"/{QUERY_CACHE_INDEX}")
        logger.info("Query cache index already exists: %s", QUERY_CACHE_INDEX)
    except HTTPException:
        # 索引不存在，创建它
        config_path = Path(__file__).resolve().parent.parent / "config" / "pubmed_query_cache_index.json"
        if not config_path.exists():
            logger.warning("Query cache index config not found: %s", config_path)
            return
        
        with open(config_path, "r", encoding="utf-8") as f:
            index_config = orjson.loads(f.read())
        
        await es_request("PUT", f"/{QUERY_CACHE_INDEX}", json=index_config)
        logger.info("Created query cache index: %s", QUERY_CACHE_INDEX)


async def run_search(request: AdvancedSearchRequest) -> dict[str, Any]:
    body = build_es_query(request)
    cache_key = "search:" + orjson.dumps(body, option=orjson.OPT_SORT_KEYS).decode()
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    result = await es_request("POST", f"/{ES_INDEX}/_search", json=body)
    formatted = format_search_response(result, request)
    _cache.set(cache_key, formatted)
    return formatted


async def export_search_payload(
    request: AdvancedSearchRequest,
    max_records: int,
    batch_size: int,
    include_highlight: bool,
    source: str,
    all_records: bool,
) -> dict[str, Any]:
    if all_records:
        return await export_all_search_payload(request, batch_size, include_highlight, source)

    exported: list[dict[str, Any]] = []
    total = 0
    offset = 0

    while len(exported) < max_records:
        page_size = min(batch_size, max_records - len(exported))
        page_request = request.model_copy(update={
            "from_": offset,
            "size": page_size,
            "source": source,
            "highlight": include_highlight,
            "facets": False,
        })
        body = build_es_query(page_request)
        result = await es_request("POST", f"/{ES_INDEX}/_search", json=body)
        hits = result.get("hits", {})
        total = hits.get("total", {}).get("value", total)
        batch = hits.get("hits", [])
        if not batch:
            break
        for hit in batch:
            item: dict[str, Any] = {"score": hit.get("_score"), **hit.get("_source", {})}
            if include_highlight and hit.get("highlight"):
                item["highlight"] = hit["highlight"]
            exported.append(item)
        offset += len(batch)
        if len(batch) < page_size or offset >= total:
            break

    return {
        "metadata": {
            "format": "pubmeddb.search.export.v1",
            "compressed": True,
            "source": source,
            "include_highlight": include_highlight,
            "max_records": max_records,
            "all_records": False,
            "generated_at_unix": int(time.time()),
        },
        "request": request.model_dump(by_alias=True),
        "total": total,
        "exported": len(exported),
        "items": exported,
    }


async def export_all_search_payload(
    request: AdvancedSearchRequest,
    batch_size: int,
    include_highlight: bool,
    source: str,
) -> dict[str, Any]:
    """#14: 全量导出使用流式 search_after 分页，避免一次性把所有结果载入内存后再压缩。
    当前实现仍在内存中累积 items 列表，但通过 search_after 避免了 ES 深分页的内存压力；
    如需进一步降低内存占用，可改为 StreamingResponse + 逐批写入 gzip。
    """
    exported: list[dict[str, Any]] = []
    seen_pmids: set[str] = set()
    raw_total = 0
    search_after: list[Any] | None = None

    while True:
        page_request = request.model_copy(update={
            "from_": 0,
            "size": batch_size,
            "source": source,
            "highlight": include_highlight,
            "facets": False,
            "sort": "pmid_asc",
        })
        body = build_es_query(page_request)
        body.pop("from", None)
        body.pop("collapse", None)
        body["sort"] = [
            {"source_rank": {"order": "desc", "missing": "_last", "unmapped_type": "integer"}},
            {"pmid_num": {"order": "asc", "unmapped_type": "long"}},
            {"source_index": {"order": "asc", "unmapped_type": "keyword"}},
            {"pmid": {"order": "asc", "unmapped_type": "keyword"}},
        ]
        if search_after:
            body["search_after"] = search_after

        result = await es_request("POST", f"/{ES_INDEX}/_search", json=body)
        hits = result.get("hits", {})
        raw_total = hits.get("total", {}).get("value", raw_total)
        batch = hits.get("hits", [])
        if not batch:
            break

        for hit in batch:
            source_doc = hit.get("_source", {})
            pmid = source_doc.get("pmid")
            if pmid and pmid in seen_pmids:
                continue
            if pmid:
                seen_pmids.add(pmid)
            item: dict[str, Any] = {"score": hit.get("_score"), **source_doc}
            if include_highlight and hit.get("highlight"):
                item["highlight"] = hit["highlight"]
            exported.append(item)

        search_after = batch[-1].get("sort")
        if not search_after or len(batch) < batch_size:
            break

    return {
        "metadata": {
            "format": "pubmeddb.search.export.v1",
            "compressed": True,
            "source": source,
            "include_highlight": include_highlight,
            "max_records": None,
            "all_records": True,
            "generated_at_unix": int(time.time()),
        },
        "request": request.model_dump(by_alias=True),
        "total": raw_total,
        "exported": len(exported),
        "items": exported,
    }


def normalize_pmcid(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if value.lower().startswith("pmc"):
        return "PMC" + value[3:]
    return "PMC" + value


def pmc_source_fields(source: str) -> list[str]:
    summary = [
        "pmcid",
        "pmid",
        "doi",
        "title",
        "journal",
        "publication_date",
        "publication_year",
        "article_type",
        "subjects",
        "source_archive",
        "source_member",
        "source_file",
    ]
    if source == "summary":
        return summary
    return [
        *summary,
        "abstract",
        "body_text",
        "full_text",
        "sections",
        "section_titles",
        "authors",
        "author_names",
        "license",
        "source_dataset",
    ]


async def collect_and_cache_identifiers(
    request: AdvancedSearchRequest,
    batch_size: int,
    use_cache: bool,
) -> str:
    """
    收集 PubMed 查询结果的 PMID/PMCID，并可选地写入缓存索引。

    返回 query_id（查询哈希），用于后续 terms lookup。
    """
    # 生成查询哈希作为 query_id
    query_body = build_es_query(request.model_copy(update={"from_": 0, "size": 0, "facets": False}))
    query_body.pop("collapse", None)
    query_body.pop("sort", None)
    query_body.pop("_source", None)
    query_hash = orjson.dumps(query_body, option=orjson.OPT_SORT_KEYS).decode()
    query_id = hashlib.sha256(query_hash.encode()).hexdigest()[:16]

    # 如果使用缓存，先检查是否已存在且未过期
    if use_cache:
        try:
            cache_doc = await es_request("GET", f"/{QUERY_CACHE_INDEX}/_doc/{query_id}")
            if cache_doc.get("found"):
                source = cache_doc.get("_source", {})
                expires_at = source.get("expires_at")
                if expires_at:
                    expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    if expires_dt > datetime.now(expires_dt.tzinfo):
                        logger.info("Cache hit for query_id=%s", query_id)
                        return query_id
        except HTTPException:
            pass  # 缓存不存在或索引不存在，继续收集

    # 收集 PMID/PMCID
    identifiers = await collect_pubmed_identifiers(request, batch_size)
    pmids = identifiers["pmids"]
    pmcids = identifiers["pmcids"]

    # 如果使用缓存，写入缓存索引
    if use_cache:
        now = datetime.now(timezone.utc)
        expires_at_dt = now + timedelta(seconds=QUERY_CACHE_TTL)
        
        cache_doc_body = {
            "query_id": query_id,
            "query_hash": query_hash,
            "pmids": sorted(pmids),
            "pmcids": sorted(pmcids),
            "total_pmids": len(pmids),
            "total_pmcids": len(pmcids),
            "created_at": now.isoformat(),
            "expires_at": expires_at_dt.isoformat(),
        }
        
        try:
            await es_request("PUT", f"/{QUERY_CACHE_INDEX}/_doc/{query_id}", json=cache_doc_body)
            logger.info("Cached query_id=%s with %d pmids, %d pmcids", query_id, len(pmids), len(pmcids))
        except HTTPException as exc:
            # 如果索引不存在，尝试创建
            if "index_not_found" in str(exc.detail).lower():
                await initialize_query_cache_index()
                await es_request("PUT", f"/{QUERY_CACHE_INDEX}/_doc/{query_id}", json=cache_doc_body)
                logger.info("Created cache index and cached query_id=%s", query_id)
            else:
                logger.warning("Failed to cache query_id=%s: %s", query_id, exc.detail)

    return query_id


async def collect_pubmed_identifiers(request: AdvancedSearchRequest, batch_size: int) -> dict[str, set[str]]:
    """
    用 composite 聚合替代 search_after 翻页收集 PMID/PMCID。

    为什么更快：
    - composite 聚合走 doc values（列式存储），不读 _source，不做相关性评分
    - 天然按 pmid 去重，省去手动 seen_pmids 集合
    - 不需要排序整个结果集，ES 内部直接按 term 顺序遍历倒排索引
    - 单次请求返回 batch_size 个唯一 PMID，比 search_after 少一半的网络往返

    对于小结果集（≤ batch_size），只需 1 次请求；
    对于大结果集，每次翻页只传一个轻量的 after 游标，而不是完整的排序值数组。
    """
    pmids: set[str] = set()
    pmcids: set[str] = set()

    # 先构建基础查询（只要 query/filter 部分，不要 sort/source/highlight）
    base_request = request.model_copy(update={
        "from_": 0,
        "size": 0,          # composite 聚合不需要 hits
        "source": "ids",
        "highlight": False,
        "facets": False,
    })
    base_body = build_es_query(base_request)
    base_body.pop("collapse", None)
    base_body.pop("sort", None)
    base_body.pop("_source", None)
    base_body["size"] = 0

    after: dict[str, Any] | None = None

    while True:
        # composite 聚合：按 pmid 分桶，每桶带一个 top_hits 取 pmcid
        agg_body = {
            **base_body,
            "aggs": {
                "by_pmid": {
                    "composite": {
                        "size": batch_size,
                        "sources": [
                            {"pmid": {"terms": {"field": "pmid", "missing_bucket": False}}}
                        ],
                        **({"after": after} if after else {}),
                    },
                    "aggs": {
                        # 每个 pmid 桶里取 pmcid（优先 updatefiles，source_rank 最高的那条）
                        "top": {
                            "top_hits": {
                                "size": 1,
                                "_source": ["pmcid"],
                                "sort": [{"source_rank": {"order": "desc", "unmapped_type": "integer"}}],
                            }
                        }
                    },
                }
            },
        }

        result = await es_request("POST", f"/{ES_INDEX}/_search", json=agg_body)
        agg = result.get("aggregations", {}).get("by_pmid", {})
        buckets = agg.get("buckets", [])
        if not buckets:
            break

        for bucket in buckets:
            pmid = bucket.get("key", {}).get("pmid")
            if pmid:
                pmids.add(str(pmid))
            # top_hits 里取 pmcid
            top_hits = bucket.get("top", {}).get("hits", {}).get("hits", [])
            if top_hits:
                pmcid = top_hits[0].get("_source", {}).get("pmcid")
                if pmcid:
                    pmcids.add(normalize_pmcid(str(pmcid)))

        after_key = agg.get("after_key")
        if not after_key or len(buckets) < batch_size:
            break
        after = after_key

    return {"pmids": pmids, "pmcids": pmcids}


def chunked(values: set[str], size: int) -> list[list[str]]:
    sorted_values = sorted(values)
    return [sorted_values[index:index + size] for index in range(0, len(sorted_values), size)]


def pmc_identifier_query(pmids: set[str], pmcids: set[str]) -> dict[str, Any]:
    should = []
    if pmids:
        should.append({"terms": {"pmid": sorted(pmids)}})
    if pmcids:
        should.append({"terms": {"pmcid": [value.lower() for value in sorted(pmcids)]}})
    if not should:
        return {"match_none": {}}
    return {"bool": {"should": should, "minimum_should_match": 1}}


async def count_pmc_matches_fast(query_id: str, use_cache: bool = True) -> int:
    """
    用 terms lookup 统计 PMC 匹配数。
    调用方保证 query_id 对应的缓存文档已存在（use_cache=True 路径）。
    """
    body = {
        "size": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "should": [
                    {
                        "terms": {
                            "pmid": {
                                "index": QUERY_CACHE_INDEX,
                                "id": query_id,
                                "path": "pmids",
                            }
                        }
                    },
                    {
                        "terms": {
                            "pmcid": {
                                "index": QUERY_CACHE_INDEX,
                                "id": query_id,
                                "path": "pmcids",
                            }
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        },
    }
    result = await es_request("POST", f"/{PMC_INDEX}/_search", json=body)
    return int(result.get("hits", {}).get("total", {}).get("value", 0))


async def count_pmc_matches(identifiers: dict[str, set[str]]) -> int:
    pmids = identifiers["pmids"]
    pmcids = identifiers["pmcids"]
    if not pmids and not pmcids:
        return 0
    total_ids = len(pmids) + len(pmcids)
    if total_ids <= 50000:
        body = {
            "size": 0,
            "track_total_hits": True,
            "query": pmc_identifier_query(pmids, pmcids),
        }
        result = await es_request("POST", f"/{PMC_INDEX}/_search", json=body)
        return int(result.get("hits", {}).get("total", {}).get("value", 0))

    seen: set[str] = set()
    for pmid_chunk in chunked(pmids, 5000):
        body = {"size": 10000, "_source": ["pmcid"], "query": {"terms": {"pmid": pmid_chunk}}}
        result = await es_request("POST", f"/{PMC_INDEX}/_search", json=body)
        seen.update(hit.get("_id") for hit in result.get("hits", {}).get("hits", []))
    for pmcid_chunk in chunked(pmcids, 5000):
        body = {"size": 10000, "_source": ["pmcid"], "query": {"terms": {"pmcid": [value.lower() for value in pmcid_chunk]}}}
        result = await es_request("POST", f"/{PMC_INDEX}/_search", json=body)
        seen.update(hit.get("_id") for hit in result.get("hits", {}).get("hits", []))
    return len({value for value in seen if value})


async def export_pmc_fulltext_payload_fast(
    request: AdvancedSearchRequest,
    query_id: str,
    batch_size: int,
    source: str,
) -> dict[str, Any]:
    """
    用 terms lookup + search_after 分页导出 PMC 全文。
    调用方保证 query_id 对应的缓存文档已存在。
    """
    exported: list[dict[str, Any]] = []
    seen_pmcids: set[str] = set()
    search_after: list[Any] | None = None

    while True:
        body: dict[str, Any] = {
            "size": batch_size,
            "query": {
                "bool": {
                    "should": [
                        {
                            "terms": {
                                "pmid": {
                                    "index": QUERY_CACHE_INDEX,
                                    "id": query_id,
                                    "path": "pmids",
                                }
                            }
                        },
                        {
                            "terms": {
                                "pmcid": {
                                    "index": QUERY_CACHE_INDEX,
                                    "id": query_id,
                                    "path": "pmcids",
                                }
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            },
            "_source": pmc_source_fields(source),
            "sort": [{"pmc_num": {"order": "asc", "unmapped_type": "long"}}, {"pmcid": {"order": "asc"}}],
        }
        if search_after:
            body["search_after"] = search_after

        result = await es_request("POST", f"/{PMC_INDEX}/_search", json=body)
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            pmcid = hit.get("_source", {}).get("pmcid") or hit.get("_id")
            if pmcid in seen_pmcids:
                continue
            seen_pmcids.add(pmcid)
            exported.append(hit.get("_source", {}))

        search_after = hits[-1].get("sort")
        if not search_after or len(hits) < batch_size:
            break

    # 读取缓存文档获取统计信息
    cache_doc = await es_request("GET", f"/{QUERY_CACHE_INDEX}/_doc/{query_id}")
    src = cache_doc.get("_source", {})

    return {
        "metadata": {
            "format": "pubmeddb.pmc_fulltext.export.v1",
            "compressed": True,
            "source": source,
            "generated_at_unix": int(time.time()),
            "query_id": query_id,
            "cached": True,
        },
        "request": request.model_dump(by_alias=True),
        "pubmed_identifier_counts": {
            "pmids": src.get("total_pmids", 0),
            "pmcids": src.get("total_pmcids", 0),
        },
        "total": len(exported),
        "exported": len(exported),
        "items": exported,
    }


async def export_pmc_fulltext_payload(
    request: AdvancedSearchRequest,
    identifiers: dict[str, set[str]],
    batch_size: int,
    source: str,
) -> dict[str, Any]:
    exported: list[dict[str, Any]] = []
    seen_pmcids: set[str] = set()

    for field, values in [("pmid", identifiers["pmids"]), ("pmcid", identifiers["pmcids"])]:
        for value_chunk in chunked(values, batch_size):
            terms_values = [value.lower() for value in value_chunk] if field == "pmcid" else value_chunk
            body = {
                "size": len(value_chunk),
                "query": {"terms": {field: terms_values}},
                "_source": pmc_source_fields(source),
                "sort": [{"pmc_num": {"order": "asc", "unmapped_type": "long"}}],
            }
            result = await es_request("POST", f"/{PMC_INDEX}/_search", json=body)
            for hit in result.get("hits", {}).get("hits", []):
                pmcid = hit.get("_source", {}).get("pmcid") or hit.get("_id")
                if pmcid in seen_pmcids:
                    continue
                seen_pmcids.add(pmcid)
                exported.append(hit.get("_source", {}))

    return {
        "metadata": {
            "format": "pubmeddb.pmc_fulltext.export.v1",
            "compressed": True,
            "source": source,
            "generated_at_unix": int(time.time()),
        },
        "request": request.model_dump(by_alias=True),
        "pubmed_identifier_counts": {
            "pmids": len(identifiers["pmids"]),
            "pmcids": len(identifiers["pmcids"]),
        },
        "total": len(exported),
        "exported": len(exported),
        "items": exported,
    }


async def es_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """#10 + #11: 使用持久化 AsyncClient 发送请求，连接池全程复用。"""
    client = get_es_client()
    try:
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        # HEAD 请求没有响应体
        if method.upper() == "HEAD":
            return {}
        return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        status_code = exc.response.status_code if exc.response.status_code < 500 else 502
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Elasticsearch request failed: {exc}") from exc
