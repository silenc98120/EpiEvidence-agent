"""统一检索结果的确定性去重。

该模块只负责判断哪些 ``EvidenceRecord`` 指向同一篇文献，以及合并一组记录中
可以安全合并的字段。原始来源记录仍然应该先写入 ``source_records``，本模块不
负责数据库事务，也不删除任何来源记录。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from time import perf_counter

from loguru import logger
from pydantic import BaseModel, Field
from enum import StrEnum

from app.core.search_results_normalizer import (
    EvidenceRecord,
    FullTextResourceCandidate,
    PeerReviewStatus,
)


class DeduplicationKeyKind(StrEnum):
    """用于跨来源判断同一文献的身份键类型。"""

    DOI = "doi"
    PMID = "pmid"
    PMCID = "pmcid"
    TITLE_YEAR_AUTHOR = "title_year_author"


class DeduplicationKey(BaseModel):
    """一条保守的、可解释的去重键。"""

    kind: DeduplicationKeyKind
    value: str = Field(min_length=1)


class DeduplicationCluster(BaseModel):
    """一组被判定为同一篇文献的来源记录。"""

    cluster_id: str = Field(min_length=1)
    canonical_record: EvidenceRecord
    members: list[EvidenceRecord] = Field(min_length=1)
    matched_by: list[DeduplicationKey] = Field(default_factory=list)

    @property
    def duplicate_count(self) -> int:
        return max(0, len(self.members) - 1)


class DeduplicationResult(BaseModel):
    """一批检索结果的去重结果和可观测统计。"""

    records: list[EvidenceRecord] = Field(default_factory=list)
    clusters: list[DeduplicationCluster] = Field(default_factory=list)
    input_count: int = Field(ge=0)
    unique_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    latency_ms: float = Field(ge=0.0)


def build_deduplication_keys(record: EvidenceRecord) -> list[DeduplicationKey]:
    """为一条统一文献生成可解释的身份键。

    DOI、PMID 和 PMCID 是强身份键。只有记录完全缺少这些强身份键时，才使用题名、
    年份和第一作者组合的兜底键；缺少任意一部分时不生成该键，避免把拥有不同 DOI
    或 PMID 的不同文献误合并。
    """

    keys: list[DeduplicationKey] = []
    if record.doi:
        keys.append(
            DeduplicationKey(
                kind=DeduplicationKeyKind.DOI,
                value=record.doi.strip().casefold(),
            )
        )
    if record.pmid:
        keys.append(
            DeduplicationKey(
                kind=DeduplicationKeyKind.PMID,
                value=record.pmid.strip(),
            )
        )
    if record.pmcid:
        keys.append(
            DeduplicationKey(
                kind=DeduplicationKeyKind.PMCID,
                value=record.pmcid.strip().upper(),
            )
        )

    first_author = _normalize_identity_text(record.first_author)
    if not first_author and record.authors:
        first_author = _normalize_identity_text(record.authors[0].full_name)
    has_strong_identifier = any((record.doi, record.pmid, record.pmcid))
    if not has_strong_identifier and record.publication_year is not None and first_author:
        value = f"{record.normalized_title}|{record.publication_year}|{first_author}"
        keys.append(
            DeduplicationKey(
                kind=DeduplicationKeyKind.TITLE_YEAR_AUTHOR,
                value=value,
            )
        )
    return keys


class Deduplicator:
    """在当前批次内进行确定性、可解释、保守的跨来源去重。"""

    def deduplicate(self, records: list[EvidenceRecord]) -> DeduplicationResult:
        start_time = perf_counter()
        if not records:
            return DeduplicationResult(
                records=[],
                clusters=[],
                input_count=0,
                unique_count=0,
                duplicate_count=0,
                conflict_count=0,
                latency_ms=0.0,
            )

        parent = list(range(len(records)))
        key_to_first_index: dict[tuple[DeduplicationKeyKind, str], int] = {}
        record_keys: list[list[DeduplicationKey]] = []

        for index, record in enumerate(records):
            keys = build_deduplication_keys(record)
            record_keys.append(keys)
            for key in keys:
                lookup_key = (key.kind, key.value)
                previous = key_to_first_index.get(lookup_key)
                if previous is None:
                    key_to_first_index[lookup_key] = index
                else:
                    self._union(parent, index, previous)

        grouped_indexes: dict[int, list[int]] = defaultdict(list)
        for index in range(len(records)):
            grouped_indexes[self._find(parent, index)].append(index)

        clusters: list[DeduplicationCluster] = []
        conflict_count = 0
        for indexes in sorted(grouped_indexes.values(), key=lambda values: values[0]):
            members = [records[index] for index in indexes]
            matched_by = _matched_keys(indexes, record_keys)
            if _has_conflicting_strong_identifiers(members):
                conflict_count += 1
                logger.bind(
                    component="deduplicator",
                    event="deduplication_identity_conflict",
                    member_count=len(members),
                    matched_by=[key.kind.value for key in matched_by],
                ).warning("同一去重簇包含冲突的强身份标识，保留全部来源记录")

            canonical = _merge_cluster(members)
            cluster_id = _cluster_id(canonical, indexes, record_keys)
            clusters.append(
                DeduplicationCluster(
                    cluster_id=cluster_id,
                    canonical_record=canonical,
                    members=members,
                    matched_by=matched_by,
                )
            )

        latency_ms = (perf_counter() - start_time) * 1000
        result = DeduplicationResult(
            records=[cluster.canonical_record for cluster in clusters],
            clusters=clusters,
            input_count=len(records),
            unique_count=len(clusters),
            duplicate_count=len(records) - len(clusters),
            conflict_count=conflict_count,
            latency_ms=latency_ms,
        )
        logger.bind(
            component="deduplicator",
            event="deduplication_completed",
            input_count=result.input_count,
            unique_count=result.unique_count,
            duplicate_count=result.duplicate_count,
            conflict_count=result.conflict_count,
            latency_ms=round(result.latency_ms, 1),
        ).info("文献去重完成")
        return result

    @staticmethod
    def _find(parent: list[int], index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    @classmethod
    def _union(cls, parent: list[int], left: int, right: int) -> None:
        left_root = cls._find(parent, left)
        right_root = cls._find(parent, right)
        if left_root != right_root:
            parent[right_root] = left_root


def _matched_keys(
    indexes: list[int],
    record_keys: list[list[DeduplicationKey]],
) -> list[DeduplicationKey]:
    counts: dict[tuple[DeduplicationKeyKind, str], int] = defaultdict(int)
    representatives: dict[tuple[DeduplicationKeyKind, str], DeduplicationKey] = {}
    for index in indexes:
        for key in record_keys[index]:
            lookup_key = (key.kind, key.value)
            counts[lookup_key] += 1
            representatives[lookup_key] = key
    return [
        representatives[lookup_key]
        for lookup_key, count in sorted(
            counts.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
        if count > 1
    ]


def _merge_cluster(members: list[EvidenceRecord]) -> EvidenceRecord:
    ordered = sorted(members, key=_canonical_sort_key, reverse=True)
    canonical = ordered[0]
    data = canonical.model_dump(mode="python")
    data.update(
        doi=_first_value(ordered, "doi"),
        pmid=_first_value(ordered, "pmid"),
        pmcid=_first_value(ordered, "pmcid"),
        published_doi=_first_value(ordered, "published_doi"),
        abstract=_longest_text(ordered, "abstract"),
        abstract_available=any(record.abstract_available for record in ordered),
        authors=_first_non_empty_list(ordered, "authors"),
        first_author=_first_value(ordered, "first_author"),
        journal_title=_first_value(ordered, "journal_title"),
        journal_abbreviation=_first_value(ordered, "journal_abbreviation"),
        issn=_first_value(ordered, "issn"),
        electronic_issn=_first_value(ordered, "electronic_issn"),
        publication_date=_first_value(ordered, "publication_date"),
        publication_year=_first_value(ordered, "publication_year"),
        publication_types=_merge_text_lists(ordered, "publication_types"),
        study_design=_first_value(ordered, "study_design"),
        publication_status=_first_value(ordered, "publication_status"),
        language=_first_value(ordered, "language"),
        keywords=_merge_text_lists(ordered, "keywords"),
        peer_review_status=_merged_peer_review_status(ordered),
        preprint_server=_first_value(ordered, "preprint_server"),
        preprint_version=_max_value(ordered, "preprint_version"),
        is_open_access=_merged_optional_bool(ordered, "is_open_access"),
        has_full_text=any(record.has_full_text for record in ordered),
        can_download_full_text=any(record.can_download_full_text for record in ordered),
        license=_first_value(ordered, "license"),
        full_text_resources=_merge_resources(ordered),
        citation_count=_max_value(ordered, "citation_count"),
        is_retracted=any(record.is_retracted for record in ordered),
        landing_url=_first_value(ordered, "landing_url"),
        retrieved_at=max(record.retrieved_at for record in ordered),
    )
    return EvidenceRecord.model_validate(data)


def _canonical_sort_key(record: EvidenceRecord) -> tuple[int, int, int, int, int, int, int]:
    review_priority = {
        PeerReviewStatus.PEER_REVIEWED: 2,
        PeerReviewStatus.UNKNOWN: 1,
        PeerReviewStatus.PREPRINT: 0,
    }[record.peer_review_status]
    source_priority = {
        "europe_pmc": 5,
        "pmc": 4,
        "pubmed": 3,
        "semantic_scholar": 2,
        "biorxiv": 1,
        "medrxiv": 0,
    }.get(record.source.value, 0)
    rank_priority = -record.source_rank if record.source_rank is not None else -100000
    identifier_count = sum(bool(value) for value in (record.doi, record.pmid, record.pmcid))
    return (
        review_priority,
        int(record.can_download_full_text),
        int(record.abstract_available),
        int(record.has_full_text),
        identifier_count,
        source_priority,
        rank_priority,
    )


def _cluster_id(
    canonical: EvidenceRecord,
    indexes: list[int],
    record_keys: list[list[DeduplicationKey]],
) -> str:
    keys = [key for index in indexes for key in record_keys[index]]
    if keys:
        identity = "|".join(sorted(f"{key.kind.value}:{key.value}" for key in keys))
    else:
        identity = f"unidentified:{canonical.normalized_title}:{indexes[0]}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _has_conflicting_strong_identifiers(members: list[EvidenceRecord]) -> bool:
    for field_name in ("doi", "pmid", "pmcid"):
        values = {
            _normalize_strong_identifier(field_name, getattr(record, field_name))
            for record in members
            if getattr(record, field_name)
        }
        if len(values) > 1:
            return True
    return False


def _normalize_strong_identifier(field_name: str, value: str) -> str:
    if field_name == "doi":
        return value.strip().casefold()
    if field_name == "pmcid":
        return value.strip().upper()
    return value.strip()


def _normalize_identity_text(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = " ".join(normalized.split())
    return normalized or None


def _first_value(records: list[EvidenceRecord], field_name: str):
    for record in records:
        value = getattr(record, field_name)
        if value not in (None, ""):
            return value
    return None


def _first_non_empty_list(records: list[EvidenceRecord], field_name: str):
    for record in records:
        value = getattr(record, field_name)
        if value:
            return value
    return []


def _longest_text(records: list[EvidenceRecord], field_name: str) -> str | None:
    values = [getattr(record, field_name) for record in records]
    values = [value for value in values if value]
    return max(values, key=len) if values else None


def _merge_text_lists(records: list[EvidenceRecord], field_name: str) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for record in records:
        for value in getattr(record, field_name):
            normalized = _normalize_identity_text(value)
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(value)
    return merged


def _merge_resources(records: list[EvidenceRecord]) -> list[FullTextResourceCandidate]:
    merged: list[FullTextResourceCandidate] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        for resource in record.full_text_resources:
            key = (resource.format.value, resource.url.casefold())
            if key not in seen:
                seen.add(key)
                merged.append(resource)
    return merged


def _merged_peer_review_status(records: list[EvidenceRecord]) -> PeerReviewStatus:
    statuses = {record.peer_review_status for record in records}
    if PeerReviewStatus.PEER_REVIEWED in statuses:
        return PeerReviewStatus.PEER_REVIEWED
    if PeerReviewStatus.UNKNOWN in statuses:
        return PeerReviewStatus.UNKNOWN
    return PeerReviewStatus.PREPRINT


def _merged_optional_bool(records: list[EvidenceRecord], field_name: str) -> bool | None:
    values = [getattr(record, field_name) for record in records]
    if True in values:
        return True
    if all(value is False for value in values if value is not None) and any(
        value is False for value in values
    ):
        return False
    return None


def _max_value(records: list[EvidenceRecord], field_name: str):
    values = [getattr(record, field_name) for record in records]
    values = [value for value in values if value is not None]
    return max(values) if values else None
