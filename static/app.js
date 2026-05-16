const state = {
  page: 1,
  total: 0,
  facets: {},
  facetFilters: [],
  lastRequest: null,
  lang: localStorage.getItem("pubmed_lang") || "zh",
  pmcCountRequestId: 0,
};

const els = {
  form: document.getElementById("searchForm"),
  query: document.getElementById("queryInput"),
  field: document.getElementById("fieldSelect"),
  sort: document.getElementById("sortSelect"),
  pageSize: document.getElementById("pageSize"),
  exportLimit: document.getElementById("exportLimit"),
  exportJson: document.getElementById("exportJson"),
  exportPmcFulltext: document.getElementById("exportPmcFulltext"),
  pmcFulltextCount: document.getElementById("pmcFulltextCount"),
  author: document.getElementById("authorInput"),
  journal: document.getElementById("journalInput"),
  mesh: document.getElementById("meshInput"),
  chemical: document.getElementById("chemicalInput"),
  keyword: document.getElementById("keywordInput"),
  language: document.getElementById("languageInput"),
  yearFrom: document.getElementById("yearFrom"),
  yearTo: document.getElementById("yearTo"),
  publicationType: document.getElementById("publicationType"),
  hasAbstract: document.getElementById("hasAbstract"),
  resultCount: document.getElementById("resultCount"),
  queryTranslation: document.getElementById("queryTranslation"),
  results: document.getElementById("results"),
  message: document.getElementById("message"),
  facetList: document.getElementById("facetList"),
  activeFilters: document.getElementById("activeFilters"),
  prevPage: document.getElementById("prevPage"),
  nextPage: document.getElementById("nextPage"),
  pageLabel: document.getElementById("pageLabel"),
  languageToggle: document.getElementById("languageToggle"),
  refreshStatus: document.getElementById("refreshStatus"),
  detailDialog: document.getElementById("detailDialog"),
  closeDetail: document.getElementById("closeDetail"),
  detailPmid: document.getElementById("detailPmid"),
  detailTitle: document.getElementById("detailTitle"),
  detailBody: document.getElementById("detailBody"),
  exampleChips: document.querySelectorAll(".example-chip"),
};

