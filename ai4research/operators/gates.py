"""PreRenderQualityGateSuite (design §8.4, §11). Deterministic validators run on the
working files before rendering. Each gate returns a structured result; the suite writes
`gate_results`, the `quality_dossier`, and `repair_tasks`, and decides whether rendering
is approved. Product invariant: nothing unsupported, unreferenced, or improperly
provider-specific reaches the report.

Working files have no enforced foreign keys, so these gates check referential integrity
before `PersistRunToStore`; SQLite re-confirms references at load.
"""
from __future__ import annotations

import re

from .. import ids
from ..adapters import SOURCE_PACK_MAP
from ..runtime import RunContext
from ..workfiles import WorkStore
from .base import Operator

GATE_VERSION = "0.1.0"

# status: pass | warning | repairable_fail | hard_fail   severity: blocking | warning
PASS, WARNING, HARD_FAIL = "pass", "warning", "hard_fail"
BLOCKING, WARN = "blocking", "warning"

# Source-agnostic invariant (§8.4): generic tables carry only their documented generic
# columns; provider data lives only in provider_metadata. The gate is a NAME whitelist —
# any column not in a table's allowed set is flagged, so unanticipated provider fields
# (a future vimeo_id, spotify_uri, ...) are caught without enumerating them.
_GENERIC_TABLE_COLUMNS = {
    "selected_source_items": {"selected_item_id", "run_id", "container_id", "source_family",
                              "adapter_id", "item_locator", "title", "creator", "published_at",
                              "accessed_at", "source_rank", "selection_reason", "acquisition_status",
                              "provider_metadata"},
    "spans": {"span_id", "run_id", "document_id", "selected_item_id", "span_index",
              "start_char", "end_char", "text", "segmentation_strategy", "text_hash"},
    "evidence": {"evidence_id", "run_id", "selected_item_id", "document_id", "span_id",
                 "evidence_type", "summary", "quoted_text", "support_strength", "limitations",
                 "published_at", "source_quality_score"},
    "claims": {"claim_id", "run_id", "claim_type", "claim_text", "claim_scope",
               "criticality", "status", "confidence", "limitations"},
    "claim_evidence": {"claim_id", "evidence_id", "role"},
    "citations": {"citation_id", "run_id", "evidence_id", "span_id", "document_id",
                  "selected_item_id", "label", "url", "accessed_at"},
}
# Generic JSON columns are recursed into; provider_metadata is the one sanctioned bag.
_ALLOWED_LOCATOR_KEYS = {"url", "local_path", "local_fixture_path", "inline_text"}
# Provider name fragments — a secondary scan of free-form nested JSON (limitations).
_PROVIDER_KEY_FRAGMENTS = ("youtube", "github", "video_id", "channel", "timestamp_url",
                           "watch_url", "repo", "sha", "stars", "handle", "transcript")

def _enums() -> dict[tuple[str, str], set[str]]:
    return {
    ("runs", "status"): {"initialized", "running", "finalized", "diagnostic_only", "failed"},
    ("acquisition_attempts", "status"): {"succeeded", "failed", "skipped"},
    ("source_containers", "source_pack_type"): set(SOURCE_PACK_MAP),
    ("documents", "document_kind"): {doc_kind for _, _, doc_kind in SOURCE_PACK_MAP.values()},
    ("evidence", "evidence_type"): {"definition", "source_statement", "example", "risk",
                                    "recommendation", "limitation", "unknown", "quote",
                                    "benchmark", "code", "policy", "product_release"},
    ("claims", "claim_type"): {"definition", "technical_fact", "risk_claim", "recommendation_claim"},
    ("claims", "status"): {"draft", "accepted", "qualified", "rejected"},
    ("claims", "criticality"): {"normal", "critical"},
    ("claim_evidence", "role"): {"supporting", "contradicting", "qualifying"},
    }
_REQUIRED_TABLES = ["runs", "research_contracts", "question_graph_nodes",
                    "physical_plan_nodes", "source_containers", "selected_source_items"]
