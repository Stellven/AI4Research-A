-- Phase 0 SQLite schema. Generated from docs/pipeline_a_phase0_system_design.md (do not edit by hand).
PRAGMA foreign_keys = ON;

CREATE TABLE schema_meta (
  key            TEXT PRIMARY KEY,        -- e.g. 'schema_version'
  value          TEXT NOT NULL
);

CREATE TABLE runs (
  run_id         TEXT PRIMARY KEY,
  topic          TEXT NOT NULL,
  phase          TEXT NOT NULL DEFAULT 'phase0',
  status         TEXT NOT NULL,           -- initialized | running | finalized | diagnostic_only | failed
  started_at     TEXT NOT NULL,
  completed_at   TEXT,
  root_dir       TEXT NOT NULL
);

CREATE TABLE operator_specs (             -- the operator registry
  operator_name  TEXT PRIMARY KEY,
  version        TEXT NOT NULL,
  input_schemas  TEXT NOT NULL,           -- JSON array
  output_schemas TEXT NOT NULL,           -- JSON array
  runtime        TEXT NOT NULL DEFAULT 'local_python'
);

CREATE TABLE physical_plan_nodes (        -- the static plan's steps
  node_id        TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  operator_name  TEXT NOT NULL REFERENCES operator_specs(operator_name),
  runtime        TEXT NOT NULL DEFAULT 'local_python',
  order_index    INTEGER NOT NULL
);

CREATE TABLE physical_plan_edges (        -- which step feeds which
  run_id    TEXT NOT NULL REFERENCES runs(run_id),
  from_node TEXT NOT NULL REFERENCES physical_plan_nodes(node_id),
  to_node   TEXT NOT NULL REFERENCES physical_plan_nodes(node_id),
  artifact  TEXT                          -- the table dependency this edge carries
);

CREATE TABLE optimizer_decisions (        -- why the static plan looks the way it does
  decision_id             TEXT PRIMARY KEY,
  run_id                  TEXT NOT NULL REFERENCES runs(run_id),
  reason                  TEXT NOT NULL,
  alternatives_considered TEXT            -- JSON array; empty in Phase 0
);

CREATE TABLE operator_invocations (       -- provenance + trace, one row per operator run
  invocation_id  TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  node_id        TEXT REFERENCES physical_plan_nodes(node_id),
  operator_name  TEXT NOT NULL REFERENCES operator_specs(operator_name),
  operator_version TEXT NOT NULL,
  runtime        TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  completed_at   TEXT,
  status         TEXT NOT NULL,           -- success | failed | skipped
  metrics        TEXT,                    -- JSON
  error          TEXT
);

CREATE TABLE artifact_exports (           -- ONLY files written to disk
  export_id      TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  path           TEXT NOT NULL,           -- relative to run dir
  kind           TEXT NOT NULL,           -- markdown_report | html_report | diagnostic_report | bundle_manifest | json_export
  sha256         TEXT NOT NULL,
  created_by_invocation_id TEXT REFERENCES operator_invocations(invocation_id),
  created_at     TEXT NOT NULL
);

CREATE TABLE domain_packs (
  pack_id TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  source_families TEXT NOT NULL,
  freshness_window_days INTEGER,
  max_items_per_container INTEGER,
  vocabulary TEXT NOT NULL,
  scoring_weights TEXT NOT NULL,
  required_sections TEXT NOT NULL,
  required_gates TEXT NOT NULL,
  question_template TEXT NOT NULL
);

CREATE TABLE research_contracts (
  contract_id    TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  domain_pack_id TEXT REFERENCES domain_packs(pack_id),
  topic          TEXT NOT NULL,
  research_type  TEXT NOT NULL,           -- 'source_pack_evidence_report'
  audience       TEXT,
  freshness_required INTEGER NOT NULL DEFAULT 0,
  freshness_window_days INTEGER,
  source_policy  TEXT NOT NULL,           -- JSON: allowed_source_pack_types, allowed_source_adapters,
                                          --       minimum_source_count, expected_source_count (nullable), exact_source_list_required
  required_dimensions TEXT NOT NULL,      -- JSON array
  critical_claim_policy TEXT NOT NULL,    -- JSON: minimum_supporting_evidence, allow_unsupported_critical_claims=false
  deliverables   TEXT NOT NULL            -- JSON array
);