const I18N = {
  zh: {
    help: "帮助",
    apiDocs: "API 文档",
    status: "状态",
    fieldAll: "全部字段",
    fieldTitle: "题名",
    fieldAbstract: "摘要",
    fieldAuthor: "作者",
    fieldJournal: "期刊",
    fieldChemical: "化合物",
    fieldGene: "基因",
    fieldId: "标识符",
    search: "检索",
    modeBalanced: "均衡",
    modeStrict: "严格",
    modeBroad: "宽泛",
    modePhrase: "短语",
    sortRelevance: "最佳匹配",
    sortNewest: "最新",
    sortOldest: "最早",
    sortPmidDesc: "PMID 降序",
    sortPmidAsc: "PMID 升序",
    page10: "每页 10 条",
    page20: "每页 20 条",
    page50: "每页 50 条",
    export100: "导出 100 条",
    export1000: "导出 1000 条",
    export5000: "导出 5000 条",
    export10000: "导出 10000 条",
    exportAll: "全量导出",
    exportJson: "导出 JSON.gz",
    exportPmcFulltext: "下载 PMC 全文 JSON.gz",
    exportingPmc: "正在导出 PMC...",
    exporting: "正在导出...",
    exported: "导出已开始",
    pmcChecking: "PMC 全文：统计中...",
    pmcCount: "PMC 全文",
    pmcUnavailable: "PMC 全文：不可用",
    tipsTitle: "检索导航与技巧",
    tipsQuickExamples: "快速示例",
    tipsKeywordTitle: "关键词策略",
    tipsModeTitle: "模式选择",
    exampleFreeText: "自由词检索",
    exampleExactMesh: "精确 MeSH",
    exampleTitle: "题名限定",
    exampleCombined: "组合检索",
    exampleRetraction: "撤稿线索",
    tipKeyword1: "先用 2-4 个核心英文词检索，再用左侧 MeSH、年份、期刊筛选收窄。",
    tipKeyword2: "疾病或标准主题词优先尝试 MeSH，例如 \"Breast Neoplasms\"[MeSH]。",
    tipKeyword3: "基因、药物、化合物可放入高级检索对应字段，避免与普通摘要词混在一起。",
    tipMode1: "均衡适合日常检索；严格适合高精度；宽泛适合扩展召回。",
    tipMode2: "短语模式用于固定表达，如 \"immune checkpoint inhibitor\"。",
    tipMode3: "点击结果标题打开详情，导出按钮会导出当前检索条件下的结果。",
    advanced: "高级检索",
    author: "作者",
    journal: "期刊",
    chemical: "化合物",
    keyword: "关键词",
    language: "语言",
    fromYear: "起始年份",
    toYear: "结束年份",
    publicationType: "出版类型",
    hasAbstract: "有摘要",
    filters: "筛选",
    ready: "就绪",
    prev: "上一页",
    next: "下一页",
    searching: "检索中...",
    noResults: "没有匹配记录。",
    results: "条结果",
    green: "正常",
    offline: "离线",
    bestQuery: "查询",
    facetYear: "年份",
    facetPublicationType: "出版类型",
    facetPublicationStatus: "文献状态",
    facetMajorMesh: "主要 MeSH",
    facetMesh: "MeSH",
    facetChemical: "化合物",
    facetJournal: "期刊",
    facetSource: "数据来源",
    facetLanguage: "语言",
    abstract: "摘要",
    majorMesh: "主要 MeSH",
    gene: "基因",
    untitled: "无题名",
    unableDetail: "无法载入文献详情。",
    exportFailed: "导出失败",
  },
  en: {
    help: "Help",
    apiDocs: "API Docs",
    status: "Status",
    fieldAll: "All fields",
    fieldTitle: "Title",
    fieldAbstract: "Abstract",
    fieldAuthor: "Author",
    fieldJournal: "Journal",
    fieldChemical: "Chemical",
    fieldGene: "Gene",
    fieldId: "Identifier",
    search: "Search",
    modeBalanced: "Balanced",
    modeStrict: "Strict",
    modeBroad: "Broad",
    modePhrase: "Phrase",
    sortRelevance: "Best match",
    sortNewest: "Newest",
    sortOldest: "Oldest",
    sortPmidDesc: "PMID desc",
    sortPmidAsc: "PMID asc",
    page10: "10 per page",
    page20: "20 per page",
    page50: "50 per page",
    export100: "Export 100",
    export1000: "Export 1000",
    export5000: "Export 5000",
    export10000: "Export 10000",
    exportAll: "Export all",
    exportJson: "Export JSON.gz",
    exportPmcFulltext: "Download PMC JSON.gz",
    exportingPmc: "Exporting PMC...",
    exporting: "Exporting...",
    exported: "Export started",
    pmcChecking: "PMC full text: checking...",
    pmcCount: "PMC full text",
    pmcUnavailable: "PMC full text: unavailable",
    tipsTitle: "Search Navigation & Tips",
    tipsQuickExamples: "Quick examples",
    tipsKeywordTitle: "Keyword strategy",
    tipsModeTitle: "Mode guide",
    exampleFreeText: "Free text",
    exampleExactMesh: "Exact MeSH",
    exampleTitle: "Title field",
    exampleCombined: "Combined query",
    exampleRetraction: "Retraction clues",
    tipKeyword1: "Start with 2-4 core English terms, then narrow with MeSH, year, and journal facets on the left.",
    tipKeyword2: "For diseases or controlled concepts, try MeSH first, for example \"Breast Neoplasms\"[MeSH].",
    tipKeyword3: "Put genes, drugs, and chemicals into their advanced fields when possible to keep them separate from abstract text.",
    tipMode1: "Balanced is the daily default; Strict favors precision; Broad expands recall.",
    tipMode2: "Phrase mode is useful for fixed expressions such as \"immune checkpoint inhibitor\".",
    tipMode3: "Open details by clicking a result title. Export uses the current search request.",
    advanced: "Advanced",
    author: "Author",
    journal: "Journal",
    chemical: "Chemical",
    keyword: "Keyword",
    language: "Language",
    fromYear: "From year",
    toYear: "To year",
    publicationType: "Publication type",
    hasAbstract: "Has abstract",
    filters: "Filters",
    ready: "Ready",
    prev: "Prev",
    next: "Next",
    searching: "Searching...",
    noResults: "No matching records.",
    results: "results",
    green: "Green",
    offline: "Offline",
    bestQuery: "Query",
    facetYear: "Year",
    facetPublicationType: "Publication type",
    facetPublicationStatus: "Publication status",
    facetMajorMesh: "Major MeSH",
    facetMesh: "MeSH",
    facetChemical: "Chemical",
    facetJournal: "Journal",
    facetSource: "Source",
    facetLanguage: "Language",
    abstract: "Abstract",
    majorMesh: "Major MeSH",
    gene: "Gene",
    untitled: "Untitled",
    unableDetail: "Unable to load article.",
    exportFailed: "Export failed",
  },
};

