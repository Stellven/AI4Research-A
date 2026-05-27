# Pipeline A Actual Architecture SDD

## 1. Purpose

This document defines the target architecture for the actual Pipeline A in AI4Research-A.

The primary baseline is `docs/architecture.md`, which defines Pipeline A at a high level:

```text
topic -> scoped research brief -> research plan -> source corpus -> evidence -> claims -> reviewed report
```

That baseline is correct, but too high-level for implementation. This SDD keeps the same product goal and applies the deeper architecture model we have been developing: typed operators, an inspectable optimizer, evidence ledger, claim graph, lightweight ontology, quality gates, and repair DAGs.

Pipeline A should not be a single agent that searches and writes a report. It should be a file-first, inspectable research pipeline that starts simple and grows into a general-purpose research compiler.

Target flow:

```text
user topic
  -> run initialization
  -> research contract
  -> domain/task classification
  -> question graph
  -> logical research plan
  -> physical operator plan
  -> source retrieval and ingestion
  -> span extraction
  -> evidence ledger
  -> claim graph
  -> report blueprint and section packets
  -> citation rendering
  -> quality gates
  -> repair DAGs
  -> Codex worker execution where useful
  -> final research bundle
```

## 2. Baseline Mapping

Pipeline A keeps the original `docs/architecture.md` flow, but deepens each stage.

| `docs/architecture.md` Stage | Pipeline A Actual Architecture |
| --- | --- |
| Topic Scoper | `ResearchContractOperator` |
| Planner | `QuestionGraphOperator`, `LogicalPlanOperator`, `Optimizer` |
| Retriever | `SourceFabric` and source-family operators |
| Source Ingestor | fetch, parse, normalize, and span operators |
| Evidence Extractor | `EvidenceOS`, span extraction, evidence ledger |
| Claim Builder | `ClaimCompiler`, claim graph, entailment checks |
| Report Writer | report blueprint, section contracts, section compiler |
| Review Gate | quality gate system and final closeout gate |
| Final Renderer | citation renderer, Markdown/HTML renderer, bundle exporter |

The key change is granularity. The old architecture names broad workflow steps. Pipeline A should implement those steps as typed, inspectable artifacts and operators.

## 3. Product Goal

Pipeline A should produce a research bundle, not only a prose report.

Minimum final bundle:

```text
final_report.md
final_report.html
research_contract.json
domain_classification.json
question_graph.json
physical_plan.json
source_index.jsonl
spans.jsonl
evidence_ledger.jsonl
claims.jsonl
claim_graph.json
citation_map.json
quality_dossier.json
repair_history.jsonl
operator_trace.jsonl
```

The user should be able to inspect:

- What topic was understood.
- What domain and research type were selected.
- What questions the system decided to answer.
- Which sources were retrieved or loaded.
- Which exact spans support each important claim.
- Which claims are contradicted, uncertain, or weak.
- Which quality gates passed or failed.
- Which repair actions were attempted.
- Why each final report section says what it says.

## 4. Design Principles

- Preserve the file-first architecture from `docs/architecture.md`.
- Treat sources, spans, evidence, claims, gates, and repairs as first-class artifacts.
- Use operators as the unit of execution.
- Keep optimizer decisions explicit and saved.
- Use LLMs inside bounded operators, not as the owner of the whole run.
- Render citations only from stored evidence spans.
- Block unsupported critical claims from final reports.
- Start with a lightweight ontology and evolve it by domain.
- Prefer repairable failure over silent degradation.
- Keep every phase runnable end to end.
- Treat Codex as a worker runtime behind adapters, not as the owner of Pipeline A state.

## 5. Core Concepts

### 5.1 Operator

An operator is a well-defined work unit.

```text
operator type = reusable capability
operator node = one invocation of that capability in a run
```

Examples:

```text
ResearchContractOperator
DomainClassifyOperator
QuestionGraphOperator
WebSearchOperator
FetchDocumentOperator
SpanExtractOperator
EvidenceExtractOperator
AtomicClaimExtractOperator
ClaimVerifyOperator
ContradictionSearchOperator
ReportBlueprintOperator
SectionDraftOperator
CitationRenderOperator
QualityGateOperator
RepairPlanOperator
```

Each operator declares:

```text
name
version
input schema
output schema
required artifacts
produced artifacts
allowed runtime
cost estimate
quality metrics
failure modes
retry policy
```

An operator may be deterministic, model-assisted, or LLM-heavy. The boundary is the contract, not whether it uses an LLM.