CREATE TABLE question_graph_nodes (
  node_id TEXT PRIMARY KEY,
  run_id  TEXT NOT NULL REFERENCES runs(run_id),
  type    TEXT NOT NULL,                  -- 'root_question' in Phase 0
  text    TEXT NOT NULL,
  status  TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE question_graph_edges (
  run_id  TEXT NOT NULL REFERENCES runs(run_id),
  from_node TEXT NOT NULL REFERENCES question_graph_nodes(node_id),
  to_node   TEXT NOT NULL REFERENCES question_graph_nodes(node_id),
  type    TEXT NOT NULL
);

CREATE TABLE source_containers (
  container_id     TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  source_pack_type TEXT NOT NULL,         -- 'local_document_set' | 'youtube_channel' | 'github_repo'
  container_locator TEXT,                 -- channel URL / repo URL / folder path
  label            TEXT,
  container_rank   INTEGER,               -- preserves user order
  user_supplied    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE selected_source_items (
  selected_item_id   TEXT PRIMARY KEY,
  run_id             TEXT NOT NULL REFERENCES runs(run_id),
  container_id       TEXT NOT NULL REFERENCES source_containers(container_id),
  source_family      TEXT NOT NULL,       -- generic routing key: 'local_document'|'youtube_video'|'github_file'
  adapter_id         TEXT NOT NULL,
  item_locator       TEXT NOT NULL,       -- JSON: url / local_path / local_fixture_path / inline_text
  title              TEXT,
  creator            TEXT,
  published_at       TEXT,
  accessed_at        TEXT,
  source_rank        INTEGER,
  selection_reason   TEXT,                -- 'Phase 0 include_all policy'
  acquisition_status TEXT NOT NULL DEFAULT 'pending',
  provider_metadata  TEXT                 -- JSON: youtube_handle, video_id, github_path, sha, ... (engine never reads)
);

CREATE TABLE acquisition_attempts (
  attempt_id       TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  selected_item_id TEXT NOT NULL REFERENCES selected_source_items(selected_item_id),
  adapter_id       TEXT NOT NULL,
  attempt_number   INTEGER NOT NULL DEFAULT 1,
  started_at       TEXT NOT NULL,
  completed_at     TEXT,
  status           TEXT NOT NULL,         -- succeeded | failed | skipped
  input_locator    TEXT,                  -- JSON
  failure_code     TEXT,                  -- e.g. 'local_fixture_missing'
  failure_message  TEXT,
  retryable        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE documents (
  document_id          TEXT PRIMARY KEY,
  run_id               TEXT NOT NULL REFERENCES runs(run_id),
  selected_item_id     TEXT NOT NULL REFERENCES selected_source_items(selected_item_id),
  acquisition_attempt_id TEXT NOT NULL REFERENCES acquisition_attempts(attempt_id),
  document_kind        TEXT NOT NULL,     -- 'youtube_transcript' | 'github_document' | 'local_document'
  title                TEXT,
  raw_text             TEXT NOT NULL,
  normalized_text      TEXT,              -- set by O7; spans index THIS, never raw_text
  language             TEXT,
  published_at         TEXT,
  content_hash         TEXT NOT NULL,
  source_quality_score REAL,
  normalization        TEXT,              -- JSON: rules_applied, removed_content
  provider_metadata    TEXT               -- JSON: video_id, repo sha, ...
);

CREATE TABLE spans (
  span_id            TEXT PRIMARY KEY,
  run_id             TEXT NOT NULL REFERENCES runs(run_id),
  document_id        TEXT NOT NULL REFERENCES documents(document_id),
  selected_item_id   TEXT NOT NULL REFERENCES selected_source_items(selected_item_id),
  span_index         INTEGER NOT NULL,
  start_char         INTEGER NOT NULL,
  end_char           INTEGER NOT NULL,
  text               TEXT NOT NULL,
  segmentation_strategy TEXT NOT NULL,
  text_hash          TEXT NOT NULL
);

CREATE TABLE evidence (
  evidence_id      TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  selected_item_id TEXT NOT NULL REFERENCES selected_source_items(selected_item_id),
  document_id      TEXT NOT NULL REFERENCES documents(document_id),
  span_id          TEXT NOT NULL REFERENCES spans(span_id),
  evidence_type    TEXT NOT NULL,         -- definition|source_statement|example|risk|recommendation|limitation|unknown
  summary          TEXT NOT NULL,
  quoted_text      TEXT NOT NULL,         -- contained within the span text
  support_strength TEXT,
  limitations      TEXT,                  -- JSON array
  published_at     TEXT,
  source_quality_score REAL
);

CREATE TABLE claims (
  claim_id     TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES runs(run_id),
  claim_type   TEXT NOT NULL,             -- definition|technical_fact|risk_claim|recommendation_claim|trend_claim|comparison_claim
  claim_kind   TEXT NOT NULL DEFAULT 'extractive', -- extractive|synthesized|comparative
  claim_text   TEXT NOT NULL,
  claim_scope  TEXT NOT NULL,             -- 'within provided source set'
  criticality  TEXT NOT NULL DEFAULT 'normal',  -- normal | critical
  status       TEXT NOT NULL,             -- draft | accepted | qualified | rejected
  confidence   TEXT,
  limitations  TEXT,                      -- JSON array
  derivation   TEXT                       -- JSON: {method, inputs, computed}
);
CREATE TABLE claim_evidence (
  claim_id     TEXT NOT NULL REFERENCES claims(claim_id),
  evidence_id  TEXT NOT NULL REFERENCES evidence(evidence_id),
  role         TEXT NOT NULL,             -- supporting | contradicting | qualifying
  PRIMARY KEY (claim_id, evidence_id, role)
);

CREATE TABLE claim_edges (
  run_id  TEXT NOT NULL REFERENCES runs(run_id),
  from_id TEXT NOT NULL,                  -- claim_id or evidence_id
  to_id   TEXT NOT NULL,
  type    TEXT NOT NULL                   -- supports | qualifies | refutes | cited_by | belongs_to_section | compares_to
);

CREATE TABLE entities (
  entity_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  canonical_name TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  synonyms TEXT,
  domain_tags TEXT
);

CREATE TABLE claim_entities (
  claim_id TEXT NOT NULL REFERENCES claims(claim_id),
  entity_id TEXT NOT NULL REFERENCES entities(entity_id),
  PRIMARY KEY (claim_id, entity_id)
);

CREATE TABLE citations (
  citation_id      TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL REFERENCES runs(run_id),
  evidence_id      TEXT NOT NULL REFERENCES evidence(evidence_id),
  span_id          TEXT NOT NULL REFERENCES spans(span_id),
  document_id      TEXT NOT NULL REFERENCES documents(document_id),
  selected_item_id TEXT NOT NULL REFERENCES selected_source_items(selected_item_id),
  label            TEXT NOT NULL,
  url              TEXT,
  accessed_at      TEXT
);

CREATE TABLE report_sections (
  section_id TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL REFERENCES runs(run_id),
  heading    TEXT NOT NULL,
  purpose    TEXT,
  order_index INTEGER NOT NULL,
  section_status TEXT NOT NULL DEFAULT 'ready'
);

CREATE TABLE figures (
  figure_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  kind TEXT NOT NULL,                 -- comparison_matrix
  spec TEXT NOT NULL,                 -- JSON: {columns:[...], rows:[[...]]}
  grounded_claim_ids TEXT NOT NULL    -- JSON array; every row traces to a claim
);

CREATE TABLE section_claims (
  section_id TEXT NOT NULL REFERENCES report_sections(section_id),
  claim_id   TEXT NOT NULL REFERENCES claims(claim_id),
  PRIMARY KEY (section_id, claim_id)
);
CREATE TABLE section_citations (
  section_id  TEXT NOT NULL REFERENCES report_sections(section_id),
  citation_id TEXT NOT NULL REFERENCES citations(citation_id),
  PRIMARY KEY (section_id, citation_id)
);

CREATE TABLE gate_results (
  gate_result_id TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  gate_id        TEXT NOT NULL,
  gate_version   TEXT NOT NULL,
  status         TEXT NOT NULL,           -- pass | warning | repairable_fail | hard_fail
  severity       TEXT NOT NULL,           -- blocking | warning
  checked_tables TEXT,                    -- JSON array
  issues         TEXT,                    -- JSON array
  metrics        TEXT,                    -- JSON
  created_at     TEXT NOT NULL
);

CREATE TABLE quality_dossier (
  run_id                       TEXT PRIMARY KEY REFERENCES runs(run_id),
  overall_status               TEXT NOT NULL,   -- pass | warning | fail
  blocking_gate_failures       INTEGER NOT NULL,
  warning_count                INTEGER NOT NULL,
  approved_for_report_rendering INTEGER NOT NULL,
  coverage                     TEXT,            -- JSON: QuestionCoverageGate coverage summary
  grounding_level              TEXT NOT NULL DEFAULT 'traceable' -- traceable|entailment_checked
);

CREATE TABLE repair_tasks (
  task_id     TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  gate_id     TEXT,
  description TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'planned_not_executed'
);

CREATE TABLE bundle_artifacts (
  bundle_artifact_id TEXT PRIMARY KEY,
  run_id             TEXT NOT NULL REFERENCES runs(run_id),
  kind               TEXT NOT NULL,       -- 'table' | 'export'
  ref                TEXT NOT NULL,       -- table name or export path
  sha256             TEXT,                -- for exports and table content snapshots
  bundle_status      TEXT NOT NULL        -- finalized | diagnostic_only | failed
);
