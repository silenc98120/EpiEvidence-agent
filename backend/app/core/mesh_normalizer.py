"""MeSH术语匹配与近似词召回模块
该模块负责对 IntentRecognizer返回的ResearchKeywords进行MeSH术语匹配与entry terms返回。
"""
from time import perf_counter

import unicodedata
from typing import Literal
from pydantic import BaseModel,Field

from pathlib import Path
import sqlite3

from backend.app.core.intent_recognizer import (
    IntentRecognitionResult,
    ResearchKeywordKind,
    KeywordCandidate,
)

from loguru import logger

class MeshNormalizedKeyword(BaseModel):
    """单个研究关键词的 MeSH 数据库精准匹配标准化结果。"""

    kind: ResearchKeywordKind
    raw_text: str = Field(min_length=1, max_length=200)
    lookup_terms: list[str] = Field(default_factory=list)
    llm_confidence: float = Field(ge=0.0, le=1.0)
    mesh_matches: list[MeshMatch] = Field(default_factory=list)

class MeshNormalizationResult(BaseModel):
    """当前意图识别结果中所有研究关键词 MeSH 数据库精准匹配标准化结果。"""

    normalized_keywords:list[MeshNormalizedKeyword] = Field(default_factory=list)

class MeshNormalizer:
    """编排关键词格式化与 MeSH 数据库精确匹配，为 agent 编排提供接口。"""

    def __init__(self,repositroy:MeshRepository)-> None:
        self._repository = repositroy

    def normalize(self, intent:IntentRecognitionResult)->MeshNormalizationResult:
        start_time = perf_counter()
        try:
            lookup_requests = prepare_keyword_lookups(intent)

            normalized_keywords:list[MeshNormalizedKeyword] = []

            for lookup_request in lookup_requests:
                matches_by_mesh_id:dict[str,MeshMatch] = {}

                for lookup_term in lookup_request.lookup_terms:
                    matches = self._repository.find_exact(lookup_term)

                    for match in matches:
                        matches_by_mesh_id.setdefault(match.mesh_id,match)

                normalized_keyword = MeshNormalizedKeyword(
                    kind=lookup_request.kind,
                    raw_text=lookup_request.raw_text,
                    lookup_terms=lookup_request.lookup_terms,
                    llm_confidence=lookup_request.llm_confidence,
                    mesh_matches=list(matches_by_mesh_id.values()),
                )

                normalized_keywords.append(normalized_keyword)
            result = MeshNormalizationResult(
                normalized_keywords=normalized_keywords,
                )


        except Exception as exc:
            latency_ms = (perf_counter() - start_time) * 1000

            logger.bind(
                component="mesh_normalizer",
                event="mesh_normalization_failed",
                error_type=type(exc).__name__,
                latency_ms=round(latency_ms, 1),
            ).exception("MeSH 术语标准化失败")

            raise

        keyword_count = len(result.normalized_keywords)

        matched_keyword_count = sum(
            bool(keyword.mesh_matches)
            for keyword in result.normalized_keywords
        )

        mesh_match_count = sum(
            len(keyword.mesh_matches)
            for keyword in result.normalized_keywords
        )

        latency_ms = (perf_counter() - start_time) * 1000

        logger.bind(
            component="mesh_normalizer",
            event="mesh_normalization_completed",
            keyword_count=keyword_count,
            matched_keyword_count=matched_keyword_count,
            unmatched_keyword_count=(
                    keyword_count - matched_keyword_count
            ),
            mesh_match_count=mesh_match_count,
            latency_ms=round(latency_ms, 1),
        ).info("MeSH 术语标准化完成")

        return result

class KeywordLookupRequest(BaseModel):
    """校验等待查询 MeSH 数据库的研究关键词。"""

    kind:ResearchKeywordKind
    raw_text:str = Field(min_length=1,max_length=200)
    lookup_terms:list[str] = Field(default_factory=list)
    llm_confidence:float =Field(ge=0.0,le=1.0)

def prepare_keyword_lookups(intent:IntentRecognitionResult)->list[KeywordLookupRequest]:
    """对 IntentRecognitionResult 中需要匹配的研究关键词进行提取。"""

    lookup_requests:list[KeywordLookupRequest] = []

    for kind, keyword_candidates in intent.keywords.items():
        for keyword in keyword_candidates:
            lookup_request = KeywordLookupRequest(
                kind=kind,
                raw_text=keyword.raw_text,
                lookup_terms=prepare_lookup_terms(keyword),
                llm_confidence=keyword.confidence,
            )
            lookup_requests.append(lookup_request)

    return lookup_requests

def normalize_term(value: str) -> str:
    """对待查询keywords进行标准化。"""

    normalized = unicodedata.normalize("NFKC",value)
    return " ".join(normalized.casefold().split())

def prepare_lookup_terms(keyword:KeywordCandidate) -> list[str]:
    """对研究关键词英文候选进行格式化和去重。"""

    candidate_terms = [
        *keyword.english_candidates,
    ]

    lookup_terms:list[str] = []
    seen_normalized_terms:set[str] = set()

    for candidate_term in candidate_terms:
        cleaned_term = candidate_term.strip()
        normalized_term = normalize_term(cleaned_term)

        if not normalized_term:
            continue

        if normalized_term in seen_normalized_terms:
            continue

        seen_normalized_terms.add(normalized_term)
        lookup_terms.append(cleaned_term)

    return lookup_terms

class MeshMatch(BaseModel):
    """经过本地 MeSH 数据库精确匹配的返回结果。"""

    mesh_id:str = Field(min_length=1)
    preferred_label:str = Field(min_length=1)
    matched_term:str = Field(min_length=1)
    matched_term_type: Literal["preferred", "entry"]
    entry_terms: list[str] = Field(default_factory=list)

class MeshRepository:
    """负责查询本地 MeSH SQLite 数据库。"""

    def __init__(self,database_path:Path):
        if not database_path.is_file():
            raise FileNotFoundError(
                f"MeSH 数据库不存在: {database_path}"
            )
        self._database_path = database_path

    def _connect(self)->sqlite3.Connection:
        database_uri =(
            f"file:{self._database_path.resolve().as_posix()}?mode=ro"
        )

        connection = sqlite3.connect(
            database_uri,
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        return connection
    def find_exact(self,term:str)->list[MeshMatch]:
        normalized_term = normalize_term(term)

        if not normalized_term:
            return []

        connection = self._connect()

        try:
            matched_rows = connection.execute(
                """
                SELECT 
                    c.mesh_id,
                    c.preferred_label,
                    t.term AS matched_term,
                    t.term_type AS matched_term_type
                FROM mesh_terms AS t
                JOIN mesh_concepts AS c
                    ON c.mesh_id = t.mesh_id
                WHERE t.normalized_term = ?
                  AND c.obsolete = 0
                ORDER BY c.mesh_id
                """,
                (normalized_term,),
            ).fetchall()

            matches:list[MeshMatch] = []

            for row in matched_rows:
                entry_rows = connection.execute(
                    """
                    SELECT term FROM mesh_terms
                    WHERE mesh_id = ? AND term_type = 'entry'
                    ORDER BY term COLLATE NOCASE
                    """,
                    (row["mesh_id"],),
                ).fetchall()

                entry_terms = [entry_row["term"] for entry_row in entry_rows]

                matches.append(
                    MeshMatch(
                        mesh_id=row["mesh_id"],
                        preferred_label=row["preferred_label"],
                        matched_term=row["matched_term"],
                        matched_term_type=row["matched_term_type"],
                        entry_terms=entry_terms,
                    )
                )
            return matches
        finally:
            connection.close()

