# Local PubMed Elasticsearch

This repository builds a local PubMed literature search index from NLM PubMed XML gzip files in `data/updatefiles`.

## 1. Start Elasticsearch

```bash
docker compose up -d
```

The compose file uses the local image `harbor.tt.com/yuzho/elastic/elasticsearch:8.19.0`, disables security for local development, and persists data in `./esdata`.

Check readiness:

```bash
curl http://localhost:9200/_cluster/health?pretty
```

## 2. Import PubMed XML

Create the local Python environment and install dependencies with `uv`:

```bash
uv sync
```

The recommended local layout mirrors the NCBI PubMed FTP folders:

```text
data/baseline/
data/updatefiles/
```

Build the mostly static baseline index once:

```bash
uv run python scripts/import_pubmed_to_es.py \
  --input data/baseline \
  --source-dataset baseline \
  --index pubmed_baseline_v1 \
  --alias pubmed_baseline \
  --read-alias pubmed_articles \
  --reset-read-alias \
  --recreate
```

Build or refresh the updatefiles index:

```bash
uv run python scripts/import_pubmed_to_es.py \
  --input data/updatefiles \
  --source-dataset updatefiles \
  --index pubmed_updates_v1 \
  --alias pubmed_updates \
  --read-alias pubmed_articles \
  --recreate
```

For later incremental updatefile imports, omit `--recreate` so PMID documents are upserted into the update index:

```bash
uv run python scripts/import_pubmed_to_es.py \
  --input data/updatefiles \
  --source-dataset updatefiles \
  --index pubmed_updates_v1 \
  --alias pubmed_updates \
  --read-alias pubmed_articles
```

In this design:

- `pubmed_baseline_v1`: baseline write index, normally imported once.
- `pubmed_updates_v1`: updatefiles write index, repeatedly upserted as FTP update files change.
- `pubmed_baseline`: exclusive alias for the current baseline index.
- `pubmed_updates`: exclusive alias for the current updates index.
- `pubmed_articles`: multi-index read alias used by the API and web UI.

The importer stores `source_dataset`, `source_index`, and `source_rank` in each document. Search collapses by PMID and prefers `updatefiles` over `baseline` when the same PMID exists in both.

Test with a small subset first:

```bash
uv run python scripts/import_pubmed_to_es.py \
  --input data/updatefiles \
  --source-dataset updatefiles \
  --index pubmed_updates_v1 \
  --alias pubmed_updates \
  --read-alias pubmed_articles \
  --recreate \
  --limit 1000
```

Legacy single-index import is still possible by setting only `--index` and `--alias`:

```bash
uv run python scripts/import_pubmed_to_es.py --index pubmed_articles_v2 --alias pubmed_articles --recreate
```

The importer streams `*.xml.gz` files and uses PMID as the Elasticsearch `_id`, so repeated imports update existing documents instead of creating duplicates inside the same write index.

Retraction handling is derived during import from `PublicationTypeList` and `CommentsCorrectionsList`.

- `publication_status_normalized`: `active`, `retracted`, `retraction_notice`, or `expression_of_concern`.
- `publication_status_flags`: booleans such as `is_retracted`, `is_retraction_notice`, `has_retraction_relation`, and `has_expression_of_concern`.
- `retraction`: normalized PMID relationships such as `retracted_by_pmids` and `retracts_pmids`.

When a later updatefile contains the same PMID, the importer indexes with the same `_id`, so the newer XML record fully overwrites the previous document in that write index. If PubMed publishes a revised record for the original PMID with `Retracted Publication` or `RetractionIn`, the local document is updated and tagged as retracted. If the updatefile only contains a separate Retraction Notice PMID with `RetractionOf`, that notice is tagged and linked, but the original article is only tagged if its own revised XML record is also present.

## 3. Search

```bash
uv run python scripts/search_pubmed.py "esophageal cancer GNAS"
```

The default search targets title, abstract, MeSH terms, author names, journal title, keywords, and chemicals. PMID and DOI are stored as exact keyword fields for backend lookups.

## PMC OA Full Text

PMC OA Bulk JATS XML archives can be indexed into a separate full-text index:

```bash
uv run python scripts/import_pmc_oa_to_es.py \
  --input data/pmc/oa_bulk \
  --index pmc_articles_v1 \
  --alias pmc_articles \
  --mapping config/pmc_index.json \
  --recreate
```

