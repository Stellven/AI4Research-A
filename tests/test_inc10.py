"""Increment 10 (#10): topic-derived ONTOLOGY. The deterministic EntityTagOperator tags claims
against the pack's static vocabulary (the floor); OntologyDeriveOperator then asks the LLM for the
topic's salient concepts and code GROUNDS each — a proposed entity is kept only if its name/synonym
actually occurs (word-boundary) in a real accepted claim, so the ontology can never be hallucinated.
Prompt-keyed StubRuntime, mirroring test_inc6."""
import json
import re
import tempfile
import unittest

from ai4research.model_runtime import StubRuntime
from ai4research.operators import OperatorRunner, build_pipeline
from ai4research.operators.llm import _anchor_claims, _contested_claims
from ai4research.report import _key_concepts
from tests import support


class _FakeReport:
    def __init__(self, entities, links, claims, container_by_claim):
        self.entities = entities
        self.claim_entities = links
        self.claims = claims
        self.container_by_claim = container_by_claim


class _FakeWork:
    def __init__(self, links):
        self._links = links

    def read_rows(self, table):
        return self._links if table == "claim_entities" else []

_ONTOLOGY_KEY = "ONTOLOGY of a research topic"   # only the OntologyDerive prompt contains this


def _write_run_config(ctx, config: dict) -> None:
    (ctx.input_dir / "run_config.json").write_text(json.dumps(config), encoding="utf-8")


def _excerpt_words(prompt: str) -> list:
    body = prompt.split("Claim excerpts:", 1)[-1]
    return re.findall(r"[A-Za-z]{5,}", body)


def _run(tmp, proposal_fn, *, pack="youtube_github_research", runtime="stub", containers=None):
    ctx, work = support.stage(tmp, support.PROVING_TOPIC, containers or [support.local_container()])
    cfg = {"domain_pack": pack}
    if runtime:
        cfg["model_runtime"] = runtime
    _write_run_config(ctx, cfg)

    def stub(prompt):
        if proposal_fn is not None and _ONTOLOGY_KEY in prompt:
            return [proposal_fn(prompt)]
        return []

    rt = StubRuntime(stub) if runtime else None
    OperatorRunner(build_pipeline(model_runtime=rt)).run(ctx, work)
    from ai4research.finalize import finalize
    return ctx, work, finalize(ctx, work)


def _llm_entities(work):
    return [e for e in work.read_rows("entities") if "llm_derived" in (e.get("domain_tags") or [])]


