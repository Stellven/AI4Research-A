# AI4Research-A Architecture

## Goal

AI4Research-A converts a user-provided topic into a source-grounded research report through an incremental, inspectable pipeline:

```text
topic -> scoped brief -> research plan -> sources -> evidence -> claims -> reviewed report
```

The system should not behave like a single essay-writing prompt. It should preserve intermediate artifacts, validate model outputs, and make every final claim traceable to evidence.

## Design Principles

- Follow SDD: define contracts, interfaces, and acceptance criteria before implementation.
- Keep every phase runnable end to end, even when early phases use stubs or manual artifacts.
- Store major outputs as files so runs are easy to inspect, test, and debug.
- Treat sources, evidence, claims, reviews, and reports as separate artifacts.
- Gate later stages on validated artifacts instead of hidden model context.
- Add capability incrementally rather than building a fragile full agent at once.

## Target Pipeline

```text
User Topic
  -> Topic Scoper
  -> Planner
  -> Retriever
  -> Source Ingestor
  -> Evidence Extractor
  -> Claim Builder
  -> Report Writer
  -> Review Gate
  -> Final Renderer
```

## Core Components

### Orchestrator

Owns run creation, step ordering, artifact paths, validation, trace events, and revision loops.

### Agent Runtime

Provides a common interface for model-backed, manual, and stubbed agents. The orchestrator should call agents through this abstraction rather than directly depending on a specific model provider.

### Retrieval and Ingestion

Generates search queries, collects candidate sources, deduplicates results, and stores normalized source records with retrieval metadata.

### Evidence and Claims

Extracts evidence cards from sources and builds atomic claims linked to evidence IDs.

### Writer

Drafts reports from validated claims and evidence only. The writer should not invent uncited claims.

### Review Gate

Checks evidence coverage, citation readiness, unsupported claims, and report coherence. It returns `accept`, `revise`, or `reject`.

## Run Artifacts

Each run should use a file-first layout:

```text
runs/<run_id>/
  input/
    topic.json
    brief.md
  plan/
    task_graph.json
  retrieval/
    search_queries.json
    source_candidates.jsonl
  sources/
    source_index.json
    raw/
    normalized/
  evidence/
    evidence_cards.jsonl
  claims/
    claims_table.json
  writer/
    draft_report.md
    final_report.md
    source_map.json
    claim_traceability_table.json
  review/
    review_report.json
  trace/
    events.jsonl
```

## Phase 0: File-First Skeleton

### Objective

Establish the project structure, artifact contracts, and a deterministic demo pipeline.

### Deliverables

- CLI commands for run initialization, validation, demo execution, and rendering.
- File layout under `runs/<run_id>/`.
- Initial schemas for topic, brief, task graph, sources, evidence, claims, and review reports.
- Demo pipeline that produces a Markdown and HTML report without network access.
- Unit tests for artifact validation and run lifecycle.

### Acceptance Criteria

- A topic creates a valid run directory.
- Invalid artifacts fail validation.
- A demo run produces `final_report.md` and `final_report.html`.
- Tests pass without API keys or external services.

## Phase 1: Planning and Local Writing Loop

### Objective

Add LLM-assisted scoping, planning, writing, and review while still relying on local validated artifacts.

### Deliverables

- `AgentRuntime` abstraction with stub, manual, and model-backed implementations.
- Prompt templates for topic scoping, planning, writing, and reviewing.
- Structured-output parsing and retry-on-invalid-output behavior.
- Writer manifest that restricts the writer to validated claims and evidence.
- Review loop that can request revisions.

### Acceptance Criteria

- A topic generates a scoped brief and task graph.
- Model outputs are validated before downstream use.
- The writer cannot run without valid evidence and claims.
- Review feedback produces actionable revision instructions.

## Phase 2: Retrieval and Evidence Extraction

### Objective

Add real research capability through source retrieval, ingestion, evidence extraction, and claim construction.

### Deliverables

- Retrieval provider interface.
- Search query generation per task.
- Source candidate storage, deduplication, and ranking.
- Source ingestion for web pages and/or local files.
- Evidence card extraction.
- Claim table generation linked to evidence IDs.
- Fixture-based integration tests.

### Acceptance Criteria

- A topic produces source candidates for each planned task.
- Ingested sources include retrieval metadata.
- Evidence cards link to valid source IDs.
- Claims link to valid evidence IDs.
- Unsupported or weak claims are explicitly marked.

## Phase 3: Review, Revision, and Final Quality

### Objective

Make the pipeline robust enough for realistic research reports with quality gates, bounded revision, and human inspection.

### Deliverables

- Claim-level and citation-level review.
- Source coverage analysis against the original brief.
- Contradiction and uncertainty checks.
- Bounded revision loop.
- Human approval artifact.
- Final report bundle containing report, sources, evidence, claims, reviews, and trace log.
- CI for tests and linting.

### Acceptance Criteria

- Final rendering is blocked if key claims lack evidence.
- Missing citations and unsupported claims are identified.
- Revision stops after a configured maximum number of attempts.
- A human can trace each final claim back to sources and review decisions.

## Suggested Repository Structure

```text
AI4Research-A/
  ai4research/
    cli.py
    orchestrator.py
    artifacts.py
    validation.py
    runtime.py
    planning/
    retrieval/
    ingestion/
    evidence/
    writing/
    review/
  docs/
  prompts/
  schemas/
  tests/
  runs/
```

## Near-Term Build Order

1. Create the project skeleton.
2. Define artifact models and validation.
3. Implement run initialization.
4. Add the Phase 0 demo pipeline.
5. Add Markdown/HTML rendering.
6. Add tests.
7. Add the agent runtime abstraction.
8. Add planning prompts.
9. Add retrieval fixtures.
10. Add the first real retrieval provider.