The importer streams `*.tar.gz` archives directly and uses `pmcid` as the Elasticsearch `_id`. Each document records `source_archive`, `source_member`, and `source_file` so the exact source tarball and XML member can be traced.

After PMC indexing is available, the web UI shows how many PMC full-text documents match the current PubMed search result set. The count and export are based on PMID/PMCID linkage between `pubmed_articles` and `pmc_articles`.

Useful PMC endpoints:

- `GET /pmc/articles/{pmcid}`: PMC full-text detail lookup.
- `POST /pmc/fulltext/count`: count PMC full-text records corresponding to a PubMed advanced search request.
- `POST /export/pmc-fulltext`: export the corresponding PMC full-text records as compressed `.json.gz`.

Example:

```bash
curl -X POST 'http://127.0.0.1:8000/pmc/fulltext/count' \
  -H 'Content-Type: application/json' \
  -d '{"query":"type 1 diabetes glucagon","mode":"balanced","source":"ids"}'

curl -o pmc_fulltext.json.gz \
  -X POST 'http://127.0.0.1:8000/export/pmc-fulltext?source=full' \
  -H 'Content-Type: application/json' \
  -d '{"query":"type 1 diabetes glucagon","mode":"balanced","source":"ids"}'
```

## 4. Search API

Start the local search app:

```bash
uv run uvicorn pubmeddb.api:app --host 127.0.0.1 --port 8000
```

Open the web UI:

```text
http://127.0.0.1:8000/
```

Open the bilingual help page:

```text
http://127.0.0.1:8000/help
```

Useful endpoints:

- `GET /health`: Elasticsearch health.
- `GET /fields`: supported query fields, filter fields, and sort modes.
- `GET /articles/{pmid}`: PMID detail lookup.
- `GET /pmc/articles/{pmcid}`: PMC full-text detail lookup.
- `GET /lookup?id=...`: PMID, DOI, PMCID, PII, or ArticleId lookup.
- `GET /search`: simple query interface for frontend forms.
- `POST /search/advanced`: compound query interface.
- `POST /pmc/fulltext/count`: PMC full-text count for the current PubMed query.
- `POST /search/dsl`: debug endpoint that returns the Elasticsearch DSL generated from an advanced request.
- `POST /export/search`: export search results as compressed `.json.gz`.
- `POST /export/pmc-fulltext`: export matching PMC full text as compressed `.json.gz`.

Search modes:

- `balanced`: default; all terms are required, with phrase and identifier boosts.
- `strict`: all terms are required without phrase boosting.
- `broad`: expands recall using OR plus `minimum_should_match`.
- `phrase`: requires phrase matching.

Source profiles:

- `summary`: default list payload with identifiers, title, journal, authors, MeSH, chemicals, and highlight snippets.
- `ids`: compact payload for fast identifier-oriented result lists.
- `full`: full search result source; use detail pages for large records when possible.

Simple examples:

```bash
curl 'http://127.0.0.1:8000/search?q=esophageal%20cancer%20GNAS&size=5'
curl 'http://127.0.0.1:8000/search?q=esophageal%20cancer%20GNAS&mode=broad&source=ids&size=5'
curl 'http://127.0.0.1:8000/search?field=title&q=single%20nucleotide%20polymorphism&year_from=2010&year_to=2012'
curl 'http://127.0.0.1:8000/search?mesh=Esophageal%20Neoplasms&chemical=GNAS&language=eng&facets=true'
curl -X POST 'http://127.0.0.1:8000/search/advanced' \
  -H 'Content-Type: application/json' \
  -d '{"filters":[{"field":"publication_status_normalized","values":["retracted"]}],"size":5,"facets":true}'
curl 'http://127.0.0.1:8000/articles/21340746'
curl 'http://127.0.0.1:8000/lookup?id=10.1007/s13402-011-0016-x'
```

Export compressed JSON:

```bash
curl -o pubmed_export.json.gz \
  -X POST 'http://127.0.0.1:8000/export/search?max_records=1000&source=full' \
  -H 'Content-Type: application/json' \
  -d '{"query":"esophageal cancer GNAS","mode":"balanced","source":"summary"}'
```

Export all matching records as compressed JSON:

