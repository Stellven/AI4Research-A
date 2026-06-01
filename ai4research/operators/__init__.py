"""Operator package: the Phase 0 spine pipeline + runner.

The runner executes the plan O0-O14 + the pre-render gate suite (G). Persist, closeout,
and bundle (O15/F/B) run afterward as the finalization driver (see ai4research.finalize).

  O0 RunInitialize -> O1 ResearchContract -> O2 QuestionGraphStub -> O3 StaticPlan
  -> O4 SourceContainerLoad -> O5 SourceItemSelect -> O6 DocumentAcquisition
  -> O7 DocumentNormalize -> O8 SpanSegment -> O9 EvidenceCardBuild -> O10 ClaimLiteBuild
  -> O11 CitationMapBuild -> O12 ReportBlueprint -> G PreRenderQualityGateSuite
  -> O13 MarkdownReportCompile -> O14 HtmlRender
"""
from __future__ import annotations

from .base import Operator, RunFailed
from .core import (
    QuestionGraphStubOperator,
    ResearchContractOperator,
    RunInitializeOperator,
    StaticPlanOperator,
)
from .extraction import (
    CitationMapBuildOperator,
    ClaimLiteBuildOperator,
    DocumentNormalizeOperator,
    EvidenceCardBuildOperator,
    ReportBlueprintOperator,
    SpanSegmentOperator,
)
from .gates import PreRenderQualityGateSuiteOperator
from .render import HtmlRenderOperator, MarkdownReportCompileOperator
from .runner import OperatorRunner
from .sources import (
    DocumentAcquisitionOperator,
    SourceContainerLoadOperator,
    SourceItemSelectOperator,
)


def build_pipeline() -> list[Operator]:
    return [
        RunInitializeOperator(),
        ResearchContractOperator(),
        QuestionGraphStubOperator(),
        StaticPlanOperator(),
        SourceContainerLoadOperator(),
        SourceItemSelectOperator(),
        DocumentAcquisitionOperator(),
        DocumentNormalizeOperator(),
        SpanSegmentOperator(),
        EvidenceCardBuildOperator(),
        ClaimLiteBuildOperator(),
        CitationMapBuildOperator(),
        ReportBlueprintOperator(),
        PreRenderQualityGateSuiteOperator(),
        MarkdownReportCompileOperator(),
        HtmlRenderOperator(),
    ]


__all__ = ["Operator", "RunFailed", "OperatorRunner", "build_pipeline"]