class OntologyDeriveTest(unittest.TestCase):
    def test_pipeline_order(self):
        names = [op.NAME for op in build_pipeline()]
        self.assertLess(names.index("EntityTagOperator"), names.index("OntologyDeriveOperator"))
        self.assertLess(names.index("OntologyDeriveOperator"), names.index("ContradictionDetectOperator"))
        self.assertLess(names.index("OntologyDeriveOperator"), names.index("AnswerSynthesisOperator"))

    def test_grounded_entities_written_ungrounded_dropped(self):
        def proposal(prompt):
            words = _excerpt_words(prompt)
            grounded = words[0]                       # a word that really occurs in a claim
            return {"entities": [
                {"name": grounded, "type": "Concept", "synonyms": []},
                {"name": "Zzqxnonexistent Concept", "type": "Concept", "synonyms": []},  # absent -> dropped
            ]}

        with tempfile.TemporaryDirectory() as tmp:
            ctx, work, result = _run(tmp, proposal)
            self.assertEqual(result.status, "finalized")
            llm = _llm_entities(work)
            self.assertEqual(len(llm), 1)             # only the grounded entity survived
            self.assertNotIn("zzqxnonexistent concept", {e["canonical_name"].lower() for e in work.read_rows("entities")})
            ent_id = llm[0]["entity_id"]
            links = [l for l in work.read_rows("claim_entities") if l["entity_id"] == ent_id]
            self.assertTrue(links)                    # the grounded entity tags at least one real claim

    def test_static_floor_preserved_alongside_llm_entities(self):
        # the static-vocabulary entities (the deterministic floor) coexist with the derived ones
        def proposal(prompt):
            return {"entities": [{"name": _excerpt_words(prompt)[0], "type": "Concept", "synonyms": []}]}

        with tempfile.TemporaryDirectory() as tmp:
            _, work, _ = _run(tmp, proposal)
            static = [e for e in work.read_rows("entities") if "llm_derived" not in (e.get("domain_tags") or [])]
            self.assertTrue(static)                   # EntityTagOperator's static tagging still ran
            self.assertTrue(_llm_entities(work))      # and the derived layer was added

    def test_inert_without_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run(tmp, None, runtime=None)
            self.assertEqual(result.status, "finalized")
            self.assertEqual(_llm_entities(work), [])  # no derived ontology without a runtime

    def test_inert_without_pack_optin(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, work, result = _run(tmp, lambda p: {"entities": []}, pack="generic")
            self.assertEqual(result.status, "finalized")
            self.assertEqual(_llm_entities(work), [])


class ContestedAnchoringTest(unittest.TestCase):
    """10b: the ontology drives contradiction anchoring — claims on contested cross-source entities
    are disputed first."""

    def test_contested_detection_is_cross_source(self):
        links = [                                   # E1 spans containers A+B; E2 only A
            {"claim_id": "cA1", "entity_id": "E1"},
            {"claim_id": "cB1", "entity_id": "E1"},
            {"claim_id": "cA2", "entity_id": "E2"},
        ]
        claim_container = {"cA1": "A", "cB1": "B", "cA2": "A"}
        accepted = {"cA1", "cB1", "cA2"}
        self.assertEqual(_contested_claims(_FakeWork(links), claim_container, accepted), {"cA1", "cB1"})

    def test_contested_claim_anchored_ahead_of_higher_kind(self):
        claims = [
            {"claim_id": "c_ext_plain", "claim_kind": "extractive"},
            {"claim_id": "c_synth", "claim_kind": "synthesized"},
            {"claim_id": "c_ext_contested", "claim_kind": "extractive"},
        ]
        accepted = {c["claim_id"] for c in claims}
        cont = {c["claim_id"]: "A" for c in claims}
        # contested extractive beats a non-contested synthesized claim despite the lower kind rank
        anchored = _anchor_claims(claims, accepted, cont, {"c_ext_contested"})
        self.assertEqual(anchored[0]["claim_id"], "c_ext_contested")
        # without an ontology, ordering falls back to kind (synthesized first)
        self.assertEqual(_anchor_claims(claims, accepted, cont, set())[0]["claim_id"], "c_synth")


class _FakeStore:
    def __init__(self, tables):
        self.tables = tables

    def read_rows(self, name):
        return self.tables.get(name, [])

    def write_rows(self, name, rows):
        self.tables[name] = rows


class ClaimEntityBackfillTest(unittest.TestCase):
    """Post-gate backfill: claims promoted to accepted AFTER the taggers ran (the synthesized
    comparison claims) must still get entity links, or contested-anchoring can't see them."""

    def test_tags_accepted_claims_missed_by_pregate_taggers(self):
        from ai4research.operators.extraction import ClaimEntityBackfillOperator
        tables = {
            "entities": [{"entity_id": "E1", "canonical_name": "Mamba", "synonyms": []}],
            "claim_entities": [],   # synthesized claim was draft when taggers ran -> untagged
            "claims": [
                {"claim_id": "c1", "claim_text": "Mamba scales linearly with sequence length.", "status": "accepted"},
                {"claim_id": "c2", "claim_text": "Some unrelated statement about widgets.", "status": "accepted"},
                {"claim_id": "c3", "claim_text": "Mamba is wonderful.", "status": "draft"},   # not accepted
            ],
        }
        res = ClaimEntityBackfillOperator().run(None, _FakeStore(tables), [])
        links = tables["claim_entities"]
        self.assertIn({"claim_id": "c1", "entity_id": "E1"}, links)
        self.assertTrue(all(l["claim_id"] != "c2" for l in links))   # no concept mention
        self.assertTrue(all(l["claim_id"] != "c3" for l in links))   # draft, not accepted
        self.assertEqual(res["backfilled"], 1)

    def test_inert_without_ontology(self):
        from ai4research.operators.extraction import ClaimEntityBackfillOperator
        tables = {"entities": [], "claim_entities": [],
                  "claims": [{"claim_id": "c1", "claim_text": "Mamba", "status": "accepted"}]}
        self.assertEqual(ClaimEntityBackfillOperator().run(None, _FakeStore(tables), [])["backfilled"], 0)

    def test_runs_after_gate_suite_before_contradiction(self):
        names = [op.NAME for op in build_pipeline()]
        self.assertLess(names.index("PreRenderQualityGateSuite"), names.index("ClaimEntityBackfillOperator"))
        self.assertLess(names.index("ClaimEntityBackfillOperator"), names.index("ContradictionDetectOperator"))


class KeyConceptsRenderTest(unittest.TestCase):
    """10c: the ontology surfaces as a relational spine — cross-source concepts flagged contested."""

    def _data(self):
        entities = [
            {"entity_id": "E1", "canonical_name": "Mamba", "entity_type": "Architecture"},
            {"entity_id": "E2", "canonical_name": "SRAM", "entity_type": "System"},
        ]
        links = [
            {"claim_id": "cA", "entity_id": "E1"},   # Mamba in C1 + C2 -> contested
            {"claim_id": "cB", "entity_id": "E1"},
            {"claim_id": "cA", "entity_id": "E2"},   # SRAM only in C1 -> not contested
        ]
        claims = [{"claim_id": "cA", "status": "accepted"}, {"claim_id": "cB", "status": "accepted"}]
        return _FakeReport(entities, links, claims, {"cA": "C1", "cB": "C2"})

    def test_contested_concept_flagged(self):
        md = "\n".join(_key_concepts(self._data()))
        self.assertIn("## Key concepts", md)
        self.assertIn("⚑ **Mamba**", md)          # cross-source -> contested
        self.assertIn("- **SRAM**", md)            # single-source -> rendered, not flagged
        self.assertNotIn("⚑ **SRAM**", md)

    def test_empty_ontology_renders_nothing(self):
        self.assertEqual(_key_concepts(_FakeReport([], [], [], {})), [])


if __name__ == "__main__":
    unittest.main()