const TAG_FIELD_MAP = {
  title: "title",
  ti: "title",
  abstract: "abstract",
  tiab: "abstract",
  author: "author",
  au: "author",
  journal: "journal",
  ta: "journal",
  mesh: "mesh",
  mh: "mesh",
  chemical: "chemical",
  nm: "chemical",
  keyword: "keyword",
  gene: "gene",
  pmid: "id",
  doi: "id",
  id: "id",
};

const EXACT_TAG_FIELDS = new Set([
  "mesh",
  "major_mesh",
  "chemical",
  "keyword",
  "gene",
  "journal",
]);

const FILTER_FROM_FACET = {
  languages: "language",
  publication_types: "publication_type",
  major_mesh: "major_mesh",
  mesh: "mesh",
  chemicals: "chemical",
  journals: "journal_iso",
  publication_status: "publication_status",
  publication_status_normalized: "publication_status_normalized",
  source_dataset: "source_dataset",
};

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  state.page = 1;
  runSearch();
});

els.prevPage.addEventListener("click", () => {
  if (state.page <= 1) return;
  state.page -= 1;
  runSearch();
});

els.nextPage.addEventListener("click", () => {
  const size = Number(els.pageSize.value);
  if (state.page * size >= state.total) return;
  state.page += 1;
  runSearch();
});

els.refreshStatus.addEventListener("click", refreshStatus);
els.languageToggle.addEventListener("click", toggleLanguage);
els.exportJson.addEventListener("click", exportCurrentSearch);
els.exportPmcFulltext.addEventListener("click", exportCurrentPmcFulltext);
els.closeDetail.addEventListener("click", () => els.detailDialog.close());
for (const chip of els.exampleChips) {
  chip.addEventListener("click", () => {
    els.query.value = chip.dataset.query || "";
    state.page = 1;
    runSearch();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  applyTranslations();
  els.query.value = "esophageal cancer GNAS";
  runSearch();
  refreshStatus();
});

function t(key) {
  return I18N[state.lang][key] || I18N.en[key] || key;
}

function applyTranslations() {
  document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  els.languageToggle.textContent = state.lang === "zh" ? "EN" : "中";
  if (!state.total) {
    els.resultCount.textContent = t("ready");
  }
}

function toggleLanguage() {
  state.lang = state.lang === "zh" ? "en" : "zh";
  localStorage.setItem("pubmed_lang", state.lang);
  applyTranslations();
  renderFacets(state.facets || {});
  renderActiveFilters();
  if (state.lastRequest) renderTranslation(state.lastRequest);
}

async function refreshStatus() {
  try {
    const health = await requestJSON("/health");
    els.refreshStatus.textContent = health.status === "green" ? t("green") : health.status || t("status");
  } catch {
    els.refreshStatus.textContent = t("offline");
  }
}

async function runSearch() {
  clearMessage();
  setLoading(true);
  const request = buildRequest();
  state.lastRequest = request;
  renderTranslation(request);
  try {
    const result = await requestJSON("/search/advanced", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(request),
    });
    state.total = result.total || 0;
    state.facets = result.facets || {};
    renderResults(result);
    renderFacets(result.facets || {});
    renderActiveFilters();
    refreshPmcFulltextCount(request);
  } catch (error) {
    showMessage(error.message || "Search failed");
    renderPmcFulltextCount(null);
  } finally {
    setLoading(false);
  }
}