_REQUIRED_COLUMNS = {
    "runs": ["run_id", "topic", "status"],
    "selected_source_items": ["selected_item_id", "container_id", "source_family", "adapter_id"],
    "documents": ["document_id", "selected_item_id", "content_hash"],
    "spans": ["span_id", "document_id", "start_char", "end_char", "text"],
    "evidence": ["evidence_id", "span_id", "summary", "quoted_text"],
    "claims": ["claim_id", "claim_type", "status"],
    "citations": ["citation_id", "evidence_id", "label"],
}
_PRIMARY_KEYS = {
    "runs": ["run_id"],
    "selected_source_items": ["selected_item_id"],
    "documents": ["document_id"],
    "spans": ["span_id"],
    "evidence": ["evidence_id"],
    "claims": ["claim_id"],
    "citations": ["citation_id"],
}


def _result(gate_id, status, severity, checked, issues, metrics=None):
    return {"gate_id": gate_id, "status": status, "severity": severity,
            "checked_tables": checked, "issues": issues, "metrics": metrics or {}}


def _ok(gate_id, severity, checked, metrics=None):
    return _result(gate_id, PASS, severity, checked, [], metrics)


# --- individual gates: each takes the work snapshot + contract, returns one result ---

def gate_required_rows(s, contract):
    missing = [t for t in _REQUIRED_TABLES if not s.get(t)]
    if missing:
        return _result("RequiredRowsGate", HARD_FAIL, BLOCKING, _REQUIRED_TABLES,
                       [f"required table empty: {t}" for t in missing])
    return _ok("RequiredRowsGate", BLOCKING, _REQUIRED_TABLES)


def gate_schema_validation(s, contract):
    issues = []
    enums = _enums()
    for table, cols in _REQUIRED_COLUMNS.items():
        for row in s.get(table, []):
            for col in cols:
                if col not in row or row[col] is None:
                    issues.append(f"{table}.{col} is required")
    for table, cols in _PRIMARY_KEYS.items():
        seen = set()
        for row in s.get(table, []):
            key = tuple(row.get(col) for col in cols)
            if any(value is None for value in key):
                continue
            if key in seen:
                issues.append(f"{table} duplicate primary key {key}")
            seen.add(key)
    for (table, col), allowed in enums.items():
        for row in s.get(table, []):
            if col in row and row[col] not in allowed:
                issues.append(f"{table}.{col}={row[col]!r} not in {sorted(allowed)}")
    for sp in s.get("spans", []):
        if not (isinstance(sp.get("start_char"), int) and isinstance(sp.get("end_char"), int)
                and sp["end_char"] > sp["start_char"]):
            issues.append(f"span {sp.get('span_id')} has invalid offsets")
    for ev in s.get("evidence", []):
        if not (ev.get("summary") or "").strip() or not (ev.get("quoted_text") or "").strip():
            issues.append(f"evidence {ev.get('evidence_id')} missing summary/quoted_text")
    status, sev = (HARD_FAIL, BLOCKING) if issues else (PASS, BLOCKING)
    checked = sorted({t for (t, _) in enums} | set(_REQUIRED_COLUMNS) | set(_PRIMARY_KEYS) | {"spans", "evidence"})
    return _result("SchemaValidationGate", status, sev, checked, issues)


def gate_provider_quarantine(s, contract):
    issues = []
    for table, allowed in _GENERIC_TABLE_COLUMNS.items():
        for row in s.get(table, []):
            for key, value in row.items():
                if key == "provider_metadata":
                    continue  # the one sanctioned provider bag
                if key not in allowed:
                    issues.append(f"{table} has non-generic column '{key}' outside provider_metadata")
                    continue
                # recurse into the known generic JSON columns (provider_metadata excepted)
                if key == "item_locator" and isinstance(value, dict):
                    issues += [f"{table}.item_locator has non-generic key '{k}'"
                               for k in value if k not in _ALLOWED_LOCATOR_KEYS]
                elif key == "limitations" and isinstance(value, list):
                    for entry in value:
                        if isinstance(entry, dict):
                            issues += [f"{table}.limitations carries provider key '{k}'"
                                       for k in entry if any(fr in k.lower() for fr in _PROVIDER_KEY_FRAGMENTS)]
    status, sev = (HARD_FAIL, BLOCKING) if issues else (PASS, BLOCKING)
    return _result("ProviderFieldQuarantineGate", status, sev, list(_GENERIC_TABLE_COLUMNS), issues)


