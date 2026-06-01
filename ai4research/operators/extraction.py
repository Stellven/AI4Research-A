"""Extraction spine O7-O12 (design §8.3): normalize documents, segment spans, build
evidence cards, build ClaimLite rows + links, map citations, lay out report sections.

All deterministic and source-agnostic: these operators read the generic `documents`/
`spans`/`evidence` rows and never branch on a provider. A future LLM extractor can
replace an operator body (e.g. O9) without changing the row shapes downstream.
"""
from __future__ import annotations

import re

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
                "claim_kind": "extractive",
                "claim_text": ev["summary"],
                "claim_scope": "within provided source set",
                "criticality": "normal",  # the spine never invents criticality; the gate enforces policy
                "status": "accepted",      # accepted iff a supporting evidence row exists (added below)
                "confidence": "supported_by_source",
                "limitations": [],
                "derivation": None,
            })
            links.append({"claim_id": claim_id, "evidence_id": ev["evidence_id"], "role": "supporting"})
            edges.append({"run_id": ctx.run_id, "from_id": ev["evidence_id"], "to_id": claim_id, "type": "supports"})
        work.write_rows("claims", claims)
        work.write_rows("claim_evidence", links)
        work.write_rows("claim_edges", edges)
        return {"claims": len(claims), "accepted": len(claims)}


def _numeric(value) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _repo_label(item: dict, doc: dict) -> str:
    return item.get("title") or doc.get("title") or item["selected_item_id"].rsplit(".", 1)[-1]


