"""Report compilation (design §13). A report is a deterministic view over the run's
artifacts — it never invents content. Findings render only from accepted claims and
their resolved citations; a blocked run renders a diagnostic report (coverage + gaps,
no findings) instead. A minimal Markdown->HTML renderer keeps the run dependency-free.
"""
from __future__ import annotations

import html as _html
import re

from .workfiles import WorkStore


class ReportData:
    """Everything the report needs, assembled from the working files."""

    def __init__(self, work: WorkStore):
        rows = work.read_rows
        self.run = (rows("runs") or [{}])[0]
        self.contract = (rows("research_contracts") or [{}])[0]
        self.dossier = (rows("quality_dossier") or [{}])[0]
        self.gate_results = rows("gate_results")
        self.repair_tasks = rows("repair_tasks")
        self.containers = rows("source_containers")
        self.items = rows("selected_source_items")
        self.attempts = rows("acquisition_attempts")
        self.documents = work.read_documents()
        self.spans = rows("spans")
        self.evidence = rows("evidence")
        self.claims = rows("claims")
        self.claim_evidence = rows("claim_evidence")
        self.citations = rows("citations")
        self.figures = rows("figures")
        self.approved = bool(self.dossier.get("approved_for_report_rendering"))

        self.by_item = {i["selected_item_id"]: i for i in self.items}
        self.by_doc = {d["document_id"]: d for d in self.documents}
        self.by_span = {s["span_id"]: s for s in self.spans}
        self.by_evidence = {e["evidence_id"]: e for e in self.evidence}
        self.by_claim = {c["claim_id"]: c for c in self.claims}
        self.cite_by_evidence = {c["evidence_id"]: c for c in self.citations}


def _summary(d: ReportData) -> list[str]:
    acquired = sum(1 for a in d.attempts if a["status"] == "succeeded")
    gaps = sum(1 for a in d.attempts if a["status"] != "succeeded")
    status = d.dossier.get("overall_status", "unknown")
    return [
        "## Executive Run Summary", "",
        f"Run `{d.run.get('run_id')}` processed {len(d.items)} supplied item(s) "
        f"({acquired} acquired, {gaps} gap(s)) across {len(d.containers)} container(s). "
        f"Gate status: **{status}**. No live retrieval (Phase 0).", "",
        f"- Topic: {d.run.get('topic')}",
        f"- Documents: {len(d.documents)} | Spans: {len(d.spans)} | "
        f"Evidence: {len(d.evidence)} | Claims: {len(d.claims)} | Citations: {len(d.citations)}", "",
    ]


def _coverage(d: ReportData) -> list[str]:
    out = ["## Source And Acquisition Coverage", "",
           "| Container | Type | Item | Acquisition |", "| --- | --- | --- | --- |"]
    status_by_item = {a["selected_item_id"]: a for a in d.attempts}
    for c in d.containers:
        items = [i for i in d.items if i["container_id"] == c["container_id"]]
        label = c.get("label") or c["container_id"]
        if not items:
            out.append(f"| {label} | {c['source_pack_type']} | _(no items — coverage gap)_ | — |")
        for i in items:
            att = status_by_item.get(i["selected_item_id"], {})
            verdict = att.get("status", "pending")
            if att.get("failure_code"):
                verdict += f" ({att['failure_code']})"
            out.append(f"| {label} | {c['source_pack_type']} | {i.get('title') or i['selected_item_id']} | {verdict} |")
    out.append("")
    return out


def _findings(d: ReportData) -> list[str]:
    out = ["## Evidence-Backed Findings", ""]
    accepted = [c for c in d.claims if c["status"] == "accepted"]
    if not accepted:
        out += ["_No accepted claims for this run._", ""]
        return out
    support = {}
    for ce in d.claim_evidence:
        support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])
    for c in accepted:
        labels = []
        for eid in support.get(c["claim_id"], []):
            cite = d.cite_by_evidence.get(eid)
            if cite:
                if cite.get("url"):
                    labels.append(f"[{cite['label']}]({cite['url']})")
                else:
                    labels.append(f"[{cite['label']}]")
        suffix = (" " + " ".join(labels)) if labels else ""
        out.append(f"- {c['claim_text']}{suffix}")
    out.append("")
    return out


def _evidence_table(d: ReportData) -> list[str]:
    out = ["## Evidence Table", "",
           "| Citation | Item | Evidence | Excerpt |", "| --- | --- | --- | --- |"]
    for ev in d.evidence:
        cite = d.cite_by_evidence.get(ev["evidence_id"])
        label = cite["label"] if cite else "—"
        item = d.by_item.get(ev["selected_item_id"], {})
        excerpt = ev["quoted_text"].strip().replace("\n", " ")
        if len(excerpt) > 160:
            excerpt = excerpt[:157] + "..."
        out.append(f"| {label} | {item.get('title') or ev['selected_item_id']} "
                   f"| {ev['evidence_type']} | {excerpt} |")
    out.append("")
    return out


