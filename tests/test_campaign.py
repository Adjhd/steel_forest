"""Campaign checks use service substitutes; they never contact providers or models."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign import engine, services, run as entry
from forest import store
from scenarios import load, validate_ranges


def fixture_config():
    scene = load("nonmagnetic")
    bounds = {element: [0, 0] for element in scene["elements"] if element != "Fe"}
    bounds["Mn"] = [4, 6]
    return {
        "version": "v20261029.001", "scenario": scene, "question": "比较共同工艺下的组织与弱磁性。",
        "space": {"composition_ranges_wt_pct": validate_ranges(scene, bounds), "protocol": {}},
        "rounds": 1, "evaluations": 1, "literature_queries": 1, "llm_calls": 5,
        "recommendations": 1, "merge": False,
    }


class ResearchServices:
    """Deterministic research messages, with real admission and delivery checks."""
    def __init__(self, config, *, admit=True, finish=True, omit_plan=False):
        self.config, self.admit, self.finish = config, admit, finish
        self.omit_plan, self.stages, self.evaluations = omit_plan, [], 0

    def ask(self, system, payload):
        if "query_limit" in payload:
            self.stages.append("search")
            return {"queries": ["What changes under a common manufacturing process?"]}
        if "sources" in payload:
            self.stages.append("hypotheses")
            return {"hypotheses": [{
                "statement": "Changing Mn under a common process may change phase stability.",
                "scope": {"conditions": "Manufacturing process remains unknown.",
                          "composition_ranges_wt_pct": self.config["space"]["composition_ranges_wt_pct"]},
                "predictions": "Replicated magnetic and phase measurements distinguish responses.",
                "falsifiers": "The proposed change is absent under matched conditions.",
                "unresolved": "Unmatched process or inadequate detection limits.",
                "verification_plan": {"measurements": ["phase", "magnetic response"]},
                "evidence_ids": [payload["sources"][0]["id"]], "source_hypothesis_ids": [],
            }]}
        if "proposal" in payload:
            self.stages.append("admission")
            return self.review(payload["proposal"], accepted=self.admit)
        if "evaluated" in payload:
            self.stages.append("review")
            evaluated = payload["evaluated"]
            answer = self.review(evaluated)
            if self.omit_plan:
                answer["accepted_node_ids"] = [node["id"] for node in evaluated["nodes"]
                                                 if node["kind"] == "candidate"]
            answer.update(recommended_candidate_ids=[node["id"] for node in evaluated["nodes"]
                                                       if node["kind"] == "candidate"],
                          finish=self.finish, next_question="Determine the common manufacturing process.")
            return answer
        self.stages.append("design")
        recipe = {element: 0 for element in self.config["scenario"]["elements"]}
        recipe.update(Fe=95, Mn=5)
        measurements = [metric["key"] for metric in self.config["scenario"]["metrics"]
                        if metric.get("target") is not None]
        return {"designs": [{"hypothesis_id": payload["hypotheses"][0]["id"], "role": "target",
            "composition": recipe, "plan": {
                "question": "Does the proposed composition retain low magnetic response?",
                "intervention": "Mn substitutes for Fe.", "controls": "Use a common recorded process.",
                "measurements": measurements, "support": "Targets pass in repeated measurements.",
                "weaken": "Targets fail in controlled repeated measurements.",
                "unresolved": "Process differs or measurement uncertainty obscures the contrast.",
                "process": "Unknown; agree with the laboratory before manufacture.",
            }}]}

    @staticmethod
    def review(delta, accepted=True):
        return {"accepted_node_ids": [node["id"] for node in delta["nodes"]] if accepted else [],
                "accepted_evidence_ids": [row["id"] for row in delta["evidence"]] if accepted else [],
                "reason": "The proposed records are suitable for further study." if accepted else "Revise the hypothesis.",
                "reviewer": "test_reviewer"}

    @staticmethod
    def literature(query):
        return [{"source_id": "test-source", "text": "An unverified retrieved passage.",
                 "status": "excerpt_unverified", "authoritative_original": False}]

    def evaluate(self, scene, composition, protocol, workdir):
        self.evaluations += 1
        metrics = {}
        for metric in scene["metrics"]:
            if metric["role"] == "screening" and metric.get("target") is not None:
                value = metric["target"] * (1.1 if metric["op"] in {">", ">="} else 0.9)
                metrics[metric["key"]] = {"value": value, "unit": metric["unit"]}
            elif metric["role"] in {"diagnostic", "screening"}:
                # Deliberately large; diagnostic values must not become hard acceptance gates.
                metrics[metric["key"]] = {"value": 1000000, "unit": metric["unit"]}
        return {"metrics": metrics, "tools": [{"id": "test-model", "version": "test-v1"}],
                "qualification": "model_diagnostic", "limitations": ["Not a physical measurement."]}


class CampaignTests(unittest.TestCase):
    def test_cross_scenario_inspiration_records_a_testable_transfer(self):
        config = fixture_config()
        stub = ResearchServices(config)
        sources = [{'id': 'L1', 'source_id': 'fixture', 'text': 'Unverified source'}]
        context = {'hypotheses': [{'id': 'SOURCE', 'scenario': 'fixed_wheel_cyclic_immersion'}], 'evidence': []}
        answer = stub.ask('', {'sources': sources})
        hypothesis = answer['hypotheses'][0]
        hypothesis['source_hypothesis_ids'] = ['SOURCE']
        with self.assertRaisesRegex(ValueError, '跨场景'):
            engine.make_hypotheses(answer, sources, context, config, 'test')
        hypothesis['transfer_contract'] = {
            'source_conditions': 'Corrosion protocol', 'target_conditions': 'Magnetic protocol',
            'shared_mechanism': 'A proposed phase-stability mechanism', 'verification': 'Matched phase measurements'}
        delta = engine.make_hypotheses(answer, sources, context, config, 'test')
        self.assertEqual(delta['links'][0]['relation'], 'transfer_candidate')
        self.assertEqual(delta['links'][0]['transfer_contract'], hypothesis['transfer_contract'])

    def test_recycle_moves_only_a_project_object_without_reading_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(ROOT / 'recycle.sh', root / 'recycle.sh')
            source = root / 'unused.txt'
            source.write_text('temporary test object')
            result = subprocess.run(['bash', str(root / 'recycle.sh'), 'unused.txt', 'software test'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(source.exists())
            denied = subprocess.run(['bash', str(root / 'recycle.sh'), '/etc/hosts'],
                                    capture_output=True, text=True)
            self.assertNotEqual(denied.returncode, 0)

    def test_decimal_recipe_can_also_be_a_fixed_design_space(self):
        scene = load('nonmagnetic')
        recipe = {element: 0 for element in scene['elements']}
        recipe.update(C=.22, Si=.48, Mn=16.14, Cr=18.71, Ni=4.92, Cu=1.90, Mo=1.91, Al=.09, N=.44, Fe=55.19)
        bounds = {element: [value, value] for element, value in recipe.items()}
        self.assertEqual(validate_ranges(scene, bounds), bounds)

    def run_research(self, config=None, *, stub_type=ResearchServices, initial_state=None, **options):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        config = config or fixture_config()
        services_stub = stub_type(config, **options)
        canonical = root / "forest" / "canonical.json"
        with patch.object(store, "CANONICAL", canonical):
            result = engine.run(config, root, ask=services_stub.ask,
                literature=services_stub.literature, evaluate=services_stub.evaluate,
                initial_state=store.load(canonical) if initial_state is None else initial_state)
        return result, services_stub, root, canonical

    def test_complete_reviewed_campaign_merges_research_without_promoting_evidence(self):
        config = fixture_config()
        config["merge"] = True
        result, stub, root, canonical = self.run_research(config)
        self.assertEqual(stub.stages, ["search", "hypotheses", "admission", "design", "review"])
        self.assertEqual(result["status"], "frozen_pending_measurement")
        self.assertEqual(len(result["recommended_candidate_ids"]), 1)
        self.assertEqual(result["usage"]["model_evaluations"], 1)
        state = store.load(canonical)
        self.assertEqual({row["kind"] for row in state["evidence"].values()}, {"literature", "model"})
        self.assertTrue(all(row["scientific_result"] is False for row in state["evidence"].values()))
        self.assertTrue(all(row["physical_measurement"] is False for row in state["evidence"].values()))
        self.assertTrue((root / "round_01" / "literature.json").is_file())
        entry.write_report(root, config, result)
        self.assertIn("frozen_pending_measurement", (root / "候选报告.md").read_text())

    def test_rejected_hypothesis_never_reaches_candidate_models(self):
        result, stub, _, canonical = self.run_research(admit=False)
        self.assertEqual(stub.stages, ["search", "hypotheses", "admission"])
        self.assertEqual(stub.evaluations, 0)
        self.assertEqual(result["recommended_candidate_ids"], [])
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertFalse(canonical.exists())

    def test_budget_stop_is_not_a_final_recommendation(self):
        result, _, _, _ = self.run_research(finish=False)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(len(result["nominations"]), 1)
        self.assertEqual(result["recommended_candidate_ids"], [])

    def test_review_cannot_recommend_a_candidate_without_its_accepted_plan(self):
        with self.assertRaisesRegex(ValueError, "plan_id"):
            self.run_research(omit_plan=True)
        self.assertFalse((Path(self.temporary.name) / "forest/canonical.json").exists())

    def test_hypothesis_and_search_cannot_expand_the_declared_scope(self):
        for violation in ("hypothesis", "search"):
            model_calls = []

            class OutOfScope(ResearchServices):
                def ask(self, system, payload):
                    answer = super().ask(system, payload)
                    if violation == "hypothesis" and "sources" in payload:
                        answer["hypotheses"][0]["scope"]["composition_ranges_wt_pct"] = {
                            **self.config["space"]["composition_ranges_wt_pct"], "Mn": [4, 7]}
                    if violation == "search" and self.stages[-1] == "design":
                        answer["designs"][0].update(search_ranges_wt_pct={"Mn": [4, 7]})
                        answer["designs"][0]["plan"]["vary_elements"] = ["Mn"]
                    return answer

                def evaluate(self, *args):
                    model_calls.append(args)
                    return super().evaluate(*args)

            with self.subTest(violation=violation), self.assertRaisesRegex(ValueError, "范围"):
                self.run_research(stub_type=OutOfScope)
            self.assertEqual(model_calls, [])

    def test_review_can_use_earlier_campaign_candidates_but_not_canonical_candidates(self):
        class EarlierRound(ResearchServices):
            def ask(self, system, payload):
                answer = super().ask(system, payload)
                if "evaluated" in payload:
                    previous = [node["id"] for node in payload["campaign_pool"]["nodes"]
                                if node["kind"] == "candidate"]
                    answer.update(finish=bool(previous), recommended_candidate_ids=previous)
                return answer

        config = fixture_config()
        config.update(rounds=2, llm_calls=10)
        result, _, _, _ = self.run_research(config, stub_type=EarlierRound)
        self.assertEqual(result["status"], "frozen_pending_measurement")
        self.assertEqual(result["recommended_candidate_ids"], ["v20261029.001-R01-C001"])

        class HistoricalCandidate(ResearchServices):
            def ask(self, system, payload):
                answer = super().ask(system, payload)
                if "evaluated" in payload:
                    answer["recommended_candidate_ids"] = ["HISTORICAL-CANDIDATE"]
                return answer

        state = {"revision": 0, "nodes": {"HISTORICAL-CANDIDATE": {
            "id": "HISTORICAL-CANDIDATE", "kind": "candidate", "scenario": config["scenario"]["id"]}},
            "evidence": {}, "links": [], "campaigns": []}
        result, _, _, _ = self.run_research(stub_type=HistoricalCandidate, initial_state=state)
        self.assertEqual(result["recommended_candidate_ids"], [])
        self.assertFalse(result["delivery"]["accepted"])

    def test_later_hypothesis_can_cite_accepted_evidence_without_readding_it(self):
        class EarlierEvidence(ResearchServices):
            def ask(self, system, payload):
                answer = super().ask(system, payload)
                if "sources" in payload and payload["forest"]["evidence"]:
                    answer["hypotheses"][0]["evidence_ids"] = [payload["forest"]["evidence"][0]["id"]]
                if "proposal" in payload:
                    answer["accepted_evidence_ids"] = list({eid for node in payload["proposal"]["nodes"]
                                                           for eid in node["evidence_ids"]})
                if "evaluated" in payload:
                    answer["finish"] = self.stages.count("review") == 2
                return answer

        config = fixture_config()
        config.update(rounds=2, llm_calls=10, merge=True)
        result, stub, root, canonical = self.run_research(config, stub_type=EarlierEvidence)
        self.assertEqual(result["status"], "frozen_pending_measurement")
        self.assertEqual(stub.evaluations, 2)
        state = store.load(canonical)
        self.assertEqual(state["nodes"]["v20261029.001-R02-H01"]["evidence_ids"], ["v20261029.001-R01-L01"])
        literature_ids = [row["id"] for row in result["delta"]["evidence"] if row["kind"] == "literature"]
        self.assertEqual(literature_ids, ["v20261029.001-R01-L01"])
        entry.write_report(root, config, result)
        self.assertIn("v20261029.001-R01-L01", (root / "候选报告.md").read_text())

        class UnknownEvidence(EarlierEvidence):
            def ask(self, system, payload):
                answer = super().ask(system, payload)
                if "proposal" in payload:
                    answer["accepted_evidence_ids"].append("UNKNOWN-EVIDENCE")
                return answer

        with self.assertRaisesRegex(ValueError, "不存在"):
            self.run_research(config, stub_type=UnknownEvidence)

    def test_hea_diagnostics_do_not_screen_against_physical_measurement_targets(self):
        class HeaResearch(ResearchServices):
            def ask(self, system, payload):
                answer = super().ask(system, payload)
                if self.stages[-1] == "design":
                    answer["designs"][0]["composition"] = {element: 25 for element in ("Fe", "Co", "Ni", "Cr")}
                return answer

        config = fixture_config()
        config["scenario"] = load("hea")
        config["space"] = {"composition_ranges_wt_pct": {element: [20, 30] for element in ("Fe", "Co", "Ni", "Cr")},
                           "protocol": {"process_text": "Unknown", "testing": {"test_temperature_C": 700}}}
        result, _, _, _ = self.run_research(config, stub_type=HeaResearch)
        self.assertEqual(result["status"], "frozen_pending_measurement")
        prediction = next(row for row in result["delta"]["evidence"] if row["kind"] == "model")
        self.assertEqual(prediction["metrics"]["reported_density"]["value"], 1000000)
        self.assertNotIn("density", prediction["metrics"])

    def test_prepare_is_offline_and_existing_version_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            space = root / "space.json"
            space.write_text(json.dumps(fixture_config()["space"]))
            argv = ["--version", "v20261029.001", "--question", "研究弱磁材料", "--space", str(space),
                    "--prepare-only", "--evaluations", "1", "--recommendations", "1"]
            with patch.object(entry, "ROOT", root), patch.object(engine, "run") as execute, \
                    patch.object(store, "load") as read_forest, patch.object(services, "_post") as external:
                self.assertEqual(entry.main(argv), 0)
                original = (root / "outputs/v20261029.001/input.json").read_bytes()
                with self.assertRaises(FileExistsError):
                    entry.main(argv)
                self.assertEqual((root / "outputs/v20261029.001/input.json").read_bytes(), original)
            execute.assert_not_called()
            read_forest.assert_not_called()
            external.assert_not_called()

    def test_bash_launcher_accepts_a_natural_language_question(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "steel_forest_hf"
            root.mkdir()
            (Path(temporary) / "steel_forest").symlink_to(ROOT.parent / "steel_forest", target_is_directory=True)
            for name in ("campaign/run.sh", "campaign/run.py", "campaign/engine.py", "campaign/services.py",
                         "campaign/prompts.md", "campaign/space.json", "campaign/question.md",
                         "forest/store.py", "scenarios/__init__.py", "scenarios/tools.py", "scenarios/nonmagnetic/scene.json"):
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, destination)
            env = {"PATH": os.environ["PATH"], "STEEL_HF_PYTHON": sys.executable,
                   "PYTHONDONTWRITEBYTECODE": "1"}
            command = ["bash", str(root / "campaign/run.sh"), "--version", "v20261029.002",
                       "--question", "在明确条件下研究低磁性材料", "--prepare-only"]
            prepared = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            config = json.loads((root / "outputs/v20261029.002/input.json").read_text())
            self.assertEqual(config["question"], "在明确条件下研究低磁性材料")
            repeated = subprocess.run(command, env=env, text=True, capture_output=True, timeout=20)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertFalse((root / "forest/canonical.json").exists())


class ServiceTests(unittest.TestCase):
    def test_provider_content_keeps_its_evidence_boundary(self):
        responses = [
            {"data": {"variables": [{"title": "A paper", "claim": "A provider's interpretation"}]}},
            {"data": {"context": "Retrieved context", "retrieved_chunks": [
                {"chunk_id": "body_1", "paper_id": "p1", "text": "A retrieved passage"}]}},
        ]
        with patch.dict(os.environ, {"BOHR_ACCESS_KEY": "test", "DPRAG_API_KEY": "test"}, clear=True):
            with patch.object(services, "_post", side_effect=responses) as post:
                rows = services.literature("Does the mechanism apply to this process?")
        self.assertEqual(post.call_count, 2)
        self.assertEqual([row["text"] for row in rows],
                         ["A provider's interpretation", "A retrieved passage"])
        self.assertEqual([row["status"] for row in rows], ["provider_lead_only", "excerpt_unverified"])
        self.assertTrue(all(row["authoritative_original"] is False for row in rows))
        self.assertEqual(rows[1]["raw"]["paper_id"], "p1")

    def test_missing_credentials_and_truncated_response_are_not_results(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(services, "_post") as post:
            with self.assertRaises(services.ServiceError):
                services.literature("A scientific question")
            post.assert_not_called()
        env = {"LITELLM_API_BASE": "https://local.example/v1", "LITELLM_API_KEY": "test",
               "STEEL_FOREST_LLM_MODEL": "test"}
        response = {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        with patch.dict(os.environ, env, clear=True), patch.object(services, "_post", return_value=response):
            with self.assertRaisesRegex(services.ServiceError, "截断"):
                services.ask("Return a research decision", {})


if __name__ == "__main__":
    unittest.main()