### 5.2 Optimizer

The optimizer turns a logical research need into a physical operator plan.

Early Pipeline A can use a rule-based optimizer. Later phases can make it score-based and adaptive.

Inputs:

- Research contract.
- Domain classification.
- Question graph.
- Available operators.
- Available source connectors.
- Budget and runtime limits.
- Existing artifacts.
- Gate failures.

Outputs:

- `physical_plan.json`
- Required source families.
- Required gates.
- Operator order and dependencies.
- Repair strategy.
- Stop criteria.

The optimizer should not be hidden chain-of-thought. It should be code-defined, inspectable, and persisted. An LLM can help propose a plan, but the final plan must validate against schemas and rules.

### 5.3 LLM Placement

LLMs are used inside selected operators.

Good LLM uses:

- Scoping ambiguous topics.
- Creating question graphs.
- Extracting entities and claims.
- Summarizing evidence.
- Generating contradiction queries.
- Drafting report sections from verified section packets.

Bad LLM uses:

- Inventing citations.
- Owning run state.
- Secretly changing the research contract.
- Bypassing gates.
- Introducing unsupported facts directly into final prose.

Pipeline A rule:

```text
LLM proposes.
Schemas constrain.
Code validates.
Gates decide.
Artifacts preserve.
```

### 5.4 Codex Placement

Codex belongs in the runtime/execution layer, not the core data model.

Pipeline A should use a Python runner as the stateful spine:

```text
Python runner
  owns run state, physical plans, artifacts, validation, gates, repair, and rendering

Codex worker runtime
  executes bounded operator nodes that need tool use, multi-step reasoning, or synthesis
```

The detailed Codex execution model is defined separately in `docs/pipeline_a_codex_execution_sdd.md`. The main Pipeline A architecture only needs the boundary:

```text
OperatorInvocation
  -> RuntimeAdapter
  -> Codex worker, local Python function, direct LLM, manual step, or stub
  -> validated artifacts
```

Codex should receive bounded work packets and return artifacts. It should not receive an unrestricted instruction to "research the topic" or "write the whole report" outside the operator plan.

### 5.5 Evidence Ledger

The evidence ledger is the factual source of truth.

It stores:

```text
source -> document -> span -> evidence -> linked claims
```

Minimum evidence record:

```json
{
  "evidence_id": "E001",
  "source_id": "S001",
  "document_id": "D001",
  "span_id": "SP001",
  "evidence_type": "policy | paper | product_doc | benchmark | news | quote | vendor_claim | standard",
  "summary": "Short normalized evidence statement.",
  "published_at": "2026-03-01",
  "accessed_at": "2026-05-27T10:00:00-04:00",
  "source_quality_score": 0.82,
  "limitations": "Vendor claim; should be corroborated."
}
```

The writer may cite only evidence that exists in the ledger.

### 5.6 Claim Graph

The claim graph is the system's belief map.

It stores atomic claims and relationships:

```text
supports
refutes
qualifies
contradicts
depends_on
duplicates
updates
belongs_to_section
```

Minimum claim record:

```json
{
  "claim_id": "C001",
  "claim_type": "trend_claim",
  "text": "Skills governance platforms are increasingly incorporating AI-based skill inference.",
  "supporting_evidence_ids": ["E001", "E014"],
  "contradicting_evidence_ids": [],
  "qualifying_evidence_ids": [],
  "verification_status": "verified",
  "confidence": 0.78
}
```

The report compiler should consume verified or explicitly qualified claims. It should not create new critical claims during prose generation.

### 5.7 Lightweight Ontology

Pipeline A needs an ontology, but not a giant one on day one.

Start with:

- General entity types.
- General claim types.
- Domain profile vocabulary.
- Synonyms and aliases.
- Simple relation labels.

Generic entity types:

```text
Technology
Organization
Person
Product
Policy
Standard
Paper
Dataset
Benchmark
Event
Trend
Risk
UseCase
```

For a skills-governance topic, the domain profile may add:

```text
Skill
SkillTaxonomy
SkillOntology
CompetencyFramework
Credential
VerifiableCredential
LearningEmploymentRecord
TalentMarketplace
SkillInferenceModel
GovernancePolicy
HRSystem
Vendor
```

The ontology helps with entity resolution and report structure. It should not block useful research if a domain concept is missing.

### 5.8 Quality Gates

Quality gates are blocking checkpoints.

Minimum gates:

```text
SchemaValidationGate
EvidenceReferenceGate
ClaimSupportGate
CitationSpanGate
FreshnessGate
SourceDiversityGate
QuestionCoverageGate
ContradictionCoverageGate
ReportCompletenessGate
FinalCloseoutGate
```

Gate result:

```json
{
  "gate_id": "ClaimSupportGate",
  "verdict": "repairable_fail",
  "issues": [
    {
      "severity": "P0",
      "claim_id": "C014",
      "problem": "Critical claim has no direct supporting evidence.",
      "repair_hint": "Run targeted evidence search or downgrade/remove claim."
    }
  ]
}
```

### 5.9 Repair DAG

Repair should be explicit.

Example:

```text
ClaimSupportGate fails
  -> TargetedEvidenceSearchOperator
  -> EvidenceExtractOperator
  -> ClaimVerifyOperator
  -> SectionRewriteOperator
  -> rerun ClaimSupportGate
```

Repair actions should be persisted in `repair/repair_dags.jsonl` and `repair/repair_actions.jsonl`.

## 6. Main Components

### 6.1 CLI Layer

Initial commands:

```text
ai4research init --topic "..."
ai4research contract --run runs/<run_id>
ai4research plan --run runs/<run_id>
ai4research execute --run runs/<run_id>
ai4research gate --run runs/<run_id>
ai4research repair --run runs/<run_id>
ai4research render --run runs/<run_id>
ai4research demo --topic "..."
```

### 6.2 Orchestrator

Responsibilities:

- Create run directories.
- Load and validate artifacts.
- Dispatch operator nodes.
- Persist inputs and outputs.
- Record traces.
- Invoke gates.
- Trigger repair planner when gates fail.
- Prevent invalid artifacts from reaching downstream operators.

### 6.3 Artifact Store

File-backed storage for:

- JSON and JSONL artifacts.
- Raw and normalized source documents.
- Trace logs.
- Gate results.
- Repair history.
- Final report bundle.

### 6.4 Runtime Layer

Initial runtimes:

```text
StubRuntime
ManualRuntime
LLMRuntime
```

Operators call the runtime instead of directly calling model providers.

### 6.5 Operator Registry

Stores operator specs, versions, schemas, and implementation bindings.

### 6.6 Source Fabric

Unifies source connectors.

Initial source families:

```text
fixture_sources
manual_sources
local_files
web_search
official_docs
company_blog
academic_search
policy_and_standards
```

Later:

```text
github
conference_transcripts
benchmark_sources
news_events
regulatory_filings
private_kb
mcp_tools
```

### 6.7 Evidence OS

Owns source records, documents, spans, evidence entries, source trust profiles, and citation maps.

### 6.8 Claim Compiler

Pipeline:

```text
span
  -> evidence
  -> candidate claim
  -> atomic claim
  -> ontology mapping
  -> evidence alignment
  -> verification
  -> contradiction check
  -> claim graph insert
```

### 6.9 Report Compiler

Consumes:

- Research contract.
- Question graph.
- Report blueprint.
- Section contracts.
- Evidence packets.
- Verified claims.
- Citation map.
- Figure specs.

The report compiler may synthesize prose, but not facts.

## 7. Execution Flow

```text
1. User submits topic.
2. Orchestrator creates run directory.
3. ResearchContractOperator creates contract.
4. DomainClassifyOperator selects domain and source strategy.
5. QuestionGraphOperator creates question graph.
6. LogicalPlanOperator creates logical plan.
7. Optimizer creates physical operator plan.
8. Source operators retrieve or load sources.
9. Ingestion operators normalize documents.
10. SpanExtractOperator creates spans.
11. EvidenceExtractOperator creates evidence ledger entries.
12. Claim operators create, verify, and connect claims.
13. Analysis operators produce trends, risks, comparisons, and insights.
14. ReportBlueprintOperator creates blueprint and section contracts.
15. ReportCompiler drafts sections from evidence packets.
16. CitationRenderOperator renders citations from evidence spans.
17. Quality gates run.
18. Repair DAGs run if needed.
19. FinalCloseoutGate approves or blocks finalization.
20. Phase 4 runtime adapters may dispatch selected operator nodes to Codex workers.
21. Renderer exports final bundle.
```

## 8. Example: Skills Governance

Input:

```text
latest technologies in skills governance
```

Expected domain classification:

