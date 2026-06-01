"""Extraction spine O7-O12 (design §8.3): normalize documents, segment spans, build
evidence cards, build ClaimLite rows + links, map citations, lay out report sections.

All deterministic and source-agnostic: these operators read the generic `documents`/
`spans`/`evidence` rows and never branch on a provider. A future LLM extractor can
replace an operator body (e.g. O9) without changing the row shapes downstream.
"""
from __future__ import annotations

from .. import ids, text
from ..runtime import RunContext
from ..workfiles import WorkStore
from .base import Operator

# Conservative assertion filter (design §8.3 O9): a span becomes evidence only if it is a
# substantive declarative statement, not a heading or boilerplate.
MIN_EVIDENCE_NONWS_CHARS = 80

# Fixed Phase 0 report sections, in order (design §13.1).
REPORT_SECTIONS = [
    ("Executive Run Summary", "topic, run ID, gate status, source pack summary, what can/cannot be claimed"),
    ("Source And Acquisition Coverage", "containers, selected items, successful + failed/skipped attempts, gaps"),
    ("Evidence-Backed Findings", "accepted claims with citation labels, evidence count, limitations"),
    ("Evidence Table", "evidence ID, item, document, span, summary, excerpt, limitations"),
    ("Gaps And Repair Tasks", "failed acquisitions, warning gates, weak coverage, repair tasks"),
    ("Traceability Appendix", "claim -> evidence -> span -> document -> selected item -> source container"),
    ("Full Source Appendix", "every supplied container and item, including unused seeds"),
]
FINDINGS_HEADING = "Evidence-Backed Findings"

_EVIDENCE_TYPE_TO_CLAIM_TYPE = {
    "definition": "definition",
    "risk": "risk_claim",
    "recommendation": "recommendation_claim",
}


def _nonws_len(s: str) -> int:
    return len("".join(s.split()))


def _classify(text_lower: str) -> str:
    """Generic evidence type from surface cues (design §7.10 enum). No domain vocabulary."""
    if '"' in text_lower:  # an actual quotation; a lone apostrophe (it's, org's) is NOT a quote
        return "quote"
    if any(k in text_lower for k in ("repository metrics", "stars:", "benchmark", "sha", "commit")):
        return "benchmark"
    if "release" in text_lower:
        return "product_release"
    if any(k in text_lower for k in ("code", "api", "identifier", "schema", "repository")):
        return "code"
    if any(k in text_lower for k in ("policy", "governance", "standard", "compliance")):
        return "policy"
    if any(k in text_lower for k in ("risk", "should not", "must not", "vulnerab", "danger")):
        return "risk"
    if any(k in text_lower for k in ("should ", "recommend", "best practice", "ought to")):
        return "recommendation"
    if any(k in text_lower for k in (" is ", " are ", " means ", "refers to", "defined as")):
        return "definition"
    if any(k in text_lower for k in ("for example", "e.g.", "such as")):
        return "example"
    return "source_statement"


def _scoring_weights(work: WorkStore) -> dict:
    contract = (work.read_rows("research_contracts") or [{}])[0]
    pack_id = contract.get("domain_pack_id")
    for row in work.read_rows("domain_packs"):
        if row.get("pack_id") == pack_id:
            return row.get("scoring_weights") or {}
    return {}


def _github_quality_score(provider_metadata: dict | None, scoring_weights: dict) -> float | None:
    if not isinstance(provider_metadata, dict):
        return None
    stars = provider_metadata.get("stars")
    scale = scoring_weights.get("github_stars_full_scale")
    try:
        stars_value = float(stars)
        scale_value = float(scale)
    except (TypeError, ValueError):
        return None
    if scale_value <= 0:
        return None
    return min(1.0, stars_value / scale_value)


def _github_metrics_block(provider_metadata: dict | None) -> str:
    if not isinstance(provider_metadata, dict):
        return ""
    parts = []
    if provider_metadata.get("stars") is not None:
        parts.append(f"stars: {provider_metadata['stars']}")
    if provider_metadata.get("releases_in_window") is not None:
        parts.append(f"releases in window: {provider_metadata['releases_in_window']}")
    if provider_metadata.get("last_release") is not None:
        parts.append(f"last release: {provider_metadata['last_release']}")
    if not parts:
        return ""
    return (
        "Repository metrics - " + "; ".join(parts)
        + ". These repository metrics are included as evidence for source quality and activity.\n\n"
    )


def _first_sentence(s: str, limit: int = 240) -> str:
    """First sentence as a single clean line (collapsed whitespace) for claim/summary text.
    The verbatim span stays in `quoted_text`; only this derived field is flattened."""
    s = " ".join(s.split())
    for end in (". ", "! ", "? "):
        idx = s.find(end)
        if 0 < idx < limit:
            return s[: idx + 1]
    return s[:limit].strip()


