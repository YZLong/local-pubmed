from __future__ import annotations

import gzip
import logging
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
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
    """应用启动时初始化 ES 客户端，关闭时优雅释放连接。"""
    get_es_client()
    logger.info("Elasticsearch AsyncClient initialized: %s", ES_URL)
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
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


async def es_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """#10 + #11: 使用持久化 AsyncClient 发送请求，连接池全程复用。"""
    client = get_es_client()
    try:
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        status_code = exc.response.status_code if exc.response.status_code < 500 else 502
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Elasticsearch request failed: {exc}") from exc