def _figures(d: ReportData) -> list[str]:
    out = []
    for fig in d.figures:
        if fig.get("kind") != "comparison_matrix":
            continue
        spec = fig.get("spec") or {}
        columns = spec.get("columns") or []
        rows = spec.get("rows") or []
        if not columns or not rows:
            continue
        out += ["### Grounded Comparison Matrix", ""]
        out.append("| " + " | ".join(str(c) for c in columns) + " |")
        out.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in rows:
            out.append("| " + " | ".join("—" if value is None else str(value) for value in row) + " |")
        out.append("")
    return out


def _gaps(d: ReportData) -> list[str]:
    out = ["## Gaps And Repair Tasks", ""]
    failed = [a for a in d.attempts if a["status"] != "succeeded"]
    for a in failed:
        out.append(f"- Acquisition {a['status']} for `{a['selected_item_id']}`"
                   + (f": {a['failure_code']}" if a.get("failure_code") else ""))
    for g in d.gate_results:
        if g["status"] in ("warning", "repairable_fail", "hard_fail"):
            issues = "; ".join(g.get("issues") or []) or g["status"]
            out.append(f"- Gate {g['gate_id']} [{g['status']}]: {issues}")
    for t in d.repair_tasks:
        out.append(f"- Repair task `{t['task_id']}` ({t['status']}): {t['description']}")
    if len(out) == 2:
        out.append("_No gaps, failed acquisitions, or repair tasks._")
    out.append("")
    return out


def _traceability(d: ReportData) -> list[str]:
    out = ["## Traceability Appendix", "",
           "Each accepted claim traces to evidence, span, document, item, and container.", ""]
    container_of_item = {i["selected_item_id"]: i["container_id"] for i in d.items}
    support = {}
    for ce in d.claim_evidence:
        support.setdefault(ce["claim_id"], []).append(ce["evidence_id"])
    for c in d.claims:
        if c["status"] != "accepted":
            continue
        for eid in support.get(c["claim_id"], []):
            ev = d.by_evidence.get(eid, {})
            out.append(
                f"- `{c['claim_id']}` -> `{eid}` -> span `{ev.get('span_id')}` -> "
                f"doc `{ev.get('document_id')}` -> item `{ev.get('selected_item_id')}` -> "
                f"container `{container_of_item.get(ev.get('selected_item_id'))}`"
            )
    out.append("")
    return out


def _full_source_appendix(d: ReportData) -> list[str]:
    out = ["## Full Source Appendix", "",
           "Every supplied container and item, including unused seeds.", ""]
    for c in d.containers:
        out.append(f"- **{c.get('label') or c['container_id']}** ({c['source_pack_type']})"
                   + (f" — {c.get('container_locator')}" if c.get("container_locator") else ""))
        for i in [x for x in d.items if x["container_id"] == c["container_id"]]:
            out.append(f"  - {i.get('title') or i['selected_item_id']} (`{i['selected_item_id']}`)")
    out.append("")
    return out


def render_markdown(d: ReportData) -> tuple[str, str]:
    """Returns `(markdown, kind)`; kind is 'markdown_report' (final) or 'diagnostic_report'."""
    topic = d.run.get("topic", "")
    if d.approved:
        title = f"Phase 0 Evidence Report: {topic}"
        body = (_summary(d) + _coverage(d) + _findings(d) + _figures(d) + _evidence_table(d)
                + _gaps(d) + _traceability(d) + _full_source_appendix(d))
        kind = "markdown_report"
    else:
        title = f"Phase 0 Diagnostic Report: {topic}"
        body = (_summary(d)
                + ["> Rendering was blocked by a quality gate; findings are withheld.", ""]
                + _coverage(d) + _gaps(d) + _full_source_appendix(d))
        kind = "diagnostic_report"
    return "\n".join([f"# {title}", ""] + body), kind


def render_html(markdown: str, title: str) -> str:
    """Minimal, dependency-free Markdown->HTML for headings, tables, lists, and quotes."""
    lines = markdown.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("### "):
            out.append(f"<h3>{_html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{_html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{_html.escape(line[2:])}</h1>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{_html.escape(line[2:])}</blockquote>")
        elif line.startswith("|"):
            table, j = [], i
            while j < len(lines) and lines[j].startswith("|"):
                table.append(lines[j])
                j += 1
            out.append(_html_table(table))
            i = j
            continue
        elif line.strip().startswith("- ") or line.startswith("  - "):
            items, j = [], i
            while j < len(lines) and (lines[j].strip().startswith("- ")):
                items.append(lines[j].strip()[2:])
                j += 1
            out.append("<ul>" + "".join(f"<li>{_inline(it)}</li>" for it in items) + "</ul>")
            i = j
            continue
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
        i += 1
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{_html.escape(title)}</title></head><body>" + "".join(out) + "</body></html>")


def _html_table(rows: list[str]) -> str:
    def cells(r):
        return [c.strip() for c in r.strip().strip("|").split("|")]
    if len(rows) < 2:
        return ""
    header = cells(rows[0])
    body = [cells(r) for r in rows[2:]]  # row 1 is the markdown separator
    thead = "<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header) + "</tr>"
    tbody = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
    return f"<table border='1'>{thead}{tbody}</table>"


def _inline(s: str) -> str:
    s = _html.escape(s)
    s = s.replace("**", "")  # drop bold markers; minimal renderer
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s.replace("`", "")
