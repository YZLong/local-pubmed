#!/usr/bin/env python3
"""
初始化或重建查询缓存索引。

用法：
    python scripts/init_query_cache_index.py [--force]

选项：
    --force    强制删除并重建索引（会清空所有缓存数据）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ES_URL = os.getenv("PUBMED_ES_URL", "http://localhost:9200").rstrip("/")
QUERY_CACHE_INDEX = os.getenv("PUBMED_QUERY_CACHE_INDEX", "pubmed_query_cache")


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize query cache index")
    parser.add_argument("--force", action="store_true", help="Force delete and recreate index")
    args = parser.parse_args()

    config_path = Path(__file__).resolve().parent.parent / "config" / "pubmed_query_cache_index.json"
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        index_config = json.load(f)

    with httpx.Client(base_url=ES_URL, timeout=30.0) as client:
        # 检查索引是否存在
        response = client.head(f"/{QUERY_CACHE_INDEX}")
        exists = response.status_code == 200

        if exists:
            if args.force:
                logger.info("Deleting existing index: %s", QUERY_CACHE_INDEX)
                response = client.delete(f"/{QUERY_CACHE_INDEX}")
                response.raise_for_status()
                logger.info("Index deleted")
            else:
                logger.info("Index already exists: %s", QUERY_CACHE_INDEX)
                logger.info("Use --force to delete and recreate")
                return

        # 创建索引
        logger.info("Creating index: %s", QUERY_CACHE_INDEX)
        response = client.put(f"/{QUERY_CACHE_INDEX}", json=index_config)
        response.raise_for_status()
        logger.info("Index created successfully")

        # 验证索引
        response = client.get(f"/{QUERY_CACHE_INDEX}")
        response.raise_for_status()
        logger.info("Index verified: %s", QUERY_CACHE_INDEX)


if __name__ == "__main__":
    main()