class DocumentNormalizeOperator(Operator):
    NAME = "DocumentNormalizeOperator"
    INPUT_SCHEMAS = ["documents"]
    OUTPUT_SCHEMAS = ["documents"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        n = 0
        scoring_weights = _scoring_weights(work)
        for doc in work.read_documents():
            raw_text = doc["raw_text"]
            if doc.get("document_kind") == "github_document":
                raw_text = _github_metrics_block(doc.get("provider_metadata")) + raw_text
                doc["source_quality_score"] = _github_quality_score(doc.get("provider_metadata"), scoring_weights)
            normalized = text.normalize(raw_text)
            doc["normalized_text"] = normalized
            doc["content_hash"] = ids.sha256_text(normalized)  # canonical-text hash
            doc["normalization"] = {"rules_applied": text.NORMALIZATION_RULES, "removed_content": False}
            work.write_document(doc)
            n += 1
        return {"documents_normalized": n}


class SpanSegmentOperator(Operator):
    NAME = "SpanSegmentOperator"
    INPUT_SCHEMAS = ["documents"]
    OUTPUT_SCHEMAS = ["spans"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        rows = []
        n = 0
        for doc in work.read_documents():
            normalized = doc.get("normalized_text") or ""
            for span_index, (start, end, span_text) in enumerate(text.segment(normalized)):
                rows.append({
                    "span_id": ids.mint(ctx.run_id, "SPAN", n),
                    "run_id": ctx.run_id,
                    "document_id": doc["document_id"],
                    "selected_item_id": doc["selected_item_id"],
                    "span_index": span_index,
                    "start_char": start,
                    "end_char": end,
                    "text": span_text,
                    "segmentation_strategy": "paragraph_first",
                    "text_hash": ids.sha256_text(span_text),
                })
                n += 1
        work.write_rows("spans", rows)
        return {"spans": len(rows)}


class EvidenceCardBuildOperator(Operator):
    NAME = "EvidenceCardBuildOperator"
    INPUT_SCHEMAS = ["spans"]
    OUTPUT_SCHEMAS = ["evidence"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        docs = {d["document_id"]: d for d in work.read_documents()}
        rows = []
        considered = skipped = 0
        for span in work.read_rows("spans"):
            considered += 1
            span_text = span["text"]
            stripped = span_text.strip()
            # Conservative assertion filter: skip short, heading, or non-declarative spans.
            if (_nonws_len(span_text) < MIN_EVIDENCE_NONWS_CHARS
                    or stripped.startswith("#")
                    or stripped.isupper()
                    or not any(c.isalpha() for c in stripped)):
                skipped += 1
                continue
            evidence_type = _classify(span_text.lower())
            doc = docs.get(span["document_id"], {})
            row = {
                "evidence_id": ids.mint(ctx.run_id, "EV", len(rows)),
                "run_id": ctx.run_id,
                "selected_item_id": span["selected_item_id"],
                "document_id": span["document_id"],
                "span_id": span["span_id"],
                "evidence_type": evidence_type,
                "summary": _first_sentence(stripped),
                "quoted_text": span_text,  # exact span text => contained within the span
                "support_strength": "single_source",
                "limitations": [],
                "published_at": doc.get("published_at"),
            }
            if "source_quality_score" in doc:
                row["source_quality_score"] = doc.get("source_quality_score")
            rows.append(row)
        work.write_rows("evidence", rows)
        return {"spans_considered": considered, "evidence_built": len(rows), "spans_skipped": skipped}


class ClaimLiteBuildOperator(Operator):
    NAME = "ClaimLiteBuildOperator"
    INPUT_SCHEMAS = ["evidence"]
    OUTPUT_SCHEMAS = ["claims", "claim_evidence", "claim_edges"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        claims, links, edges = [], [], []
        for ev in work.read_rows("evidence"):
            claim_id = ids.mint(ctx.run_id, "CLAIM", len(claims))
            claim_type = _EVIDENCE_TYPE_TO_CLAIM_TYPE.get(ev["evidence_type"], "technical_fact")
            claims.append({
                "claim_id": claim_id,
                "run_id": ctx.run_id,
                "claim_type": claim_type,
                "claim_text": ev["summary"],
                "claim_scope": "within provided source set",
                "criticality": "normal",  # the spine never invents criticality; the gate enforces policy
                "status": "accepted",      # accepted iff a supporting evidence row exists (added below)
                "confidence": "supported_by_source",
                "limitations": [],
            })
            links.append({"claim_id": claim_id, "evidence_id": ev["evidence_id"], "role": "supporting"})
            edges.append({"run_id": ctx.run_id, "from_id": ev["evidence_id"], "to_id": claim_id, "type": "supports"})
        work.write_rows("claims", claims)
        work.write_rows("claim_evidence", links)
        work.write_rows("claim_edges", edges)
        return {"claims": len(claims), "accepted": len(claims)}


class CitationMapBuildOperator(Operator):
    NAME = "CitationMapBuildOperator"
    INPUT_SCHEMAS = ["evidence", "claims"]
    OUTPUT_SCHEMAS = ["citations"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        accepted = {c["claim_id"] for c in work.read_rows("claims") if c["status"] == "accepted"}
        used_evidence = [
            ce["evidence_id"] for ce in work.read_rows("claim_evidence")
            if ce["claim_id"] in accepted and ce["role"] == "supporting"
        ]
        evidence = {e["evidence_id"]: e for e in work.read_rows("evidence")}
        spans = {s["span_id"]: s for s in work.read_rows("spans")}
        docs = {d["document_id"]: d for d in work.read_documents()}
        items = {i["selected_item_id"]: i for i in work.read_rows("selected_source_items")}

        rows = []
        seen: set[str] = set()
        dropped = 0
        for evidence_id in used_evidence:
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            ev = evidence.get(evidence_id)
            span = spans.get(ev["span_id"]) if ev else None
            doc = docs.get(ev["document_id"]) if ev else None
            item = items.get(ev["selected_item_id"]) if ev else None
            if not (ev and span and doc and item):  # path must resolve end-to-end
                dropped += 1
                continue
            locator = item.get("item_locator") or {}
            citation_url = _citation_url(locator, doc, span)
            rows.append({
                "citation_id": ids.mint(ctx.run_id, "CITE", len(rows)),
                "run_id": ctx.run_id,
                "evidence_id": evidence_id,
                "span_id": ev["span_id"],
                "document_id": ev["document_id"],
                "selected_item_id": ev["selected_item_id"],
                "label": f"CITE{len(rows) + 1:04d}",
                "url": citation_url,
                "accessed_at": item.get("accessed_at"),
            })
        work.write_rows("citations", rows)
        return {"citations": len(rows), "dropped_unresolvable": dropped}


def _citation_url(locator: dict, doc: dict, span: dict) -> str | None:
    base_url = locator.get("url")
    if not base_url:
        return None
    if doc.get("document_kind") == "github_document":
        normalized = doc.get("normalized_text") or ""
        start_line = 1 + normalized[:span["start_char"]].count("\n")
        end_line = 1 + normalized[:span["end_char"]].count("\n")
        return f"{base_url}#L{start_line}-L{end_line}"
    if doc.get("document_kind") == "youtube_transcript":
        seconds = _youtube_seconds_for_span(doc.get("provider_metadata"), span["start_char"])
        if seconds is not None:
            return f"{base_url}&t={seconds}s"
    return base_url


def _youtube_seconds_for_span(provider_metadata: dict | None, start_char: int) -> int | None:
    if not isinstance(provider_metadata, dict):
        return None
    segments = provider_metadata.get("segments")
    if not isinstance(segments, list):
        return None
    selected = None
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_start = segment.get("start_char")
        if not isinstance(segment_start, int):
            continue
        if segment_start <= start_char:
            selected = segment
        else:
            break
    if selected is None:
        return None
    seconds = selected.get("start_seconds")
    return seconds if isinstance(seconds, int) else None


class ReportBlueprintOperator(Operator):
    NAME = "ReportBlueprintOperator"
    INPUT_SCHEMAS = ["claims", "citations"]
    OUTPUT_SCHEMAS = ["report_sections", "section_claims", "section_citations"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        sections = []
        findings_id = None
        for order_index, (heading, purpose) in enumerate(REPORT_SECTIONS):
            section_id = ids.mint(ctx.run_id, "SEC", order_index)
            if heading == FINDINGS_HEADING:
                findings_id = section_id
            sections.append({
                "section_id": section_id,
                "run_id": ctx.run_id,
                "heading": heading,
                "purpose": purpose,
                "order_index": order_index,
                "section_status": "ready",
            })
        work.write_rows("report_sections", sections)

        # Findings cite accepted claims and their citations; the other sections render
        # directly from tables (coverage, gaps, traceability, appendix).
        accepted = [c["claim_id"] for c in work.read_rows("claims") if c["status"] == "accepted"]
        work.write_rows("section_claims", [{"section_id": findings_id, "claim_id": cid} for cid in accepted])
        work.write_rows("section_citations", [
            {"section_id": findings_id, "citation_id": c["citation_id"]} for c in work.read_rows("citations")
        ])
        return {"sections": len(sections), "findings_claims": len(accepted)}