```json
{
  "domain": "skills_governance",
  "research_type": "technology_trend_analysis",
  "freshness_required": true,
  "source_families": [
    "official_docs",
    "company_blog",
    "policy_and_standards",
    "academic_search",
    "analyst_or_market_sources"
  ],
  "skip_families": [
    "code_benchmark",
    "hardware_benchmark"
  ]
}
```

Question graph:

```text
Q1: What does skills governance mean in enterprise talent systems?
Q2: Which technologies currently enable skills governance?
Q3: How are AI-based skill inference systems used?
Q4: What governance risks exist around inferred skills?
Q5: What standards, taxonomies, or interoperability efforts matter?
Q6: Which vendors or platforms have recent product movement?
Q7: What should technical decision makers watch next?
```

Selected operators:

```text
QueryExpansionOperator
CompanyDocsSearchOperator
PolicyStandardsSearchOperator
AcademicSearchOperator
DocumentFetchOperator
SpanExtractOperator
EvidenceExtractOperator
OntologyMapOperator
AtomicClaimExtractOperator
ClaimVerifyOperator
ContradictionSearchOperator
TrendMineOperator
RiskMineOperator
ReportBlueprintOperator
CitationRenderOperator
FinalCloseoutGateOperator
```

## 9. Artifact Layout

```text
runs/<run_id>/
  input/
    topic.json
  contract/
    research_contract.json
    domain_classification.json
    ontology_profile.json
  questions/
    question_graph.json
  plans/
    logical_plan.json
    physical_plan.json
    optimizer_decisions.jsonl
  trace/
    operator_trace.jsonl
    events.jsonl
  sources/
    source_index.jsonl
    documents/
      raw/
      normalized/
    spans.jsonl
  evidence/
    evidence_ledger.jsonl
    source_trust_profiles.jsonl
  claims/
    claims.jsonl
    claim_graph.json
    claim_evidence_edges.jsonl
    contradictions.jsonl
  analysis/
    trends.jsonl
    risks.jsonl
    comparisons.jsonl
    insights.jsonl
  report/
    blueprint.json
    section_contracts.jsonl
    drafts/
    final_report.md
    final_report.html
    citation_map.json
  quality/
    gate_results.jsonl
    quality_dossier.json
  repair/
    repair_dags.jsonl
    repair_actions.jsonl
  bundle/
    manifest.json
```

## 10. Phase 0: Pipeline A Skeleton

### Objective

Build a runnable local pipeline with artifact contracts, fixture sources, evidence records, claims, gates, and final report rendering.

### Operators

```text
ResearchContractOperator
FixtureSourceLoadOperator
SpanExtractOperator
EvidenceExtractOperator
AtomicClaimBuildOperator
ClaimSupportGateOperator
MarkdownReportCompileOperator
HtmlRenderOperator
```

### Optimizer

Static plan:

```text
contract -> fixtures -> spans -> evidence -> claims -> gates -> report -> render
```

The static optimizer still writes `plans/physical_plan.json` so later phases can replace it without changing the executor.

### Deliverables

- Run directory creation.
- Artifact schemas.
- Fixture source ingestion.
- Evidence ledger from fixture text.
- Claim graph from fixture evidence.
- Basic quality dossier.
- Final Markdown and HTML report.

### Acceptance Criteria

- `ai4research demo --topic "..."` creates a run.
- Every claim references existing evidence.
- Every evidence item references an existing span.
- Report generation fails if a critical claim lacks evidence.
- Demo succeeds without network or API keys.

## 11. Phase 1: Operators, Question Graph, Rule Optimizer

### Objective

Make Pipeline A modular and task-aware.

### Operators

```text
DomainClassifyOperator
QuestionGraphOperator
LogicalPlanOperator
RuleOptimizerOperator
ManualSourceIngestOperator
DocumentNormalizeOperator
ClaimNormalizeOperator
CitationRenderOperator
RepairPlanOperator
```

### Optimizer Rules

```text
if topic asks "latest" or "recent":
  freshness_required = true
  add FreshnessGate

if research_type == technology_trend_analysis:
  add TrendMineOperator
  require recent source families
  require risk and recommendation sections

if domain == skills_governance:
  prefer policy, standards, vendor docs, academic, and analyst sources

if domain == ai_infrastructure:
  prefer papers, docs, GitHub, releases, benchmarks, and conference sources
```

### Acceptance Criteria

- Two different topics produce different physical plans.
- Question graph is generated and saved.
- Operators record cost and metrics.
- Gate failures create repair DAGs.
- Report compiler uses section contracts.

## 12. Phase 2: Retrieval And Claim Verification

