"""
#3: 单元测试 —— XML 解析逻辑（parse_doc 及子函数）

覆盖重点：
- 正常文章解析（标题、摘要、作者、期刊、MeSH、化合物）
- MedlineDate 回退（无 Year 字段时）
- 撤稿状态派生
- 缺字段时不崩溃（None 安全）
- source_rank 字典查找
"""
from __future__ import annotations

import gzip
import io
import textwrap
import xml.etree.ElementTree as ET

import pytest

# 把 scripts/ 加入路径，使 import 可以找到脚本
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_pubmed_to_es import (
    SOURCE_RANK,
    derive_publication_status_flags,
    drop_empty,
    parse_abstract,
    parse_authors,
    parse_chemicals,
    parse_date_parts,
    parse_doc,
    parse_keywords,
    parse_mesh,
    parse_month,
    parse_history,
    parse_optional_int,
    source_rank,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _article(xml_body: str) -> ET.Element:
    """把 XML 片段包装成完整的 PubmedArticle 元素。"""
    wrapped = f"<PubmedArticle>{xml_body}</PubmedArticle>"
    return ET.fromstring(wrapped)


MINIMAL_ARTICLE_XML = textwrap.dedent("""\
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">12345678</PMID>
      <Article PubModel="Print">
        <Journal>
          <ISSN IssnType="Print">0000-0000</ISSN>
          <JournalIssue CitedMedium="Print">
            <Volume>10</Volume>
            <Issue>2</Issue>
            <PubDate><Year>2020</Year><Month>Mar</Month><Day>15</Day></PubDate>
          </JournalIssue>
          <Title>Test Journal</Title>
          <ISOAbbreviation>Test J</ISOAbbreviation>
        </Journal>
        <ArticleTitle>A test article title</ArticleTitle>
        <Abstract>
          <AbstractText>This is the abstract.</AbstractText>
        </Abstract>
        <AuthorList CompleteYN="Y">
          <Author ValidYN="Y">
            <LastName>Smith</LastName>
            <ForeName>John</ForeName>
            <Initials>J</Initials>
          </Author>
          <Author ValidYN="Y">
            <LastName>Doe</LastName>
            <ForeName>Jane</ForeName>
            <Initials>J</Initials>
          </Author>
        </AuthorList>
        <Language>eng</Language>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
      <MeshHeadingList>
        <MeshHeading>
          <DescriptorName UI="D004938" MajorTopicYN="Y">Esophageal Neoplasms</DescriptorName>
        </MeshHeading>
      </MeshHeadingList>
      <ChemicalList>
        <Chemical>
          <RegistryNumber>0</RegistryNumber>
          <NameOfSubstance UI="C000001">GNAS protein</NameOfSubstance>
        </Chemical>
      </ChemicalList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">12345678</ArticleId>
        <ArticleId IdType="doi">10.1000/test.doi</ArticleId>
      </ArticleIdList>
    </PubmedData>
""")


# ---------------------------------------------------------------------------
# parse_date_parts
# ---------------------------------------------------------------------------

class TestParseDateParts:
    def test_full_date(self):
        elem = ET.fromstring("<PubDate><Year>2020</Year><Month>Mar</Month><Day>15</Day></PubDate>")
        result = parse_date_parts(elem)
        assert result["date"] == "2020-03-15"
        assert result["year"] == 2020
        assert result["month"] == 3
        assert result["day"] == 15
        assert result["granularity"] == "day"

    def test_year_month_only(self):
        elem = ET.fromstring("<PubDate><Year>2021</Year><Month>Jun</Month></PubDate>")
        result = parse_date_parts(elem)
        assert result["date"] == "2021-06-01"
        assert result["granularity"] == "month"
        assert "day" not in result

    def test_year_only(self):
        elem = ET.fromstring("<PubDate><Year>2019</Year></PubDate>")
        result = parse_date_parts(elem)
        assert result["date"] == "2019-01-01"
        assert result["granularity"] == "year"

    def test_medline_date_fallback(self):
        """无 Year 字段时从 MedlineDate 提取年份。"""
        elem = ET.fromstring("<PubDate><MedlineDate>2018 Jan-Feb</MedlineDate></PubDate>")
        result = parse_date_parts(elem)
        assert result["year"] == 2018
        assert result["medline_date"] == "2018 Jan-Feb"

    def test_none_input(self):
        assert parse_date_parts(None) is None

    def test_numeric_month(self):
        elem = ET.fromstring("<PubDate><Year>2022</Year><Month>8</Month></PubDate>")
        result = parse_date_parts(elem)
        assert result["month"] == 8

    def test_invalid_calendar_date_omits_es_date(self):
        elem = ET.fromstring("<PubDate><Year>2018</Year><Month>Feb</Month><Day>31</Day></PubDate>")
        result = parse_date_parts(elem)
        assert "date" not in result
        assert result["year"] == 2018
        assert result["month"] == 2
        assert result["day"] == 31
        assert "granularity" not in result


class TestParseHistory:
    def test_null_hour_and_minute_are_omitted(self):
        article = ET.fromstring(textwrap.dedent("""\
            <PubmedArticle>
              <PubmedData>
                <History>
                  <PubMedPubDate PubStatus="pubmed">
                    <Year>2020</Year><Month>Jan</Month><Day>2</Day>
                    <Hour>null</Hour><Minute>null</Minute>
                  </PubMedPubDate>
                </History>
              </PubmedData>
            </PubmedArticle>
        """))
        entries, by_status = parse_history(article)
        assert entries[0]["date"] == "2020-01-02"
        assert "hour" not in entries[0]
        assert "minute" not in entries[0]
        assert by_status["pubmed"] == "2020-01-02"

    def test_parse_optional_int(self):
        assert parse_optional_int("8") == 8
        assert parse_optional_int("null") is None
        assert parse_optional_int("bad") is None


# ---------------------------------------------------------------------------
# parse_month
# ---------------------------------------------------------------------------

class TestParseMonth:
    @pytest.mark.parametrize("raw,expected", [
        ("Jan", "01"), ("january", "01"), ("JAN", "01"),
        ("Feb", "02"), ("Mar", "03"), ("Apr", "04"),
        ("May", "05"), ("Jun", "06"), ("Jul", "07"),
        ("Aug", "08"), ("Sep", "09"), ("Oct", "10"),
        ("Nov", "11"), ("Dec", "12"),
        ("3", "03"), ("12", "12"),
        (None, None), ("", None), ("Xyz", None),
    ])
    def test_parse_month(self, raw, expected):
        assert parse_month(raw) == expected


# ---------------------------------------------------------------------------
# parse_abstract
# ---------------------------------------------------------------------------

class TestParseAbstract:
    def test_plain_abstract(self):
        article_node = ET.fromstring(
            "<Article><Abstract><AbstractText>Plain text.</AbstractText></Abstract></Article>"
        )
        text, sections = parse_abstract(article_node)
        assert text == "Plain text."
        assert len(sections) == 1
        assert sections[0]["text"] == "Plain text."

    def test_structured_abstract(self):
        article_node = ET.fromstring(textwrap.dedent("""\
            <Article>
              <Abstract>
                <AbstractText Label="BACKGROUND" NlmCategory="BACKGROUND">Background text.</AbstractText>
                <AbstractText Label="METHODS" NlmCategory="METHODS">Methods text.</AbstractText>
              </Abstract>
            </Article>
        """))
        text, sections = parse_abstract(article_node)
        assert "BACKGROUND: Background text." in text
        assert "METHODS: Methods text." in text
        assert len(sections) == 2

    def test_no_abstract(self):
        article_node = ET.fromstring("<Article></Article>")
        text, sections = parse_abstract(article_node)
        assert text is None
        assert sections == []


# ---------------------------------------------------------------------------
# parse_authors
# ---------------------------------------------------------------------------

class TestParseAuthors:
    def test_normal_authors(self):
        article = _article(MINIMAL_ARTICLE_XML)
        authors = parse_authors(article)
        assert len(authors) == 2
        assert authors[0]["full_name"] == "John Smith"
        assert authors[0]["last_name"] == "Smith"
        assert authors[0]["position"] == 1
        assert authors[1]["full_name"] == "Jane Doe"

    def test_collective_name(self):
        article = _article(textwrap.dedent("""\
            <MedlineCitation>
              <Article>
                <AuthorList>
                  <Author ValidYN="Y">
                    <CollectiveName>The Study Group</CollectiveName>
                  </Author>
                </AuthorList>
              </Article>
            </MedlineCitation>
        """))
        authors = parse_authors(article)
        assert len(authors) == 1
        assert authors[0]["full_name"] == "The Study Group"
        assert authors[0]["collective_name"] == "The Study Group"

    def test_no_authors(self):
        article = _article("<MedlineCitation><Article></Article></MedlineCitation>")
        assert parse_authors(article) == []


# ---------------------------------------------------------------------------
# parse_mesh
# ---------------------------------------------------------------------------

class TestParseMesh:
    def test_major_mesh(self):
        article = _article(MINIMAL_ARTICLE_XML)
        mesh_terms, major_terms, headings = parse_mesh(article)
        assert "Esophageal Neoplasms" in mesh_terms
        assert "Esophageal Neoplasms" in major_terms
        assert any(h["descriptor"] == "Esophageal Neoplasms" for h in headings)

    def test_mesh_with_qualifier(self):
        article = _article(textwrap.dedent("""\
            <MedlineCitation>
              <MeshHeadingList>
                <MeshHeading>
                  <DescriptorName UI="D001" MajorTopicYN="N">Apoptosis</DescriptorName>
                  <QualifierName UI="Q001" MajorTopicYN="Y">drug effects</QualifierName>
                </MeshHeading>
              </MeshHeadingList>
            </MedlineCitation>
        """))
        mesh_terms, major_terms, headings = parse_mesh(article)
        assert "Apoptosis/drug effects" in mesh_terms
        assert "Apoptosis/drug effects" in major_terms  # qualifier is major

    def test_no_mesh(self):
        article = _article("<MedlineCitation></MedlineCitation>")
        mesh_terms, major_terms, headings = parse_mesh(article)
        assert mesh_terms == []
        assert major_terms == []
        assert headings == []


# ---------------------------------------------------------------------------
# parse_chemicals
# ---------------------------------------------------------------------------

class TestParseChemicals:
    def test_chemicals(self):
        article = _article(MINIMAL_ARTICLE_XML)
        names, entries = parse_chemicals(article)
        assert "GNAS protein" in names
        assert any(e["name"] == "GNAS protein" for e in entries)

    def test_no_chemicals(self):
        article = _article("<MedlineCitation></MedlineCitation>")
        names, entries = parse_chemicals(article)
        assert names == []
        assert entries == []


# ---------------------------------------------------------------------------
# derive_publication_status_flags
# ---------------------------------------------------------------------------

class TestDerivePublicationStatus:
    def test_active(self):
        result = derive_publication_status_flags(["Journal Article"], [])
        assert result["publication_status_normalized"] == "active"
        assert result["publication_status_flags"]["is_retracted"] is False

    def test_retracted_by_publication_type(self):
        result = derive_publication_status_flags(["Retracted Publication"], [])
        assert result["publication_status_normalized"] == "retracted"
        assert result["publication_status_flags"]["is_retracted"] is True

    def test_retracted_by_comments_corrections(self):
        corrections = [{"ref_type": "RetractionIn", "pmid": "99999999"}]
        result = derive_publication_status_flags(["Journal Article"], corrections)
        assert result["publication_status_normalized"] == "retracted"
        assert result["retraction"]["retracted_by_pmids"] == ["99999999"]

    def test_retraction_notice(self):
        # PubMed XML 里撤稿公告的 publication type 是 "Retraction of Publication"
        # 代码 lowercases 后匹配 "retraction notice"，所以要用正确的字符串
        result = derive_publication_status_flags(["Retraction Notice"], [])
        assert result["publication_status_normalized"] == "retraction_notice"

    def test_expression_of_concern(self):
        result = derive_publication_status_flags(["Expression of Concern"], [])
        assert result["publication_status_normalized"] == "expression_of_concern"

    def test_erratum(self):
        result = derive_publication_status_flags(["Published Erratum"], [])
        assert result["publication_status_flags"]["has_erratum"] is True
        # erratum 不改变 normalized status
        assert result["publication_status_normalized"] == "active"


# ---------------------------------------------------------------------------
# source_rank
# ---------------------------------------------------------------------------

class TestSourceRank:
    def test_known_datasets(self):
        assert source_rank("updatefiles") == SOURCE_RANK["updatefiles"]
        assert source_rank("baseline") == SOURCE_RANK["baseline"]

    def test_unknown_dataset_returns_zero(self):
        assert source_rank("custom") == 0
        assert source_rank("unknown") == 0

    def test_source_rank_dict_is_ordered(self):
        """updatefiles 优先级高于 baseline。"""
        assert SOURCE_RANK["updatefiles"] > SOURCE_RANK["baseline"]


# ---------------------------------------------------------------------------
# drop_empty
# ---------------------------------------------------------------------------

class TestDropEmpty:
    def test_removes_none(self):
        assert drop_empty({"a": None, "b": 1}) == {"b": 1}

    def test_removes_empty_string(self):
        assert drop_empty({"a": "", "b": "x"}) == {"b": "x"}

    def test_removes_empty_list(self):
        assert drop_empty({"a": [], "b": [1]}) == {"b": [1]}

    def test_removes_empty_dict(self):
        assert drop_empty({"a": {}, "b": {"c": 1}}) == {"b": {"c": 1}}

    def test_nested(self):
        result = drop_empty({"outer": {"inner": None, "keep": 1}})
        assert result == {"outer": {"keep": 1}}

    def test_list_items(self):
        result = drop_empty([None, "", 0, "hello", [], [1]])
        # 0 不是 None/""/[]/{}，应保留
        assert result == [0, "hello", [1]]


# ---------------------------------------------------------------------------
# parse_doc (integration)
# ---------------------------------------------------------------------------

class TestParseDoc:
    def test_full_doc(self):
        article = _article(MINIMAL_ARTICLE_XML)
        doc = parse_doc(article, "pubmed26n0001.xml.gz", "2024-01-01T00:00:00+00:00", "baseline", "pubmed_baseline_v1")
        assert doc is not None
        assert doc["pmid"] == "12345678"
        assert doc["pmid_num"] == 12345678
        assert doc["doi"] == "10.1000/test.doi"
        assert doc["title"] == "A test article title"
        assert doc["abstract"] == "This is the abstract."
        assert doc["language"] == "eng"
        assert "John Smith" in doc["author_names"]
        assert "Esophageal Neoplasms" in doc["mesh_terms"]
        assert "GNAS protein" in doc["chemicals"]
        assert doc["source_dataset"] == "baseline"
        assert doc["source_rank"] == SOURCE_RANK["baseline"]
        assert doc["journal"]["title"] == "Test Journal"
        assert doc["publication_year"] == 2020

    def test_missing_pmid_returns_none(self):
        article = _article("<MedlineCitation><Article></Article></MedlineCitation>")
        assert parse_doc(article, "f.xml.gz", "2024-01-01T00:00:00+00:00", "baseline", "idx") is None

    def test_missing_medline_returns_none(self):
        article = _article("")
        assert parse_doc(article, "f.xml.gz", "2024-01-01T00:00:00+00:00", "baseline", "idx") is None

    def test_source_file_basename_only(self):
        article = _article(MINIMAL_ARTICLE_XML)
        doc = parse_doc(article, "/long/path/to/pubmed26n0001.xml.gz", "2024-01-01T00:00:00+00:00", "updatefiles", "idx")
        assert doc["source_file"] == "pubmed26n0001.xml.gz"