async function exportCurrentSearch() {
  clearMessage();
  const request = state.lastRequest || buildRequest();
  const exportLimit = els.exportLimit.value;
  const allRecords = exportLimit === "all";
  const maxRecords = allRecords ? 10000 : Number(exportLimit || 1000);
  const previousText = els.exportJson.textContent;
  els.exportJson.disabled = true;
  els.exportJson.textContent = t("exporting");
  try {
    const params = new URLSearchParams({
      max_records: String(maxRecords),
      source: "full",
      include_highlight: "false",
      all_records: allRecords ? "true" : "false",
    });
    const response = await fetch(`/export/search?${params.toString()}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...request, from: 0, size: 100, source: "full", facets: false, highlight: false}),
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="([^"]+)"/)?.[1] || `pubmed_export_${Date.now()}.json.gz`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showMessage(`${t("exported")}: ${response.headers.get("X-Exported-Records") || ""}`);
  } catch (error) {
    showMessage(`${t("exportFailed")}: ${error.message || error}`);
  } finally {
    els.exportJson.disabled = false;
    els.exportJson.textContent = previousText;
  }
}

async function refreshPmcFulltextCount(request) {
  const requestId = ++state.pmcCountRequestId;
  renderPmcFulltextCount(null, true);
  try {
    const result = await requestJSON("/pmc/fulltext/count", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...request, from: 0, size: 100, source: "ids", facets: false, highlight: false}),
    });
    if (requestId !== state.pmcCountRequestId) return;
    renderPmcFulltextCount(result.pmc_fulltext_count || 0);
  } catch {
    if (requestId !== state.pmcCountRequestId) return;
    renderPmcFulltextCount(null);
  }
}

function renderPmcFulltextCount(count, checking = false) {
  if (checking) {
    els.pmcFulltextCount.textContent = t("pmcChecking");
    els.exportPmcFulltext.disabled = true;
    return;
  }
  if (count === null || count === undefined) {
    els.pmcFulltextCount.textContent = t("pmcUnavailable");
    els.exportPmcFulltext.disabled = true;
    return;
  }
  els.pmcFulltextCount.textContent = `${t("pmcCount")}: ${Number(count).toLocaleString()}`;
  els.exportPmcFulltext.disabled = Number(count) <= 0;
}

async function exportCurrentPmcFulltext() {
  clearMessage();
  const request = state.lastRequest || buildRequest();
  const previousText = els.exportPmcFulltext.textContent;
  els.exportPmcFulltext.disabled = true;
  els.exportPmcFulltext.textContent = t("exportingPmc");
  try {
    const response = await fetch("/export/pmc-fulltext?source=full", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...request, from: 0, size: 100, source: "ids", facets: false, highlight: false}),
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="([^"]+)"/)?.[1] || `pmc_fulltext_export_${Date.now()}.json.gz`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showMessage(`${t("exported")}: ${response.headers.get("X-Exported-Records") || ""}`);
  } catch (error) {
    showMessage(`${t("exportFailed")}: ${error.message || error}`);
  } finally {
    els.exportPmcFulltext.textContent = previousText;
    refreshPmcFulltextCount(request);
  }
}

function buildRequest() {
  const tagged = parsePubMedTags(els.query.value.trim());
  const fieldQueries = [...tagged.fieldQueries];
  const filters = [...state.facetFilters];

  appendField(fieldQueries, "author", els.author.value);
  appendField(fieldQueries, "journal", els.journal.value);
  appendField(fieldQueries, "mesh", els.mesh.value);
  appendField(fieldQueries, "chemical", els.chemical.value);
  appendField(fieldQueries, "keyword", els.keyword.value);

  if (els.language.value.trim()) {
    filters.push({field: "language", values: [els.language.value.trim()]});
  }
  if (els.publicationType.value.trim()) {
    filters.push({field: "publication_type", values: [els.publicationType.value.trim()]});
  }

  const yearFrom = tagged.yearFrom ?? numberOrNull(els.yearFrom.value);
  const yearTo = tagged.yearTo ?? numberOrNull(els.yearTo.value);
  const size = Number(els.pageSize.value);

  return {
    query: tagged.query || null,
    query_fields: [tagged.queryField || els.field.value],
    mode: selectedMode(),
    operator: "and",
    field_queries: fieldQueries,
    filters,
    year_from: yearFrom,
    year_to: yearTo,
    has_abstract: els.hasAbstract.checked ? true : null,
    from: (state.page - 1) * size,
    size,
    sort: els.sort.value,
    source: "summary",
    highlight: true,
    facets: true,
  };
}

function appendField(fieldQueries, field, value) {
  const query = value.trim();
  if (query) {
    fieldQueries.push({field, query, occur: "must", operator: "and", match_type: "match"});
  }
}

function parsePubMedTags(raw) {
  const fieldQueries = [];
  let query = raw;
  let queryField = null;
  let yearFrom = null;
  let yearTo = null;
  const tagPattern = /"([^"]+)"\s*\[([^\]]+)\]|([^\[\]()]+?)\s*\[([^\]]+)\]/g;
  const removals = [];
  let match;

  while ((match = tagPattern.exec(raw)) !== null) {
    const value = (match[1] || match[3] || "").trim();
    const tag = (match[2] || match[4] || "").trim().toLowerCase();
    const quoted = Boolean(match[1]);
    if (!value || !tag) continue;
    removals.push(match[0]);
    if (tag === "year" || tag === "dp" || tag === "date") {
      const years = parseYearRange(value);
      yearFrom = years.from;
      yearTo = years.to;
      continue;
    }
    const field = TAG_FIELD_MAP[tag];
    if (!field) continue;
    if (quoted && EXACT_TAG_FIELDS.has(field)) {
      fieldQueries.push({field, query: value, occur: "must", operator: "and", match_type: "exact"});
      continue;
    }
    if (!queryField && raw.trim() === match[0].trim()) {
      queryField = field;
      query = value;
    } else {
      fieldQueries.push({field, query: value, occur: "must", operator: "and", match_type: tag === "doi" || tag === "pmid" ? "exact" : "match"});
    }
  }

  for (const removal of removals) {
    query = query.replace(removal, " ");
  }
  query = query.replace(/\s+/g, " ").trim();
  return {query, queryField, fieldQueries, yearFrom, yearTo};
}

function parseYearRange(value) {
  const parts = value.split(":").map((part) => part.trim()).filter(Boolean);
  if (parts.length === 2) return {from: numberOrNull(parts[0]), to: numberOrNull(parts[1])};
  const year = numberOrNull(parts[0] || value);
  return {from: year, to: year};
}

function selectedMode() {
  return document.querySelector("input[name='mode']:checked")?.value || "balanced";
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

async function requestJSON(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function renderResults(result) {
  els.results.innerHTML = "";
  const total = result.total || 0;
  const size = Number(els.pageSize.value);
  els.resultCount.textContent = total ? `${total.toLocaleString()} ${t("results")}` : t("noResults");
  els.pageLabel.textContent = String(state.page);
  els.prevPage.disabled = state.page <= 1;
  els.nextPage.disabled = state.page * size >= total;

  if (!result.items?.length) {
    showMessage(t("noResults"));
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const item of result.items) {
    fragment.appendChild(renderResultItem(item));
  }
  els.results.appendChild(fragment);
}

function renderResultItem(item) {
  const li = document.createElement("li");
  li.className = "result-item";

  const title = document.createElement("h2");
  title.className = "result-title";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "title-button";
  button.innerHTML = firstHighlight(item, "title") || escapeHTML(item.title || t("untitled"));
  button.addEventListener("click", () => showDetail(item.pmid));
  title.appendChild(button);

  const meta = document.createElement("div");
  meta.className = "meta";
  const journal = item.journal?.iso_abbreviation || item.journal?.title || "";
  const date = item.publication_date || item.publication_year || "";
  const authors = compactList(item.authors || [], 4);
  meta.textContent = [authors, journal, date].filter(Boolean).join(" · ");

  const ids = document.createElement("div");
  ids.className = "ids";
  ids.textContent = [item.pmid ? `PMID ${item.pmid}` : "", item.pmcid || "", item.doi || ""].filter(Boolean).join(" · ");

  const snippets = document.createElement("div");
  snippets.className = "snippets";
  for (const [field, values] of Object.entries(item.highlight || {})) {
    if (!values?.length || field === "title") continue;
    const row = document.createElement("div");
    row.innerHTML = `<strong>${escapeHTML(labelFor(field))}:</strong> ${sanitizeHighlight(values.slice(0, 3).join(" ... "))}`;
    snippets.appendChild(row);
  }

  const terms = document.createElement("div");
  terms.className = "term-row";
  const status = statusLabel(item.publication_status_normalized);
  if (status) {
    const badge = document.createElement("span");
    badge.className = `status-badge status-${item.publication_status_normalized}`;
    badge.textContent = status;
    terms.appendChild(badge);
  }
  for (const term of [...(item.major_mesh_terms || []), ...(item.chemicals || [])].slice(0, 8)) {
    const pill = document.createElement("span");
    pill.className = "term";
    pill.textContent = term;
    terms.appendChild(pill);
  }

  li.append(title, meta, ids, snippets, terms);
  return li;
}

function firstHighlight(item, field) {
  const values = item.highlight?.[field];
  return values?.length ? sanitizeHighlight(values.join(" ... ")) : "";
}

function renderFacets(facets) {
  els.facetList.innerHTML = "";
  const names = [
    ["years", t("facetYear")],
    ["publication_status_normalized", t("facetPublicationStatus")],
    ["publication_types", t("facetPublicationType")],
    ["major_mesh", t("facetMajorMesh")],
    ["mesh", t("facetMesh")],
    ["chemicals", t("facetChemical")],
    ["journals", t("facetJournal")],
    ["source_dataset", t("facetSource")],
    ["languages", t("facetLanguage")],
  ];
  for (const [key, title] of names) {
    const buckets = facets[key] || [];
    if (!buckets.length) continue;
    const group = document.createElement("section");
    group.className = "facet-group";
    const h = document.createElement("h3");
    h.textContent = title;
    group.appendChild(h);
    for (const bucket of buckets.slice(0, 8)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "facet-button";
      button.innerHTML = `<span>${escapeHTML(String(bucket.key))}</span><span>${bucket.count}</span>`;
      button.addEventListener("click", () => addFacetFilter(key, String(bucket.key)));
      group.appendChild(button);
    }
    els.facetList.appendChild(group);
  }
}

function addFacetFilter(facetName, value) {
  const field = FILTER_FROM_FACET[facetName];
  if (!field) return;
  if (!state.facetFilters.some((filter) => filter.field === field && filter.values[0] === value)) {
    state.facetFilters.push({field, values: [value]});
  }
  state.page = 1;
  runSearch();
}

function renderActiveFilters() {
  els.activeFilters.innerHTML = "";
  for (const filter of state.facetFilters) {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "pill";
    pill.textContent = `${filter.field}: ${filter.values[0]} ×`;
    pill.addEventListener("click", () => {
      state.facetFilters = state.facetFilters.filter((item) => item !== filter);
      state.page = 1;
      runSearch();
    });
    els.activeFilters.appendChild(pill);
  }
}

async function showDetail(pmid) {
  if (!pmid) return;
  try {
    const item = await requestJSON(`/articles/${encodeURIComponent(pmid)}`);
    els.detailPmid.textContent = `PMID ${item.pmid || ""}`;
    els.detailTitle.textContent = item.title || "Untitled";
    els.detailBody.innerHTML = renderDetailHTML(item);
    els.detailDialog.showModal();
  } catch (error) {
    showMessage(error.message || t("unableDetail"));
  }
}

function renderDetailHTML(item) {
  const authors = compactList((item.authors || []).map((author) => author.full_name).filter(Boolean), 20);
  const journal = item.journal?.title || item.journal?.iso_abbreviation || "";
  const sections = (item.abstract_sections || [])
    .map((section) => `<div class="detail-section"><h3>${escapeHTML(section.label || section.nlm_category || "Abstract")}</h3><p>${escapeHTML(section.text || "")}</p></div>`)
    .join("");
  const mesh = (item.mesh_terms || []).slice(0, 40).map(escapeHTML).join("; ");
  const chemicals = (item.chemicals || []).map(escapeHTML).join("; ");
  const ids = (item.article_ids || []).map((entry) => `${escapeHTML(entry.type)}: ${escapeHTML(entry.value)}`).join("<br>");
  const status = statusLabel(item.publication_status_normalized);
  const retractionLinks = renderRetractionLinks(item.retraction || {});

  return `
    <dl class="kv">
      <dt>${escapeHTML(t("facetPublicationStatus"))}</dt><dd>${escapeHTML(status || item.publication_status_normalized || "")}</dd>
      <dt>Retraction</dt><dd>${retractionLinks}</dd>
      <dt>${escapeHTML(t("author"))}</dt><dd>${escapeHTML(authors)}</dd>
      <dt>${escapeHTML(t("journal"))}</dt><dd>${escapeHTML(journal)}</dd>
      <dt>Date</dt><dd>${escapeHTML(item.publication_date || "")}</dd>
      <dt>DOI</dt><dd>${escapeHTML(item.doi || "")}</dd>
      <dt>PMCID</dt><dd>${escapeHTML(item.pmcid || "")}</dd>
      <dt>Article IDs</dt><dd>${ids}</dd>
    </dl>
    ${sections || `<div class="detail-section"><h3>${escapeHTML(t("abstract"))}</h3><p>${escapeHTML(item.abstract || "")}</p></div>`}
    <div class="detail-section"><h3>MeSH</h3><p>${mesh}</p></div>
    <div class="detail-section"><h3>Chemicals</h3><p>${chemicals}</p></div>
  `;
}

function statusLabel(status) {
  const labels = {
    zh: {
      retracted: "已撤稿",
      retraction_notice: "撤稿公告",
      expression_of_concern: "表达关注",
    },
    en: {
      retracted: "Retracted",
      retraction_notice: "Retraction notice",
      expression_of_concern: "Expression of concern",
    },
  };
  return labels[state.lang][status] || "";
}

function renderRetractionLinks(retraction) {
  const lines = [];
  if (retraction.retracted_by_pmids?.length) {
    lines.push(`Retracted by PMID: ${retraction.retracted_by_pmids.map(escapeHTML).join(", ")}`);
  }
  if (retraction.retracts_pmids?.length) {
    lines.push(`Retracts PMID: ${retraction.retracts_pmids.map(escapeHTML).join(", ")}`);
  }
  if (retraction.expression_of_concern_by_pmids?.length) {
    lines.push(`Concern by PMID: ${retraction.expression_of_concern_by_pmids.map(escapeHTML).join(", ")}`);
  }
  if (retraction.expression_of_concern_for_pmids?.length) {
    lines.push(`Concern for PMID: ${retraction.expression_of_concern_for_pmids.map(escapeHTML).join(", ")}`);
  }
  return lines.length ? lines.join("<br>") : "";
}

function renderTranslation(request) {
  const parts = [];
  if (request.query) parts.push(`${t("bestQuery")} ${request.query_fields.join(",")}: ${request.query}`);
  for (const fieldQuery of request.field_queries) {
    parts.push(`${fieldQuery.field}: ${fieldQuery.query}`);
  }
  for (const filter of request.filters) {
    parts.push(`${filter.field}: ${filter.values.join(", ")}`);
  }
  if (request.year_from || request.year_to) {
    parts.push(`${request.year_from || ""}-${request.year_to || ""}`);
  }
  els.queryTranslation.textContent = parts.join(" · ");
}

function setLoading(isLoading) {
  document.body.classList.toggle("is-loading", isLoading);
  els.resultCount.textContent = isLoading ? t("searching") : els.resultCount.textContent;
}

function showMessage(text) {
  els.message.hidden = false;
  els.message.textContent = text;
}

function clearMessage() {
  els.message.hidden = true;
  els.message.textContent = "";
}

function compactList(values, limit) {
  if (!values.length) return "";
  if (values.length <= limit) return values.join(", ");
  return `${values.slice(0, limit).join(", ")} et al.`;
}

function labelFor(field) {
  return {
    abstract: t("abstract"),
    mesh_terms: "MeSH",
    major_mesh_terms: t("majorMesh"),
    chemicals: t("chemical"),
    keywords: t("keyword"),
    gene_symbols: t("gene"),
  }[field] || field;
}

function sanitizeHighlight(value) {
  return escapeHTML(value)
    .replaceAll("&lt;mark&gt;", "<mark>")
    .replaceAll("&lt;/mark&gt;", "</mark>");
}

function escapeHTML(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