### Objective

Add real source retrieval, source ingestion, ontology mapping, span-level citations, and claim verification.

### Operators

```text
WebSearchOperator
AcademicSearchOperator
PolicySearchOperator
CompanyDocsSearchOperator
FetchDocumentOperator
HTMLNormalizeOperator
PDFParseOperator
EntityResolveOperator
OntologyMapOperator
AtomicClaimExtractOperator
ClaimEntailmentOperator
ContradictionSearchOperator
ClaimConfidenceUpdateOperator
CitationSpanResolverOperator
```

### Optimizer

Score-based source/operator choice:

```text
score =
  expected_information_gain
  + freshness_gain
  + source_authority
  + source_diversity
  + contradiction_value
  + domain_fit
  - estimated_cost
  - duplication_risk
```

Adaptive additions:

```text
if recent_source_count < threshold:
  add FreshnessBackfillOperator

if source_diversity < threshold:
  add SourceFamilyBackfillOperator

if unsupported_critical_claim_count > 0:
  add TargetedEvidenceSearchOperator

if contradiction_coverage < threshold:
  add ContradictionSearchOperator
```

### Acceptance Criteria

- Retrieval produces source candidates.
- Ingested documents produce stable spans.
- Evidence references real spans.
- Critical claims require direct evidence.
- Citations resolve to spans.
- Contradictions are stored explicitly.
- Optimizer adds at least one backfill operator based on measured gaps.

## 13. Phase 3: Report Compiler And Adaptive Repair

### Objective

Make Pipeline A strong enough for realistic long-form research tasks with verified report generation, bounded repair, and final closeout.

### Operators

```text
ReportBlueprintOperator
SectionContractOperator
SectionEvidencePacketOperator
ArgumentGraphOperator
SectionDraftOperator
CrossSectionCoherenceOperator
FigureSpecOperator
FigureGroundingGateOperator
FinalCloseoutGateOperator
RepairDAGOptimizerOperator
RepairDAGExecutorOperator
BundleExportOperator
```

### Optimizer

Adaptive full-run optimizer:

```text
build initial physical plan
execute source and evidence batches
measure coverage and support
add targeted operators for gaps
stop when marginal information gain is low and required gates pass
generate repair DAGs for repairable failures
block finalization on hard failures
```

### Final Closeout Gates

```text
EvidenceLedgerGate
ClaimSupportGate
CitationSpanGate
EntailmentGate
FreshnessGate
SourceDiversityGate
ContradictionCoverageGate
QuestionCoverageGate
ReportCompletenessGate
FigureGroundingGate
FinalCloseoutGate
```

### Acceptance Criteria

- Final report is blocked unless final closeout passes.
- Repair DAGs execute up to a configured maximum.
- The quality dossier shows all gate verdicts and repair attempts.
- Every critical claim in the report traces to claim and evidence IDs.
- The final bundle includes report, evidence ledger, claim graph, citation map, quality dossier, and trace.
- At least three representative topics produce different optimized plans.

## 14. Phase 4: Codex Multi-Agent Runtime And Advanced Execution

### Objective

Make Pipeline A scalable and agentic by adding Codex as a worker runtime for selected operator nodes, while keeping Python-owned artifacts, plans, validation, gates, repair, and rendering as the source of truth.

Phase 4 should not replace Pipeline A with a vague multi-agent system. It should make Codex an execution substrate for bounded operators.

Detailed execution design lives in `docs/pipeline_a_codex_execution_sdd.md`.

### Runtime Adapters

Pipeline A should support a runtime adapter boundary:

```text
LocalPythonRuntime
StubRuntime
ManualRuntime
LLMRuntime
CodexExecRuntime
CodexMCPRuntime
```

Operators declare allowed runtimes. The optimizer or orchestrator selects one based on operator type, cost, available tools, and reliability requirements.

### Codex-Suitable Operators

Codex is most useful for operators that need multi-step reasoning, repository/file work, tool use, or synthesis:

```text
QuestionGraphOperator
SourceStrategyOperator
EvidenceExtractOperator
AtomicClaimExtractOperator
ContradictionSearchOperator
ReportSectionDraftOperator
RepairPlanOperator
FigureSpecOperator
```

Codex should not be used for deterministic mechanics unless there is a clear reason:

```text
SchemaValidationOperator
CitationRenderOperator
ArtifactPathOperator
GateAggregationOperator
HtmlRenderOperator
```

### Work Packet Contract

