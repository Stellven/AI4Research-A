"""Render operators O13/O14 (design §8.5, §13). Compile the Markdown report (final when
the dossier approves rendering, diagnostic otherwise) and render it to HTML. Each export
is recorded as an `artifact_exports` row. Reports are views over artifacts — never new
content; they use only pre-gated sections, accepted claims, and resolved citations.
"""
from __future__ import annotations

from .. import ids, report
from ..runtime import RunContext
from ..workfiles import WorkStore
from .base import Operator


def _export_row(ctx: RunContext, path, kind: str, text: str, idx: int) -> dict:
    return {
        "export_id": ids.mint(ctx.run_id, "EXP", idx),
        "run_id": ctx.run_id,
        "path": str(path.relative_to(ctx.run_dir)),
        "kind": kind,
        "sha256": ids.sha256_text(text),
        "created_by_invocation_id": None,
        "created_at": ids.utc_now_iso(),
    }


class MarkdownReportCompileOperator(Operator):
    NAME = "MarkdownReportCompileOperator"
    INPUT_SCHEMAS = ["report_sections", "claims", "citations", "quality_dossier"]
    OUTPUT_SCHEMAS = ["artifact_exports"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        data = report.ReportData(work)
        markdown, kind = report.render_markdown(data)
        filename = "final_report.md" if data.approved else "diagnostic_report.md"
        path = ctx.exports_dir / filename
        path.write_text(markdown, encoding="utf-8")
        work.append_row("artifact_exports", _export_row(ctx, path, kind, markdown, 0))
        return {"report_kind": kind, "approved": int(data.approved), "bytes": len(markdown)}


class HtmlRenderOperator(Operator):
    NAME = "HtmlRenderOperator"
    INPUT_SCHEMAS = ["artifact_exports"]
    OUTPUT_SCHEMAS = ["artifact_exports"]

    def run(self, ctx: RunContext, work: WorkStore, pipeline: list[Operator]) -> dict:
        data = report.ReportData(work)
        md_name = "final_report.md" if data.approved else "diagnostic_report.md"
        markdown = (ctx.exports_dir / md_name).read_text(encoding="utf-8")
        title = markdown.split("\n", 1)[0].lstrip("# ").strip()
        html = report.render_html(markdown, title, report.build_view_model(data))
        path = ctx.exports_dir / md_name.replace(".md", ".html")
        path.write_text(html, encoding="utf-8")
        work.append_row("artifact_exports", _export_row(ctx, path, "html_report", html, 1))
        return {"approved": int(data.approved), "bytes": len(html)}
