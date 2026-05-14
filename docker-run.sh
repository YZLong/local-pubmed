docker run -d \
  --name lyz-pubmed26-es \
  --restart unless-stopped \
  -p 9200:9200 \
  -p 9300:9300 \
  -e discovery.type=single-node \
  -e xpack.security.enabled=false \
  -e ES_JAVA_OPTS="-Xms4g -Xmx4g" \
  -e ingest.geoip.downloader.enabled=false \
  -v /gpfs/flash/data/db/lyz-pubmed26-es:/usr/share/elasticsearch/data \
  --ulimit memlock=-1:-1 \
  --health-cmd='curl -fsS http://localhost:9200/_cluster/health?wait_for_status=yellow\&timeout=1s >/dev/null' \
  --health-interval=10s \
  --health-timeout=5s \
  --health-retries=30 \
  elastic/elasticsearch:8.19.0
