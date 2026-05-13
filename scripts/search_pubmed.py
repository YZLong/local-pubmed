#!/usr/bin/env python3
import argparse
import json
import urllib.parse
import urllib.request


def es_search(es_url, index, query, size):
    body = {
        "size": size,
        "query": {
            "multi_match": {
                "query": query,
                "fields": [
                    "title^5",
                    "abstract^2",
                    "mesh_terms^3",
                    "major_mesh_terms^4",
                    "author_names^2",
                    "journal.title",
                    "keywords^2",
                    "chemicals^2",
                    "supplemental_mesh_terms^2",
                    "gene_symbols^2",
                    "article_ids.value",
                    "search_text"
                ],
                "type": "best_fields",
                "operator": "and"
            }
        },
        "highlight": {
            "require_field_match": False,
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "fields": {
                "title": {},
                "abstract": {"fragment_size": 180, "number_of_fragments": 2},
                "mesh_terms": {},
                "chemicals": {},
                "keywords": {},
                "supplemental_mesh_terms": {},
                "gene_symbols": {}
            }
        },
        "_source": [
            "pmid",
            "doi",
            "title",
            "journal.title",
            "journal.iso_abbreviation",
            "publication_date",
            "publication_year",
            "author_names",
            "mesh_terms",
            "major_mesh_terms",
            "publication_types",
            "chemicals",
            "keywords",
            "abstract"
        ]
    }
    data = json.dumps(body).encode("utf-8")
    url = f"{es_url.rstrip('/')}/{urllib.parse.quote(index)}/_search"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description="Search the local PubMed Elasticsearch index.")
    parser.add_argument("query")
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--index", default="pubmed_articles")
    parser.add_argument("--size", type=int, default=10)
    args = parser.parse_args()

    result = es_search(args.es_url, args.index, args.query, args.size)
    for hit in result["hits"]["hits"]:
        source = hit["_source"]
        authors = ", ".join(source.get("author_names", [])[:3])
        journal = source.get("journal", {})
        print(f"[{hit['_score']:.2f}] PMID {source.get('pmid')} {source.get('publication_year', '')}")
        print(source.get("title", ""))
        if authors:
            print(authors)
        print(f"{journal.get('iso_abbreviation') or journal.get('title') or ''} {source.get('publication_date', '')}")
        highlight = hit.get("highlight", {})
        for field in ["title", "abstract", "mesh_terms", "chemicals", "keywords", "supplemental_mesh_terms", "gene_symbols"]:
            if field in highlight:
                print(f"{field}: " + " ... ".join(highlight[field]))
        print()


if __name__ == "__main__":
    main()