```bash
curl -o pubmed_export_all.json.gz \
  -X POST 'http://127.0.0.1:8000/export/search?all_records=true&source=full' \
  -H 'Content-Type: application/json' \
  -d '{"query":"alpha-fetoprotein cystic fibrosis","mode":"balanced","source":"summary"}'
```

Fixed-size exports preserve the normal search ordering and are capped at `10000` records. Full exports use `search_after` deep pagination and PMID de-duplication so they can export beyond the Elasticsearch default result window.

Advanced example:

```bash
curl -X POST 'http://127.0.0.1:8000/search/advanced' \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "esophageal cancer",
    "query_fields": ["title", "abstract", "mesh"],
    "mode": "balanced",
    "operator": "and",
    "field_queries": [
      {"field": "chemical", "query": "GNAS", "occur": "must"},
      {"field": "author", "query": "Vashist", "occur": "should"}
    ],
    "filters": [
      {"field": "language", "values": ["eng"]},
      {"field": "publication_type", "values": ["Journal Article"]}
    ],
    "year_from": 2010,
    "year_to": 2012,
    "has_abstract": true,
    "sort": "relevance",
    "source": "summary",
    "facets": true,
    "size": 5
  }'
```

Runtime cache controls:

- `PUBMED_API_CACHE_TTL`: in-process search cache TTL in seconds; default `60`, set `0` to disable.
- `PUBMED_API_CACHE_MAX`: max cached search responses per process; default `512`.

The web UI supports PubMed-style tags in the main search box:

- `single nucleotide polymorphism[Title]`
- `Vashist[Author]`
- `Cell Oncol[Journal]`
- `Esophageal Neoplasms[MeSH]`
- `"Breast Neoplasms"[MeSH]` for exact MeSH descriptor matching
- `GNAS[Chemical]`
- `2010:2012[Year]`

For API callers, whole-term exact matching should use `match_type: "exact"` or a term filter. For example, exact MeSH descriptor matching:

```bash
curl -X POST 'http://127.0.0.1:8000/search/advanced' \
  -H 'Content-Type: application/json' \
  -d '{
    "field_queries": [
      {"field": "mesh", "query": "Breast Neoplasms", "match_type": "exact"}
    ],
    "size": 5,
    "source": "summary"
  }'
```

Equivalent filter form:

```bash
curl -X POST 'http://127.0.0.1:8000/search/advanced' \
  -H 'Content-Type: application/json' \
  -d '{
    "filters": [
      {"field": "mesh", "values": ["Breast Neoplasms"]}
    ],
    "size": 5,
    "source": "summary"
  }'
```

The web UI and `/help` page include Chinese/English text switching. The OpenAPI page at `/docs` also uses a bilingual title and API description.

## Index Design

Read alias: `pubmed_articles`

Recommended write indices:

- `pubmed_baseline_v1`
- `pubmed_updates_v1`

Important fields:

- `pmid`, `doi`, `pmcid`, `pii`, `article_ids`: exact identifiers and backend lookup keys.
- `title`, `vernacular_title`, `abstract`, `abstract_sections`, `search_text`: full-text retrieval and display.
- `authors`, `author_names`: author display, affiliation text, and author identifier metadata.
- `journal`: title, ISO abbreviation, ISSN, volume, issue, NLM identifiers, and journal publication date parts.
- `mesh_terms`, `major_mesh_terms`, `mesh_headings`: MeSH descriptor/qualifier search and facet data.
- `chemicals`, `chemical_entries`, `keywords`, `keyword_entries`, `supplemental_mesh_terms`, `gene_symbols`: biomedical facets and ranked retrieval.
- `publication_types`, `publication_type_entries`, `grants`, `data_banks`, `accession_numbers`: filters and detail metadata.
- `publication_date`, `publication_year`, `publication_date_granularity`, `completed_date`, `revised_date`, `pubmed_history`: filtering and sorting.
- `comments_corrections`, `references`: citation relationship metadata when present in the XML.
- `publication_status_normalized`, `publication_status_flags`, `retraction`: derived retraction and expression-of-concern tags plus PMID relationship fields.

Next step for the backend/frontend phase: keep this ES alias stable, then put an API layer in front of it that caches normalized query requests, PMID detail pages, and common facet aggregations.