def gate_reference_integrity(s, contract):
    issues = []
    containers = {c["container_id"] for c in s["source_containers"]}
    items = {i["selected_item_id"]: i for i in s["selected_source_items"]}
    attempts = {a["attempt_id"] for a in s.get("acquisition_attempts", [])}
    docs = {d["document_id"]: d for d in s.get("documents", [])}
    spans = {sp["span_id"]: sp for sp in s.get("spans", [])}
    evidence = {e["evidence_id"]: e for e in s.get("evidence", [])}
    claims = {c["claim_id"] for c in s.get("claims", [])}

    for i in s["selected_source_items"]:
        if i["container_id"] not in containers:
            issues.append(f"item {i['selected_item_id']} -> missing container {i['container_id']}")
    for a in s.get("acquisition_attempts", []):
        if a["selected_item_id"] not in items:
            issues.append(f"attempt {a['attempt_id']} -> missing item")
    for d in docs.values():
        if d["selected_item_id"] not in items:
            issues.append(f"document {d['document_id']} -> missing item")
        if d["acquisition_attempt_id"] not in attempts:
            issues.append(f"document {d['document_id']} -> missing attempt")
    for sp in spans.values():
        if sp["document_id"] not in docs:
            issues.append(f"span {sp['span_id']} -> missing document")
    for e in evidence.values():
        sp = spans.get(e["span_id"])
        if sp is None:
            issues.append(f"evidence {e['evidence_id']} -> missing span")
        elif e["document_id"] != sp["document_id"] or e["selected_item_id"] != sp["selected_item_id"]:
            issues.append(f"evidence {e['evidence_id']} path inconsistent with its span")
    for ce in s.get("claim_evidence", []):
        if ce["claim_id"] not in claims or ce["evidence_id"] not in evidence:
            issues.append(f"claim_evidence {ce} -> missing claim/evidence")
    for sc in s.get("section_claims", []):
        if sc["claim_id"] not in claims:
            issues.append(f"section_claims -> missing claim {sc['claim_id']}")
    status, sev = (HARD_FAIL, BLOCKING) if issues else (PASS, BLOCKING)
    return _result("ReferenceIntegrityGate", status, sev,
                   ["selected_source_items", "documents", "spans", "evidence", "claims"], issues)


def gate_span_offsets(s, contract):
    issues = []
    docs = {d["document_id"]: (d.get("normalized_text") or "") for d in s.get("documents", [])}
    for sp in s.get("spans", []):
        norm = docs.get(sp["document_id"], "")
        if norm[sp["start_char"]:sp["end_char"]] != sp["text"]:
            issues.append(f"span {sp['span_id']} offset mismatch against normalized_text")
    status, sev = (HARD_FAIL, BLOCKING) if issues else (PASS, BLOCKING)
    return _result("SpanOffsetGate", status, sev, ["spans", "documents"], issues)


def gate_claim_support(s, contract):
    supporting: dict[str, int] = {}
    for ce in s.get("claim_evidence", []):
        if ce["role"] == "supporting":
            supporting[ce["claim_id"]] = supporting.get(ce["claim_id"], 0) + 1
    issues = [f"accepted claim {c['claim_id']} has no supporting evidence"
              for c in s.get("claims", []) if c["status"] == "accepted" and supporting.get(c["claim_id"], 0) < 1]
    status, sev = (HARD_FAIL, BLOCKING) if issues else (PASS, BLOCKING)
    return _result("ClaimSupportGate", status, sev, ["claims", "claim_evidence"], issues)


def gate_critical_claim(s, contract):
    minimum = int(contract.get("critical_claim_policy", {}).get("minimum_supporting_evidence", 1))
    supporting: dict[str, int] = {}
    for ce in s.get("claim_evidence", []):
        if ce["role"] == "supporting":
            supporting[ce["claim_id"]] = supporting.get(ce["claim_id"], 0) + 1
    issues = [f"critical claim {c['claim_id']} has {supporting.get(c['claim_id'], 0)} < {minimum} supporting"
              for c in s.get("claims", [])
              if c["criticality"] == "critical" and supporting.get(c["claim_id"], 0) < minimum]
    status, sev = (HARD_FAIL, BLOCKING) if issues else (PASS, BLOCKING)
    return _result("CriticalClaimGate", status, sev, ["claims", "claim_evidence"],
                   issues, {"minimum_supporting_evidence": minimum})


