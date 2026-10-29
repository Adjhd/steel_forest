"""Campaign checks use service substitutes; they never contact providers or models."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from campaign import engine, services, run as entry
from forest import store
from scenarios import complete_composition, load, validate_ranges


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
                "evidence_ids": [source["id"] for source in payload["sources"][:1]], "source_hypothesis_ids": [],
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

    def test_sparse_steel_inputs_fill_only_zero_elements_and_fe_balance(self):
        scene = load('nonmagnetic')
        bounds = validate_ranges(scene, {'Mn': [4, 6]})
        recipe = complete_composition(scene, {'Mn': 5}, bounds)
        self.assertEqual(recipe['Fe'], 95)
        self.assertEqual(recipe['C'], 0)
        self.assertEqual(set(recipe), set(scene['elements']))
        self.assertEqual(sum(recipe.values()), 100)
        with self.assertRaisesRegex(ValueError, 'Mn=.*outside'):
            complete_composition(scene, {'Fe': 100}, bounds)
        with self.assertRaisesRegex(ValueError, 'Unsupported elements'):
            validate_ranges(scene, {'not_an_element': [0, 1]})

    def test_hea_hypothesis_may_exclude_a_zero_lower_bound_element(self):
        config = fixture_config()
        config['scenario'] = load('hea')
        config['space']['composition_ranges_wt_pct'] = {
            'Fe': [20, 40], 'Co': [20, 40], 'Ni': [20, 40], 'Cr': [0, 10]}
        answer = ResearchServices(config).ask('', {'sources': []})
        answer['hypotheses'][0]['scope']['composition_ranges_wt_pct'] = {
            'Fe': [20, 40], 'Co': [20, 40], 'Ni': [20, 40]}
        proposal = engine.make_hypotheses(answer, [], {'hypotheses': [], 'evidence': []}, config, 'HEA')
        recipe = complete_composition(config['scenario'], {'Fe': 30, 'Co': 30, 'Ni': 40},
                                      proposal['nodes'][0]['scope']['composition_ranges_wt_pct'])
        self.assertEqual(sum(recipe.values()), 100)
        self.assertEqual(recipe.get('Cr', 0), 0)
        design = ResearchServices(config).ask('', {'hypotheses': proposal['nodes']})['designs'][0]
        design.update(composition={**recipe, 'Cr': 0}, search_ranges_wt_pct={'Fe': [20, 40]})
        design['plan']['vary_elements'] = ['Fe']
        self.assertEqual(len(engine.expand_designs([design], proposal['nodes'], config, 1)), 1)

    def test_context_retains_every_reference_of_a_selected_hypothesis(self):
        state = {'revision': 1, 'nodes': {'H1': {
            'id': 'H1', 'kind': 'hypothesis', 'scenario': 'scene', 'evidence_ids': ['L1', 'L2', 'L3']}},
            'links': [], 'evidence': {key: {'id': key} for key in ('L1', 'L2', 'L3', 'UNRELATED')}}
        context = store.context('scene', limit=1, state=state)
        self.assertEqual(len(context['hypotheses']), 1)
        self.assertEqual({row['id'] for row in context['evidence']}, {'L1', 'L2', 'L3'})

    def run_research(self, config=None, *, stub_type=ResearchServices, initial_state=None, **options):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        config = config or fixture_config()
        project = Path(self.temporary.name)
        root = project / 'outputs' / config['version']
        root.mkdir(parents=True)
        engine.save(root / 'input.json', config)
        services_stub = stub_type(config, **options)
        canonical = project / "forest" / "canonical.json"
        with patch.object(store, "CANONICAL", canonical):
            result = engine.run(config, root, ask=services_stub.ask,
                literature=services_stub.literature, evaluate=services_stub.evaluate,
                initial_state=store.load(canonical) if initial_state is None else initial_state)
        return result, services_stub, root, canonical

    def test_stage_readings_keep_cited_science_and_leave_original_records_intact(self):
        class ReferencedResearch(ResearchServices):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.requests = []
                self.searches = 0

            def literature(self, query):
                self.searches += 1
                return [{'source_id': f'paper-{self.searches}-{i}', 'title': f'Paper {i}',
                         'text': f'Passage from round {self.searches}, paper {i}.',
                         'url': f'https://example.test/{self.searches}/{i}',
                         'provider': 'fixture', 'status': 'excerpt_unverified',
                         'authoritative_original': False,
                         'raw': {'text': f'Passage from round {self.searches}, paper {i}.',
                                 'provider_trace': 'Retain this in the saved original.'}}
                        for i in (1, 2)]

            def ask(self, system, payload):
                self.requests.append((system, copy.deepcopy(payload)))
                answer = super().ask(system, payload)
                if 'sources' in payload:
                    hypothesis = answer['hypotheses'][0]
                    hypothesis['evidence_ids'].append('L0')
                    hypothesis['source_hypothesis_ids'] = ['H0']
                    if self.searches == 2:
                        hypothesis['evidence_ids'].append(f'{self.config["version"]}-R01-L01')
                if 'evaluated' in payload:
                    answer['finish'] = self.searches == 2
                return answer

            def evaluate(self, *args):
                return {**super().evaluate(*args), 'raw': {'full_model_log': 'Saved model record.'}}

        config = fixture_config()
        config.update(rounds=2, llm_calls=10)
        config['scenario']['tools'][0]['asset'] = 'asset-path-not-a-scientific-input'
        state = {'revision': 3, 'nodes': {'H0': {
            'id': 'H0', 'kind': 'hypothesis', 'scenario': config['scenario']['id'],
            'statement': 'An existing hypothesis.', 'scope': copy.deepcopy(config['space']),
            'predictions': 'Matched measurements should differ.', 'falsifiers': 'No difference.',
            'evidence_ids': ['L0'], 'verification_plan': {'details': 'An entire earlier experiment plan.'}}},
            'links': [], 'campaigns': [], 'evidence': {'L0': {
                'id': 'L0', 'kind': 'literature', 'source': 'https://example.test/original',
                'summary': 'The existing cited passage.', 'status': 'unverified',
                'record': {'text': 'The existing cited passage.', 'url': 'https://example.test/original',
                           'authoritative_original': False, 'raw': {'text': 'The existing cited passage.'}}}}}
        state['nodes']['H0']['evidence_ids'].append('L-SOURCE')
        state['evidence']['L-SOURCE'] = {'id': 'L-SOURCE', 'kind': 'literature',
            'source': 'https://example.test/source', 'summary': 'Evidence for the inspiring source hypothesis.',
            'status': 'unverified'}
        original_config, original_state = copy.deepcopy(config), copy.deepcopy(state)
        result, stub, root, _ = self.run_research(config, stub_type=ReferencedResearch, initial_state=state)
        self.assertEqual(result['status'], 'frozen_pending_measurement')
        self.assertEqual(config, original_config)
        self.assertEqual(state, original_state)
        for round_index in (1, 2):
            folder = root / f'round_{round_index:02}'
            payloads = {stage: json.loads((folder / f'{stage}_input.json').read_text())
                        for stage in ('search', 'hypotheses', 'admission', 'design', 'review')}
            search = payloads['search']
            self.assertNotIn('sources', search)
            self.assertNotIn('evidence', search['forest'])
            self.assertNotIn('asset-path-not-a-scientific-input', json.dumps(payloads))
            self.assertNotIn('An entire earlier experiment plan.', json.dumps(search))
            self.assertEqual(search['query_limit'], 1)
            sources = payloads['hypotheses']['sources']
            self.assertEqual(len(sources), 2)
            for source in sources:
                self.assertNotIn('raw', source)
                self.assertEqual(json.dumps(sources).count(source['text']), 1)
                self.assertEqual(source['status'], 'excerpt_unverified')
                self.assertIs(source['authoritative_original'], False)
                self.assertTrue(source['source_id'] and source['url'] and source['id'])
            current_id = f'{config["version"]}-R{round_index:02}-L01'
            admission = payloads['admission']
            self.assertEqual([row['id'] for row in admission['proposal']['evidence']], [current_id])
            self.assertEqual([node['id'] for node in admission['forest']['hypotheses']], ['H0'])
            self.assertIn('Evidence for the inspiring source hypothesis.',
                          [row['text'] for row in admission['forest']['evidence']])
            expected = {'L0', current_id}
            if round_index == 2:
                expected.add(f'{config["version"]}-R01-L01')
            for stage in ('design', 'review'):
                self.assertNotIn('forest', payloads[stage])
                records = {record['id']: record for record in payloads[stage]['literature']}
                self.assertEqual(set(records), expected)
                self.assertEqual(records['L0']['text'], 'The existing cited passage.')
                self.assertEqual(records[current_id]['text'], f'Passage from round {round_index}, paper 1.')
                self.assertIs(records[current_id]['authoritative_original'], False)
            self.assertIn('evaluations', payloads['design'])
            self.assertNotIn('evaluated', payloads['design'])
            self.assertIn('evaluated', payloads['review'])
            original_sources = json.loads((folder / 'literature.json').read_text())
            self.assertEqual(len(original_sources), 2)
            self.assertIn('provider_trace', original_sources[1]['raw'])
            original_model = json.loads((folder / 'evaluated.json').read_text())['evidence'][0]
            self.assertEqual(original_model['raw']['full_model_log'], 'Saved model record.')
            self.assertIn('source', original_model)
            self.assertIn('tools', original_model)
            self.assertNotIn('raw', payloads['review']['evaluated']['evidence'][0])
        self.assertEqual(len(stub.requests), 10)

    def test_review_preserves_all_candidates_and_individual_model_conditions(self):
        def records(index, mn, *, different_conditions=False):
            cid, pid, eid = (f'{kind}{index}' for kind in ('C', 'P', 'M'))
            composition = {'Mn': mn, 'Fe': 100 - mn}
            evidence = {'id': eid, 'kind': 'model', 'composition': dict(composition),
                'metrics': {'permeability': {'value': 1.000123456789, 'unit': '1'}},
                'protocol': {'temperature_C': 80 if different_conditions else 20},
                'qualification': 'model_diagnostic', 'limitations': ['Measurement still required.'],
                'tools': [{'id': 'model', 'version': 'v1', 'purpose': 'Predict', 'asset': 'local-weight-file'}]}
            if different_conditions:
                evidence.update(warnings=['Outside training temperature range.'],
                                applicability={'temperature_supported': False})
                evidence['tools'][0]['warning'] = 'Temperature extrapolation.'
                evidence['composition']['Mn'] = mn + 0.000001
            return {'nodes': [
                {'id': cid, 'kind': 'candidate', 'composition': composition, 'role': 'target',
                 'hypothesis_id': 'H1', 'hypothesis_version': 1, 'plan_id': pid, 'evidence_ids': [eid]},
                {'id': pid, 'kind': 'experiment_plan', 'candidate_id': cid,
                 'question': 'Compare under a common process.', 'controls': 'Fixed thermal history.',
                 'measurements': ['phase', 'permeability'], 'process': 'Record manufacturing conditions.'}],
                'evidence': [evidence], 'links': []}

        previous = records(1, 4.123456789012345)
        evaluated = records(2, 5.234567890123456)
        engine.append_delta(evaluated, records(3, 5.876543210987654, different_conditions=True))
        originals = copy.deepcopy((evaluated, previous))
        reading = engine.review_reading(evaluated, previous)
        self.assertEqual((evaluated, previous), originals)
        self.assertEqual(len(reading['plan_details']), 1)
        self.assertEqual(len(reading['model_context']), 2)
        for name, original in (('evaluated', evaluated), ('campaign_pool', previous)):
            visible = reading[name]
            nodes = {node['id']: node for node in visible['nodes']}
            self.assertEqual(set(nodes), {node['id'] for node in original['nodes']})
            for node in original['nodes']:
                if node['kind'] == 'candidate':
                    self.assertEqual(nodes[node['id']]['composition'], node['composition'])
                    self.assertEqual(nodes[node['id']]['evidence_ids'], node['evidence_ids'])
                else:
                    self.assertEqual(nodes[node['id']]['candidate_id'], node['candidate_id'])
                    details = reading['plan_details'][nodes[node['id']]['plan_details']]
                    self.assertEqual(details['controls'], node['controls'])
                    self.assertEqual(details['measurements'], node['measurements'])
            self.assertEqual({row['id'] for row in visible['evidence']},
                             {row['id'] for row in original['evidence']})
            for source, row in zip(original['evidence'], visible['evidence']):
                self.assertEqual(row['metrics'], source['metrics'])
                context = reading['model_context'][row['model_context']]
                self.assertEqual(context['protocol'], source['protocol'])
                self.assertNotIn('asset', context['tools'][0])
                if source['id'] == 'M3':
                    self.assertEqual(row['warnings'], source['warnings'])
                    self.assertEqual(row['applicability'], source['applicability'])
                    self.assertEqual(row['composition'], source['composition'])
                    self.assertEqual(context['tools'][0]['warning'], source['tools'][0]['warning'])

    def test_forest_reading_preserves_scopes_quotes_and_transfer_requirements(self):
        state = {'revision': 1, 'evidence': {}, 'nodes': {
            key: {'id': key, 'kind': 'hypothesis', 'scenario': scenario,
                  'statement': 'A conditional mechanism.', 'predictions': 'A measurable contrast.',
                  'scope': {'conditions': conditions}, 'evidence_ids': []}
            for key, scenario, conditions in (('H1', 'target', 'Room temperature'),
                                              ('H2', 'source', 'Elevated temperature'))},
            'links': [{'source': 'H2', 'target': 'H1', 'relation': 'transfer_candidate',
                'transfer_contract': {'source_scope': {'conditions': 'Elevated temperature'},
                    'target_scope': {'conditions': 'Room temperature'}, 'contract_version': 1,
                    'shared_mechanism': 'A proposed phase mechanism.',
                    'verification': 'Measure both temperatures.',
                    'scope_differences': [{'axis': 'temperature', 'source': 'Elevated', 'target': 'Room',
                                           'source_hypothesis_id': 'H2', 'target_hypothesis_id': 'H1'}]}}]}
        state['nodes']['H2']['source_refs'] = [{'quote': 'Observed under the elevated-temperature protocol.',
            'source_span': {'path': 'papers/source.txt', 'start': 10, 'end': 60},
            'quote_binding': {'doi': '10.example/paper', 'binding_revision': 8}}]
        original = copy.deepcopy(state)
        context = store.context('target', limit=2, state=state)
        cards = {node['id']: node for node in context['hypotheses']}
        self.assertEqual(cards['H1']['scope'], state['nodes']['H1']['scope'])
        self.assertEqual(cards['H2']['scope'], state['nodes']['H2']['scope'])
        reference = cards['H2']['source_refs'][0]
        self.assertEqual(reference['quote'], state['nodes']['H2']['source_refs'][0]['quote'])
        self.assertEqual(reference['source_location'],
                         {'path': 'papers/source.txt', 'start': 10, 'end': 60, 'doi': '10.example/paper'})
        contract = context['links'][0]['transfer_contract']
        self.assertEqual(contract['verification'], 'Measure both temperatures.')
        self.assertEqual(contract['scope_differences'],
                         [{'axis': 'temperature', 'source': 'Elevated', 'target': 'Room'}])
        self.assertNotIn('source_scope', contract)
        self.assertEqual(context['evidence'], [])
        context['hypotheses'][0]['scope']['conditions'] = 'Changed only in a detached view.'
        self.assertEqual(state, original)

    def test_every_stage_receives_the_shared_delivery_and_evidence_rules(self):
        instructions = engine.prompts()
        self.assertEqual(set(instructions), {'search', 'hypotheses', 'admission', 'design', 'review'})
        for stage, system in instructions.items():
            with self.subTest(stage=stage):
                self.assertIn('以 requested_candidates 为准', system)
                self.assertIn('优先于 question 中的数量描述', system)
                self.assertIn('缺失的依据不能由ID或常识补写', system)
                self.assertIn('预算限制调用，不保证交付', system)

    def test_complete_reviewed_campaign_merges_research_without_promoting_evidence(self):
        config = fixture_config()
        config['version'] = '无磁钢 初筛 #1 50%'
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
        report = entry.write_report(root, config, result)
        self.assertEqual(report, root.parent / 'reports' / config['version'] / '候选报告.md')
        self.assertFalse((root / '候选报告.md').exists())
        self.assertIn("frozen_pending_measurement", report.read_text())
        for name in ('input.json', 'result.json'):
            link = f'../../{quote(config["version"], safe="")}/{name}'
            self.assertIn(f']({link})', report.read_text())
            self.assertTrue((report.parent / unquote(link)).is_file())
        original = report.read_bytes()
        with self.assertRaises(FileExistsError):
            entry.write_report(root, config, result)
        self.assertEqual(report.read_bytes(), original)

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

    def test_partial_llm_budgets_preserve_completed_stages_without_overspending(self):
        stages = ['search', 'hypotheses', 'admission', 'design']
        for budget in (0, 1, 3, 4):
            with self.subTest(llm_calls=budget):
                config = fixture_config()
                config['llm_calls'] = budget
                result, stub, root, _ = self.run_research(config)
                self.assertEqual(stub.stages, stages[:budget])
                self.assertEqual(result['usage']['llm_calls'], budget)
                self.assertEqual(result['status'], 'budget_exhausted')
                self.assertEqual(result['recommended_candidate_ids'], [])
                self.assertTrue((root / 'result.json').is_file())
                for stage in stages[:budget]:
                    self.assertTrue((root / 'round_01' / f'{stage}.json').is_file())
                self.assertEqual(len(result['delta']['nodes']), int(budget >= 3))
                self.assertEqual(stub.evaluations, int(budget == 4))
                if budget == 4:
                    evaluated = json.loads((root / 'round_01/evaluated.json').read_text())
                    self.assertEqual(len(evaluated['evidence']), 1)
                    self.assertEqual(result['delta']['nodes'][0]['kind'], 'hypothesis')

    def test_zero_literature_and_evaluation_budgets_skip_the_optional_work(self):
        class NoLiterature(ResearchServices):
            @staticmethod
            def literature(query):
                raise AssertionError('A zero literature budget must not call a service')

        for evaluations in (0, 1):
            with self.subTest(evaluations=evaluations):
                config = fixture_config()
                config.update(literature_queries=0, evaluations=evaluations, llm_calls=2 + 2 * evaluations)
                result, stub, root, _ = self.run_research(config, stub_type=NoLiterature)
                self.assertEqual(stub.stages, ['hypotheses', 'admission'] + ['design', 'review'] * evaluations)
                self.assertEqual(result['usage']['literature_queries'], 0)
                self.assertEqual(stub.evaluations, evaluations)
                self.assertEqual(json.loads((root / 'round_01/literature.json').read_text()), [])
                self.assertEqual(result['status'], 'frozen_pending_measurement' if evaluations else 'budget_exhausted')
                self.assertTrue(any(node['kind'] == 'hypothesis' for node in result['delta']['nodes']))
                if not evaluations:
                    self.assertEqual(result['rounds'][0]['status'], 'hypotheses_only')

    def test_three_rounds_can_deliver_more_candidates_than_one_round_evaluates(self):
        class AccumulatingResearch(ResearchServices):
            def ask(self, system, payload):
                answer = super().ask(system, payload)
                if self.stages[-1] == 'design':
                    mn = 3 + self.stages.count('design')
                    answer['designs'][0]['composition'] = {'Mn': mn}
                if 'evaluated' in payload:
                    answer['recommended_candidate_ids'] = [node['id']
                        for delta in (payload['campaign_pool'], payload['evaluated'])
                        for node in delta['nodes'] if node['kind'] == 'candidate']
                return answer

        config = fixture_config()
        config.update(rounds=3, llm_calls=15, recommendations=3)
        result, stub, root, _ = self.run_research(config, stub_type=AccumulatingResearch)
        self.assertEqual(result['status'], 'frozen_pending_measurement')
        self.assertEqual(stub.evaluations, 3)
        self.assertEqual(result['recommended_candidate_ids'],
                         [f'{config["version"]}-R{index:02}-C001' for index in (1, 2, 3)])
        self.assertEqual({node['composition']['Mn'] for node in result['delta']['nodes']
                          if node['kind'] == 'candidate'}, {4, 5, 6})
        review = json.loads((root / 'round_03/review_input.json').read_text())
        self.assertEqual(sum(node['kind'] == 'candidate' for node in review['campaign_pool']['nodes']), 2)

    def test_partial_endpoint_plans_are_research_but_not_final_delivery(self):
        class FocusedResearch(ResearchServices):
            def ask(self, system, payload):
                answer = super().ask(system, payload)
                if self.stages[-1] == 'design':
                    design = answer['designs'][0]
                    design['role'] = 'control'
                    design['plan']['measurements'] = ['phase']
                    answer['designs'].append({**design, 'composition': {'Mn': 6}})
                return answer

        result, stub, root, _ = self.run_research(stub_type=FocusedResearch)
        self.assertEqual(stub.evaluations, 1)
        self.assertEqual(len(json.loads((root / 'round_01/design.json').read_text())['designs']), 2)
        plans = [node for node in result['delta']['nodes'] if node['kind'] == 'experiment_plan']
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['measurements'], ['phase'])
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(result['recommended_candidate_ids'], [])
        self.assertTrue(any('目标端点' in reason for reason in result['delivery']['reasons']))

    def test_explicit_zero_does_not_make_a_second_hea_recipe(self):
        class SameRecipe(ResearchServices):
            def ask(self, system, payload):
                answer = super().ask(system, payload)
                if self.stages[-1] == 'design':
                    design = answer['designs'][0]
                    design['composition'] = {'Fe': 30, 'Co': 30, 'Ni': 40}
                    answer['designs'].append({**design, 'composition': {**design['composition'], 'Cr': 0}})
                return answer

        config = fixture_config()
        config.update(scenario=load('hea'), evaluations=2, recommendations=2)
        config['space'] = {'composition_ranges_wt_pct': {'Fe': [20, 40], 'Co': [20, 40], 'Ni': [20, 40]},
                           'protocol': {'process_text': 'Unknown', 'testing': {'test_temperature_C': 700}}}
        result, stub, _, _ = self.run_research(config, stub_type=SameRecipe)
        self.assertEqual(stub.evaluations, 2)
        self.assertEqual(result['recommended_candidate_ids'], [])
        self.assertTrue(any('当前 1 个' in reason for reason in result['delivery']['reasons']))

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
        report = entry.write_report(root, config, result)
        self.assertIn("v20261029.001-R01-L01", report.read_text())

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
            space.write_text(json.dumps({**fixture_config()["space"], 'notes': '由研究者选择的元素范围'}))
            version = '初筛 #1 50%'
            argv = ["--version", version, "--question", "研究弱磁材料", "--space", str(space),
                    "--scenario", 'weak_field_nonmagnetic_plate', "--prepare-only", "--evaluations", "0",
                    "--recommendations", "3", "--literature-queries", "0", "--llm-calls", "0"]
            with patch.object(entry, "ROOT", root), patch.object(engine, "run") as execute, \
                    patch.object(store, "load") as read_forest, patch.object(services, "_post") as external:
                self.assertEqual(entry.main(argv), 0)
                batch = root / 'outputs' / version
                original = (batch / 'input.json').read_bytes()
                config = json.loads(original)
                self.assertEqual(config['version'], version)
                self.assertEqual(config['space']['notes'], '由研究者选择的元素范围')
                self.assertEqual(config['recommendations'], 3)
                self.assertEqual(config['evaluations'], 0)
                self.assertEqual(config['literature_queries'], 0)
                self.assertEqual(config['llm_calls'], 0)
                with self.assertRaises(FileExistsError):
                    entry.main(argv)
                self.assertEqual((batch / 'input.json').read_bytes(), original)
                (root / 'outputs/reports/report-only').mkdir(parents=True)
                report_only = [value if value != version else 'report-only' for value in argv]
                with self.assertRaises(FileExistsError):
                    entry.main(report_only)
                self.assertFalse((root / 'outputs/report-only').exists())
            execute.assert_not_called()
            read_forest.assert_not_called()
            external.assert_not_called()

    def test_version_is_a_directory_name_and_cannot_escape_outputs(self):
        for version in ('', '   ', '.', '..', '/tmp/outside', 'nested/name', 'bad\0name'):
            with self.subTest(version=version):
                args = entry.arguments(['--version', version, '--question', '研究问题'])
                with self.assertRaisesRegex(ValueError, 'version'):
                    entry.configuration(args)

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
                rows = services.literature("Does the mechanism apply to this process?", top_k=64)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[2]['limit'], 64)
        self.assertEqual(post.call_args_list[1].args[2]['top_k'], 64)
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

    def test_large_requests_only_obey_an_explicit_user_limit(self):
        env = {'LITELLM_API_BASE': 'https://local.example/v1', 'LITELLM_API_KEY': 'test',
               'STEEL_FOREST_LLM_MODEL': 'test'}
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"accepted": true}'}}]}
        payload = {'context': 'x' * 130000}
        with patch.dict(os.environ, env, clear=True), patch.object(services, '_post', return_value=response) as post:
            self.assertEqual(services.ask('Decide', payload), {'accepted': True})
            os.environ['STEEL_FOREST_MAX_REQUEST_CHARS'] = ''
            self.assertEqual(services.ask('Decide', payload), {'accepted': True})
            os.environ['STEEL_FOREST_MAX_REQUEST_CHARS'] = '100'
            with self.assertRaisesRegex(services.ServiceError, 'STEEL_FOREST_MAX_REQUEST_CHARS'):
                services.ask('Decide', payload)
            self.assertEqual(post.call_count, 2)

    def test_json_fences_are_accepted_without_accepting_extra_prose(self):
        env = {'LITELLM_API_BASE': 'https://local.example/v1', 'LITELLM_API_KEY': 'test',
               'STEEL_FOREST_LLM_MODEL': 'test'}
        for content in ('```json\n{"accepted": true}\n```', '```\n{"accepted": true}\n```'):
            response = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
            with self.subTest(content=content), patch.dict(os.environ, env, clear=True), \
                    patch.object(services, '_post', return_value=response):
                self.assertEqual(services.ask('Decide', {}), {'accepted': True})
        response = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': 'Here is my decision:\n```json\n{"accepted": true}\n```'}}]}
        with patch.dict(os.environ, env, clear=True), patch.object(services, '_post', return_value=response):
            with self.assertRaisesRegex(services.ServiceError, 'JSON'):
                services.ask('Decide', {})


if __name__ == "__main__":
    unittest.main()