class MetricSynthesisOperator(Operator):
    NAME = "MetricSynthesisOperator"
    INPUT_SCHEMAS = ["claims", "evidence", "documents"]
    OUTPUT_SCHEMAS = ["claims", "claim_evidence", "claim_edges"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        contract = (work.read_rows("research_contracts") or [{}])[0]
        if contract.get("domain_pack_id") == "generic":
            return {"comparison_claims": 0, "trend_claims": 0}

        docs = {d["document_id"]: d for d in work.read_documents()}
        items = {i["selected_item_id"]: i for i in work.read_rows("selected_source_items")}
        metric_evidence = {}
        for ev in work.read_rows("evidence"):
            doc = docs.get(ev["document_id"])
            if (doc and doc.get("document_kind") == "github_document"
                    and ev.get("quoted_text", "").startswith("Repository metrics")):
                metric_evidence[ev["document_id"]] = ev

        repos = []
        for doc_id, ev in metric_evidence.items():
            doc = docs[doc_id]
            item = items.get(doc["selected_item_id"])
            metadata = doc.get("provider_metadata") if isinstance(doc.get("provider_metadata"), dict) else {}
            if not item:
                continue
            repos.append({
                "doc": doc,
                "item": item,
                "evidence": ev,
                "label": _repo_label(item, doc),
                "stars": _numeric(metadata.get("stars")),
                "releases": _numeric(metadata.get("releases_in_window")),
            })

        claims = work.read_rows("claims")
        links = work.read_rows("claim_evidence")
        edges = work.read_rows("claim_edges")
        comparison_claims = trend_claims = 0

        star_repos = [repo for repo in repos if repo["stars"] is not None]
        star_repos.sort(key=lambda repo: (-float(repo["stars"]), repo["label"]))
        if len(star_repos) >= 2 and float(star_repos[1]["stars"]) != 0:
            top, runner_up = star_repos[0], star_repos[1]
            ratio = round(float(top["stars"]) / float(runner_up["stars"]), 2)
            claim_id = ids.mint(ctx.run_id, "CLAIM", len(claims))
            derivation = {
                "method": "star_ratio",
                "inputs": [
                    {"evidence_id": top["evidence"]["evidence_id"], "label": top["label"], "value": top["stars"]},
                    {"evidence_id": runner_up["evidence"]["evidence_id"], "label": runner_up["label"],
                     "value": runner_up["stars"]},
                ],
                "computed": {"ratio": ratio},
            }
            claims.append({
                "claim_id": claim_id,
                "run_id": ctx.run_id,
                "claim_type": "comparison_claim",
                "claim_kind": "comparative",
                "claim_text": f"Repo {top['label']} has {ratio}x the stars of Repo {runner_up['label']} "
                              f"({top['stars']} vs {runner_up['stars']}).",
                "claim_scope": "within provided source set",
                "criticality": "normal",
                "status": "accepted",
                "confidence": "computed_from_metrics",
                "limitations": [],
                "derivation": derivation,
            })
            for inp in derivation["inputs"]:
                links.append({"claim_id": claim_id, "evidence_id": inp["evidence_id"], "role": "supporting"})
            edges.append({
                "run_id": ctx.run_id,
                "from_id": top["item"]["selected_item_id"],
                "to_id": runner_up["item"]["selected_item_id"],
                "type": "compares_to",
            })
            comparison_claims += 1

        for repo in repos:
            if repo["releases"] is None:
                continue
            claim_id = ids.mint(ctx.run_id, "CLAIM", len(claims))
            derivation = {
                "method": "release_count",
                "inputs": [{"evidence_id": repo["evidence"]["evidence_id"], "label": repo["label"],
                            "value": repo["releases"]}],
                "computed": {"count": repo["releases"]},
            }
            claims.append({
                "claim_id": claim_id,
                "run_id": ctx.run_id,
                "claim_type": "trend_claim",
                "claim_kind": "synthesized",
                "claim_text": f"Repo {repo['label']} had {repo['releases']} releases in the window.",
                "claim_scope": "within provided source set",
                "criticality": "normal",
                "status": "accepted",
                "confidence": "computed_from_metrics",
                "limitations": [],
                "derivation": derivation,
            })
            links.append({"claim_id": claim_id, "evidence_id": repo["evidence"]["evidence_id"], "role": "supporting"})
            trend_claims += 1

        work.write_rows("claims", claims)
        work.write_rows("claim_evidence", links)
        work.write_rows("claim_edges", edges)
        return {"comparison_claims": comparison_claims, "trend_claims": trend_claims}


def _term_pattern(term: str):
    escaped = re.escape(term.lower())
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")


class EntityTagOperator(Operator):
    NAME = "EntityTagOperator"
    INPUT_SCHEMAS = ["claims"]
    OUTPUT_SCHEMAS = ["entities", "claim_entities"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack_id = contract.get("domain_pack_id")
        pack = next((p for p in work.read_rows("domain_packs") if p.get("pack_id") == pack_id), {})
        vocabulary = pack.get("vocabulary") or []
        if not vocabulary:
            work.write_rows("entities", [])
            work.write_rows("claim_entities", [])
            return {"entities": 0, "links": 0}

        entities_by_name = {}
        links = []
        seen_links = set()
        for claim in work.read_rows("claims"):
            if claim.get("status") != "accepted":
                continue
            text_lower = claim.get("claim_text", "").lower()
            for entry in vocabulary:
                terms = [entry["canonical"]] + list(entry.get("synonyms") or [])
                if not any(_term_pattern(term).search(text_lower) for term in terms):
                    continue
                canonical = entry["canonical"]
                if canonical not in entities_by_name:
                    entities_by_name[canonical] = {
                        "entity_id": ids.mint(ctx.run_id, "ENT", len(entities_by_name)),
                        "run_id": ctx.run_id,
                        "canonical_name": canonical,
                        "entity_type": entry.get("entity_type") or "Entity",
                        "synonyms": entry.get("synonyms") or [],
                        "domain_tags": [],
                    }
                key = (claim["claim_id"], entities_by_name[canonical]["entity_id"])
                if key not in seen_links:
                    links.append({"claim_id": key[0], "entity_id": key[1]})
                    seen_links.add(key)

        entities = list(entities_by_name.values())
        work.write_rows("entities", entities)
        work.write_rows("claim_entities", links)
        return {"entities": len(entities), "links": len(links)}


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
    OUTPUT_SCHEMAS = ["report_sections", "figures", "section_claims", "section_citations"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        sections = []
        findings_id = None
        contract = (work.read_rows("research_contracts") or [{}])[0]
        pack = next((p for p in work.read_rows("domain_packs") if p.get("pack_id") == contract.get("domain_pack_id")), {})
        section_purposes = dict(REPORT_SECTIONS)
        headings = pack.get("required_sections") or [heading for heading, _ in REPORT_SECTIONS]
        for order_index, heading in enumerate(headings):
            section_id = ids.mint(ctx.run_id, "SEC", order_index)
            if heading == FINDINGS_HEADING:
                findings_id = section_id
            sections.append({
                "section_id": section_id,
                "run_id": ctx.run_id,
                "heading": heading,
                "purpose": section_purposes.get(heading),
                "order_index": order_index,
                "section_status": "ready",
            })
        work.write_rows("report_sections", sections)
        work.write_rows("figures", _figure_rows(ctx, work))

        # Findings cite accepted claims and their citations; the other sections render
        # directly from tables (coverage, gaps, traceability, appendix).
        accepted = [c["claim_id"] for c in work.read_rows("claims") if c["status"] == "accepted"]
        work.write_rows("section_claims", [{"section_id": findings_id, "claim_id": cid} for cid in accepted])
        work.write_rows("section_citations", [
            {"section_id": findings_id, "citation_id": c["citation_id"]} for c in work.read_rows("citations")
        ])
        return {"sections": len(sections), "findings_claims": len(accepted)}


def _figure_rows(ctx: RunContext, work: WorkStore) -> list[dict]:
    claims = [
        c for c in work.read_rows("claims")
        if c.get("status") == "accepted" and c.get("claim_kind") in ("synthesized", "comparative")
    ]
    if not claims:
        return []
    repos: dict[str, dict] = {}
    grounded_claim_ids = []
    for claim in claims:
        grounded_claim_ids.append(claim["claim_id"])
        derivation = claim.get("derivation") or {}
        method = derivation.get("method")
        for inp in derivation.get("inputs", []):
            label = inp.get("label")
            if not label:
                continue
            row = repos.setdefault(label, {"repo": label, "stars": None, "releases": None})
            if method == "star_ratio":
                row["stars"] = inp.get("value")
            elif method == "release_count":
                row["releases"] = inp.get("value")
    rows = [[r["repo"], r["stars"], r["releases"]] for r in sorted(repos.values(), key=lambda r: r["repo"])]
    return [{
        "figure_id": ids.mint(ctx.run_id, "FIG", 0),
        "run_id": ctx.run_id,
        "kind": "comparison_matrix",
        "spec": {"columns": ["repo", "stars", "releases"], "rows": rows},
        "grounded_claim_ids": grounded_claim_ids,
    }]