def gate_citation_resolution(s, contract):
    spans = {sp["span_id"] for sp in s.get("spans", [])}
    docs = {d["document_id"] for d in s.get("documents", [])}
    items = {i["selected_item_id"] for i in s["selected_source_items"]}
    evidence = {e["evidence_id"]: e for e in s.get("evidence", [])}
    issues = []
    for c in s.get("citations", []):
        ev = evidence.get(c["evidence_id"])
        if not (ev is not None and c["span_id"] in spans
                and c["document_id"] in docs and c["selected_item_id"] in items):
            issues.append(f"citation {c['citation_id']} path does not resolve")
        elif (c["span_id"], c["document_id"], c["selected_item_id"]) != (
                ev["span_id"], ev["document_id"], ev["selected_item_id"]):
            issues.append(f"citation {c['citation_id']} path inconsistent with its evidence")
    status, sev = (HARD_FAIL, BLOCKING) if issues else (PASS, BLOCKING)
    return _result("CitationResolutionGate", status, sev, ["citations"], issues)


def gate_report_grounding(s, contract):
    accepted = {c["claim_id"] for c in s.get("claims", []) if c["status"] == "accepted"}
    citations = {c["citation_id"] for c in s.get("citations", [])}
    issues = [f"section references non-accepted claim {sc['claim_id']}"
              for sc in s.get("section_claims", []) if sc["claim_id"] not in accepted]
    issues += [f"section references missing citation {sc['citation_id']}"
               for sc in s.get("section_citations", []) if sc["citation_id"] not in citations]
    status, sev = (HARD_FAIL, BLOCKING) if issues else (PASS, BLOCKING)
    return _result("ReportGroundingGate", status, sev,
                   ["section_claims", "section_citations", "claims", "citations"], issues)


def gate_source_coverage(s, contract):
    minimum = int(contract.get("source_policy", {}).get("minimum_source_count", 1))
    containers = s["source_containers"]
    items = s["selected_source_items"]
    acquired = sum(1 for a in s.get("acquisition_attempts", []) if a["status"] == "succeeded")
    gaps = sum(1 for a in s.get("acquisition_attempts", []) if a["status"] != "succeeded")
    gaps += sum(1 for c in containers
                if not any(i["container_id"] == c["container_id"] for i in items))
    metrics = {"containers": len(containers), "items": len(items), "acquired": acquired, "gaps": gaps}
    if len(containers) < minimum:
        return _result("SourceCoverageGate", HARD_FAIL, BLOCKING, ["source_containers"],
                       [f"{len(containers)} sources < minimum_source_count {minimum}"], metrics)
    if len(items) > 0 and acquired == 0:
        return _result("SourceCoverageGate", HARD_FAIL, BLOCKING, ["source_containers"],
                       ["items supplied but zero documents acquired"], metrics)
    return _ok("SourceCoverageGate", BLOCKING, ["source_containers"], metrics)


def gate_source_set_limitations(s, contract):
    items = s["selected_source_items"]
    missing_meta = sum(1 for i in items if not i.get("published_at"))
    failed = sum(1 for a in s.get("acquisition_attempts", []) if a["status"] != "succeeded")
    issues = []
    if len(s["source_containers"]) < 3:
        issues.append("low source count for a robust evidence base")
    if missing_meta:
        issues.append(f"{missing_meta} items missing published_at")
    if failed:
        issues.append(f"{failed} acquisition attempts did not succeed")
    status = WARNING if issues else PASS
    return _result("SourceSetLimitationsGate", status, WARN, ["selected_source_items", "acquisition_attempts"],
                   issues, {"missing_metadata": missing_meta, "failed_attempts": failed})


_STOPWORDS = {
    "about", "above", "after", "also", "from", "have", "into", "most", "noted", "that",
    "their", "there", "these", "they", "this", "what", "when", "where", "which", "with",
}


def _keywords(value: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z0-9]+", value.lower()) if len(tok) > 3 and tok not in _STOPWORDS}