Every Codex worker invocation should receive a bounded work packet:

```json
{
  "operator_name": "ClaimMineOperator",
  "run_id": "R001",
  "input_artifacts": ["evidence/evidence_ledger.jsonl"],
  "output_artifacts": ["claims/claims.jsonl"],
  "schema": "AtomicClaim[]",
  "skill": "claim-mining",
  "allowed_tools": ["read_files", "web_search"],
  "constraints": [
    "do not write outside this run directory",
    "do not invent citations",
    "all claims must reference evidence IDs"
  ]
}
```

### Phase 4 Capabilities

- Execute at least one operator through Codex.
- Package repeatable Codex behaviors as skills.
- Support Codex headless/exec or MCP-backed execution behind runtime adapters.
- Run safe parallel operator batches.
- Enforce output artifact schemas.
- Detect unexpected file writes.
- Record Codex runtime logs, tool usage, model usage, and costs.
- Merge worker outputs through deterministic merge operators.

### Acceptance Criteria

- Python runner can dispatch a bounded operator invocation to a Codex runtime adapter.
- Codex writes only declared output artifacts for that invocation.
- Python validates Codex-produced artifacts before downstream use.
- Failed Codex invocations produce structured operator failures.
- Independent Codex worker tasks can run in parallel without corrupting shared artifacts.
- Runtime traces show work packet, input artifacts, output artifacts, status, cost, and logs.
- The same operator can fall back to `ManualRuntime`, `LLMRuntime`, or `LocalPythonRuntime` where appropriate.

## 15. Suggested Repository Structure

```text
AI4Research-A/
  ai4research/
    __init__.py
    cli.py
    orchestrator.py
    artifacts.py
    config.py
    schemas.py
    tracing.py
    runtime.py

    optimizer/
      rules.py
      scoring.py
      planner.py

    operators/
      base.py
      registry.py
      contract.py
      classify.py
      question_graph.py
      retrieval.py
      ingestion.py
      evidence.py
      claims.py
      analysis.py
      report.py
      gates.py
      repair.py

    retrieval/
      providers.py
      web_search.py
      academic.py
      ranking.py

    ingestion/
      fetch.py
      html.py
      pdf.py
      normalize.py

    evidence/
      ledger.py
      spans.py
      extractor.py

    claims/
      compiler.py
      verifier.py
      graph.py
      contradictions.py

    ontology/
      base_profile.py
      domain_profiles.py
      entity_resolver.py

    report/
      blueprint.py
      section_contracts.py
      compiler.py
      renderer.py

    quality/
      gates.py
      dossier.py
      closeout.py

    repair/
      planner.py
      executor.py

    runtimes/
      base.py
      local_python.py
      stub.py
      manual.py
      llm.py
      codex_exec.py
      codex_mcp.py

    skills/
      question_graph/
      evidence_extraction/
      claim_mining/
      contradiction_search/
      report_section_drafting/
      repair_planning/

  docs/
  tests/
  runs/
  fixtures/
```

## 16. Testing Strategy

### Unit Tests

- Schema validation.
- Artifact path resolution.
- Operator input/output contracts.
- Claim-to-evidence integrity.
- Evidence-to-span integrity.
- Gate verdict behavior.
- Optimizer rule selection.

### Integration Tests

- Phase 0 demo run.
- Two-topic optimizer plan difference.
- Fixture retrieval and ingestion.
- Claim graph construction.
- Report compilation from verified claims.
- Gate failure to repair DAG creation.
- Codex work packet validation with a stub runtime.
- Runtime fallback behavior.

### Regression Topics

Use at least:

```text
latest technologies in skills governance
compare vLLM and SGLang for inference serving
recent policy trends in semiconductor export controls
state of open-source agent frameworks
```

Each topic should validate that Pipeline A selects a different source strategy and report structure.

## 17. Immediate Next Build Steps

1. Create `ai4research/` package skeleton.
2. Add artifact path manager.
3. Add schemas for contract, span, evidence, claim, gate result, and repair DAG.
4. Add operator base class and registry.
5. Add Phase 0 fixture demo operators.
6. Add static optimizer that writes `physical_plan.json`.
7. Add basic gates.
8. Add Markdown and HTML renderers.
9. Add tests for artifact integrity.
10. Add Phase 1 domain classifier and question graph.
11. Add runtime adapter base interface.
12. Add `StubRuntime`, `LocalPythonRuntime`, and `ManualRuntime`.
13. Add Codex execution SDD-driven work packet schema.
