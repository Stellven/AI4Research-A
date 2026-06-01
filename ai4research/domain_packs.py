"""Domain pack registry.

A domain pack is deterministic run configuration: source policy, vocabulary, scoring
weights, required gates, and a question template. The generic pack preserves the Phase 0
default behavior.
"""
from __future__ import annotations

from copy import deepcopy

from .operators.extraction import REPORT_SECTIONS

_SECTION_HEADINGS = [heading for heading, _ in REPORT_SECTIONS]
_BASE_GATES = [
    "RequiredRowsGate",
    "SchemaValidationGate",
    "ProviderFieldQuarantineGate",
    "ReferenceIntegrityGate",
    "SpanOffsetGate",
    "ClaimSupportGate",
    "CriticalClaimGate",
    "CitationResolutionGate",
    "ReportGroundingGate",
    "SourceCoverageGate",
    "SourceSetLimitationsGate",
]

DOMAIN_PACKS: dict[str, dict] = {
    "generic": {
        "pack_id": "generic",
        "version": "0.1.0",
        "allowed_source_pack_types": ["local_document_set", "youtube_channel", "github_repo"],
        "freshness_window_days": None,
        "max_items_per_container": None,
        "vocabulary": [],
        "scoring_weights": {},
        "required_sections": _SECTION_HEADINGS,
        "required_gates": list(_BASE_GATES),
        "question_template": [{"id": "Q0", "type": "root_question", "text": "<topic>"}],
    },
    "youtube_github_research": {
        "pack_id": "youtube_github_research",
        "version": "0.1.0",
        "allowed_source_pack_types": ["local_document_set", "youtube_channel", "github_repo"],
        "freshness_window_days": 180,
        "max_items_per_container": 5,
        "vocabulary": [
            {"canonical": "skills taxonomy", "synonyms": ["competency framework"], "entity_type": "Standard"},
            {"canonical": "verifiable credential", "synonyms": ["portable credential"], "entity_type": "Credential"},
            {"canonical": "repository activity", "synonyms": ["release cadence"], "entity_type": "Metric"},
            {"canonical": "governance risk", "synonyms": ["limitation"], "entity_type": "Risk"},
        ],
        "scoring_weights": {"github_stars_full_scale": 10000},
        "required_sections": _SECTION_HEADINGS,
        "required_gates": list(_BASE_GATES) + ["QuestionCoverageGate"],
        "question_template": [
            {"id": "Q0", "type": "root_question", "text": "<topic>"},
            {"id": "Q1", "type": "sub_question", "text": "Which repositories are most active?"},
            {"id": "Q2", "type": "sub_question", "text": "What do the channels say about the topic?"},
            {"id": "Q3", "type": "sub_question", "text": "What risks or limitations are noted?"},
        ],
    },
}


def get_pack(pack_id: str | None) -> dict:
    resolved = pack_id or "generic"
    if resolved not in DOMAIN_PACKS:
        raise ValueError(f"unknown domain_pack: {resolved}")
    return deepcopy(DOMAIN_PACKS[resolved])


def pack_rows() -> list[dict]:
    return [
        {
            "pack_id": pack["pack_id"],
            "version": pack["version"],
            "source_families": list(pack["allowed_source_pack_types"]),
            "freshness_window_days": pack["freshness_window_days"],
            "max_items_per_container": pack["max_items_per_container"],
            "vocabulary": pack["vocabulary"],
            "scoring_weights": pack["scoring_weights"],
            "required_sections": pack["required_sections"],
            "required_gates": pack["required_gates"],
            "question_template": pack["question_template"],
        }
        for pack in DOMAIN_PACKS.values()
    ]