def gate_question_coverage(s, contract):
    sub_questions = [q for q in s.get("question_graph_nodes", []) if q.get("type") == "sub_question"]
    if not sub_questions:
        return _ok("QuestionCoverageGate", WARN, ["question_graph_nodes"], {"covered": [], "uncovered": []})
    accepted = {c["claim_id"]: c for c in s.get("claims", []) if c.get("status") == "accepted"}
    supporting_evidence_ids = {
        ce["evidence_id"] for ce in s.get("claim_evidence", []) if ce.get("claim_id") in accepted
    }
    corpus = []
    corpus.extend(c.get("claim_text", "") for c in accepted.values())
    corpus.extend(
        (e.get("summary", "") + " " + e.get("quoted_text", ""))
        for e in s.get("evidence", [])
        if not supporting_evidence_ids or e.get("evidence_id") in supporting_evidence_ids
    )
    corpus_keywords = set()
    for value in corpus:
        corpus_keywords.update(_keywords(value))

    covered = []
    uncovered = []
    for question in sub_questions:
        question_keywords = _keywords(question.get("text", ""))
        if question_keywords & corpus_keywords:
            covered.append(question["node_id"])
        else:
            uncovered.append(question["node_id"])
    metrics = {"covered": covered, "uncovered": uncovered}
    if uncovered:
        issues = [f"uncovered sub-question: {q['text']}" for q in sub_questions if q["node_id"] in uncovered]
        return _result("QuestionCoverageGate", WARNING, WARN, ["question_graph_nodes", "claims", "evidence"],
                       issues, metrics)
    return _ok("QuestionCoverageGate", WARN, ["question_graph_nodes", "claims", "evidence"], metrics)


GATES = [
    gate_required_rows, gate_schema_validation, gate_provider_quarantine,
    gate_reference_integrity, gate_span_offsets, gate_claim_support, gate_critical_claim,
    gate_citation_resolution, gate_report_grounding, gate_source_coverage, gate_source_set_limitations,
    gate_question_coverage,
]


def _snapshot(work: WorkStore) -> dict:
    tables = ["runs", "research_contracts", "question_graph_nodes", "physical_plan_nodes",
              "question_graph_edges", "physical_plan_edges", "source_containers", "selected_source_items",
              "acquisition_attempts", "spans", "evidence", "claims", "claim_evidence",
              "claim_edges", "citations", "report_sections", "section_claims", "section_citations"]
    snap = {t: work.read_rows(t) for t in tables}
    snap["documents"] = work.read_documents()
    return snap


class PreRenderQualityGateSuiteOperator(Operator):
    NAME = "PreRenderQualityGateSuite"
    INPUT_SCHEMAS = ["claims", "citations", "spans", "evidence", "report_sections"]
    OUTPUT_SCHEMAS = ["gate_results", "quality_dossier", "repair_tasks"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        snap = _snapshot(work)
        contract = (snap["research_contracts"] or [{}])[0]

        results, repair_tasks = [], []
        blocking_failures = warnings = 0
        for i, gate in enumerate(GATES):
            r = gate(snap, contract)
            r.update({
                "gate_result_id": ids.mint(ctx.run_id, "GATE", i),
                "run_id": ctx.run_id,
                "gate_version": GATE_VERSION,
                "created_at": ids.utc_now_iso(),
            })
            results.append(r)
            if r["severity"] == BLOCKING and r["status"] in (HARD_FAIL, "repairable_fail"):
                blocking_failures += 1
                repair_tasks.append({
                    "task_id": ids.mint(ctx.run_id, "REPAIR", len(repair_tasks)),
                    "run_id": ctx.run_id,
                    "gate_id": r["gate_id"],
                    "description": f"Resolve {r['gate_id']}: " + "; ".join(r["issues"][:3]),
                    "status": "planned_not_executed",
                })
            if r["status"] == WARNING:
                warnings += 1

        overall = "fail" if blocking_failures else ("warning" if warnings else "pass")
        approved = 0 if blocking_failures else 1
        coverage = next((r["metrics"] for r in results if r["gate_id"] == "QuestionCoverageGate"), {})
        work.write_rows("gate_results", results)
        work.write_rows("repair_tasks", repair_tasks)
        work.write_rows("quality_dossier", [{
            "run_id": ctx.run_id,
            "overall_status": overall,
            "blocking_gate_failures": blocking_failures,
            "warning_count": warnings,
            "approved_for_report_rendering": approved,
            "coverage": coverage,
        }])
        return {"gates": len(results), "blocking_failures": blocking_failures,
                "warnings": warnings, "approved": approved}
