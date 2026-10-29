"""一轮研究按五个步骤走完；状态和产物都是普通字典。"""
from __future__ import annotations

import json
import random
from pathlib import Path

from forest import store
from scenarios import complete_composition, validate_ranges, evaluate as evaluate_model
from campaign import services


class BudgetExhausted(Exception):
    """LLM 预算用完；结束本批并保存已经完成的阶段。"""


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def prompts():
    text = Path(__file__).with_name('prompts.md').read_text()
    preface, *stages = text.split('\n## ')
    rules = preface.split('\n', 1)[1].strip()
    return {part.split('\n', 1)[0].strip(): rules + '\n\n' + part.split('\n', 1)[1]
            for part in stages}


def fields(record, names):
    """从记录中选取阅读所需字段；完整记录仍原样保存。"""
    return {key: record[key] for key in names.split() if key in record}


def literature_reading(records):
    """文献正文只呈现一次，保留出处、适用条件和核验状态。"""
    result = []
    for record in records:
        source = record.get('record', record)
        item = fields(source, 'source_id title url source provider query authoritative_original status')
        if isinstance(item.get('source'), dict):
            item['source'] = fields(item['source'], 'title uri url doi source_id authoritative_original')
        item.update(fields(record, 'id kind status scope applicability quote'))
        item['text'] = source.get('text') or record.get('summary') or record.get('statement', '')
        result.append(item)
    return result


def hypothesis_reading(nodes):
    return [fields(node, 'id revision scenario status statement scope predictions falsifiers unresolved '
                         'evidence_ids source_hypothesis_ids transfer_contract source_refs') for node in nodes]


def cited_evidence(nodes, records):
    ids = {eid for node in nodes for eid in node.get('evidence_ids', [])}
    return [record for record in records if record['id'] in ids]


def review_reading(evaluated, previous):
    """完整候选池的阅读视图；相同模型条件和计划正文只存一份。"""
    models, plans, views = {}, {}, []

    def reference(table, value):
        for key, existing in table.items():
            if existing == value:
                return key
        key = str(len(table) + 1)
        table[key] = value
        return key

    for delta in (evaluated, previous):
        nodes, evidence = [], []
        recipes = {eid: node['composition'] for node in delta['nodes'] if node['kind'] == 'candidate'
                   for eid in node['evidence_ids']}
        for node in delta['nodes']:
            if node['kind'] == 'candidate':
                nodes.append(fields(node, 'id kind composition role hypothesis_id hypothesis_version plan_id evidence_ids sampled'))
            else:
                details = {key: value for key, value in node.items()
                           if key not in {'id', 'kind', 'scenario', 'version', 'revision', 'version_id',
                                          'updated_at', 'status', 'scientific_result', 'candidate_id', 'statement'}}
                nodes.append({**fields(node, 'id kind candidate_id'), 'plan_details': reference(plans, details)})
        for record in delta['evidence']:
            shared = fields(record, 'protocol limitations qualification')
            shared['tools'] = [{key: value for key, value in tool.items() if key not in {'asset', 'kind'}}
                               for tool in record['tools']]
            item = {key: value for key, value in record.items()
                    if key not in {'source', 'summary', 'composition', 'protocol', 'limitations',
                                   'qualification', 'tools', 'raw', 'record'}}
            item['model_context'] = reference(models, shared)
            if record.get('composition') != recipes.get(record['id']):
                item['composition'] = record.get('composition')
            evidence.append(item)
        views.append({'nodes': nodes, 'evidence': evidence})
    return {'evaluated': views[0], 'campaign_pool': views[1], 'model_context': models, 'plan_details': plans}


def accepted(delta, review, existing_evidence=()):
    """审查只选已有记录，不能通过审查意见凭空补证据。"""
    node_ids, evidence_ids = set(review['accepted_node_ids']), set(review['accepted_evidence_ids'])
    known_nodes = {n['id'] for n in delta['nodes']}
    known_evidence = {e['id'] for e in delta['evidence']}
    if not node_ids <= known_nodes or not evidence_ids <= known_evidence | set(existing_evidence):
        raise ValueError('审查引用了本阶段不存在的节点或证据')
    return {'nodes': [n for n in delta['nodes'] if n['id'] in node_ids],
            'evidence': [e for e in delta['evidence'] if e['id'] in evidence_ids],
            'links': delta.get('links', [])}


def append_delta(total, addition):
    for name in ('nodes', 'evidence', 'links'):
        total[name].extend(addition[name])


def make_hypotheses(answer, sources, context, config, prefix):
    evidence = [{'id': f'{prefix}-L{i:02}', 'kind': 'literature',
                 'source': row.get('url') or row.get('source') or row.get('source_id'),
                 'summary': row.get('text') or row.get('title', ''),
                 'status': 'lead', 'record': row} for i, row in enumerate(sources, 1)]
    # ID 在提问前已赋予；这里沿用，保持引用稳定。
    for item, row in zip(evidence, sources):
        item['id'] = row['id']
    nodes, links = [], []
    known_evidence = {e['id'] for e in evidence}
    inherited = context.get('evidence', [])
    known_evidence.update(e['id'] for e in (inherited.values() if isinstance(inherited, dict) else inherited))
    inherited_nodes = context.get('hypotheses', [])
    inherited_by_id = {n['id']: n for n in (inherited_nodes.values() if isinstance(inherited_nodes, dict) else inherited_nodes)}
    inherited_ids = inherited_by_id.keys()
    for index, hypothesis in enumerate(answer['hypotheses'], 1):
        for field in ('statement', 'scope', 'predictions', 'falsifiers', 'unresolved'):
            if not hypothesis.get(field):
                raise ValueError(f'假设缺少 {field}')
        if not set(hypothesis.get('evidence_ids', [])) <= known_evidence:
            raise ValueError('假设引用了未读取的证据')
        if not set(hypothesis.get('source_hypothesis_ids', [])) <= inherited_ids:
            raise ValueError('假设引用了未读取的森林节点')
        bounds = validate_ranges(config['scenario'], hypothesis['scope']['composition_ranges_wt_pct'])
        task_bounds = config['space']['composition_ranges_wt_pct']
        for element in bounds.keys() | task_bounds.keys():
            lo, hi = bounds.get(element, [0, 0])
            task_lo, task_hi = task_bounds.get(element, [0, 0])
            if not task_lo <= lo <= hi <= task_hi:
                raise ValueError('假设范围必须在任务范围内')
        hypothesis['scope']['composition_ranges_wt_pct'] = bounds
        node = {**hypothesis, 'id': f'{prefix}-H{index:02}', 'kind': 'hypothesis',
                'scenario': config['scenario']['id'], 'version': 1, 'status': 'proposed',
                'verification_plan': {'support': hypothesis['predictions'], 'weaken': hypothesis['falsifiers'],
                                      'unresolved': hypothesis['unresolved']}}
        nodes.append(node)
        for source in hypothesis.get('source_hypothesis_ids', []):
            link = {'source': source, 'target': node['id'], 'relation': 'inspired_by'}
            if inherited_by_id[source]['scenario'] != config['scenario']['id']:
                contract = hypothesis.get('transfer_contract', {})
                if not all(contract.get(key) for key in ('source_conditions', 'target_conditions', 'shared_mechanism', 'verification')):
                    raise ValueError('跨场景启发需要明确条件差异、共同机理及验证方法')
                link.update(relation='transfer_candidate', transfer_contract=contract)
            links.append(link)
    return {'nodes': nodes, 'evidence': evidence, 'links': links}


def validate_plan(plan):
    for field in ('question', 'intervention', 'controls', 'measurements', 'support', 'weaken', 'unresolved', 'process'):
        if not plan.get(field):
            raise ValueError(f'实验计划缺少 {field}')
    if not isinstance(plan['measurements'], list):
        raise ValueError('measurements 必须是端点列表')


def expand_designs(designs, hypotheses, config, round_index):
    """先保留明确设计，再在假设声明的子范围内补充采样。"""
    scenario = config['scenario']
    ids = {n['id']: n for n in hypotheses}
    result, regions = [], []
    for design in designs[:config['evaluations']]:
        if design['hypothesis_id'] not in ids:
            raise ValueError('候选必须绑定本轮已准入假设')
        if design['role'] not in ('target', 'control', 'discrimination_probe'):
            raise ValueError('候选角色必须是 target/control/discrimination_probe')
        if design['role'] == 'discrimination_probe':
            competitors = design['plan'].get('competing_hypothesis_ids', [])
            if len(set(competitors)) < 2 or not set(competitors) <= ids.keys() or not design['plan'].get('contrasting_predictions'):
                raise ValueError('区分探针需要至少两个已准入竞争假设及不同预测')
        validate_plan(design['plan'])
        hypothesis_ranges = ids[design['hypothesis_id']]['scope']['composition_ranges_wt_pct']
        item = dict(design)
        item['composition'] = complete_composition(scenario, design['composition'], hypothesis_ranges)
        result.append(item)
        if design.get('search_ranges_wt_pct'):
            allowed_variation = set(design['plan'].get('vary_elements', []))
            if not allowed_variation:
                raise ValueError('补充搜索必须声明 plan.vary_elements')
            region = {e: [value, value] for e, value in item['composition'].items()}
            region.update(design['search_ranges_wt_pct'])
            if scenario.get('balance'):
                region[scenario['balance']] = hypothesis_ranges[scenario['balance']]
            for element, bounds in region.items():
                low, high = hypothesis_ranges.get(element, [0, 0])
                if len(bounds) != 2 or not low <= bounds[0] <= bounds[1] <= high:
                    raise ValueError('候选搜索范围必须在已准入假设范围内')
                if bounds[0] != bounds[1] and element != scenario.get('balance') and element not in allowed_variation:
                    raise ValueError('补充搜索只能改变计划列出的元素')
            region = validate_ranges(scenario, region)
            regions.append((item, region))
    rng = random.Random(round_index)
    seen = {tuple(sorted((e, v) for e, v in d['composition'].items() if v)) for d in result}
    for attempt in range(config['evaluations'] * 100):
        if not regions or len(result) >= config['evaluations']:
            break
        template, region = regions[attempt % len(regions)]
        point = {e: rng.uniform(*bounds) for e, bounds in region.items()}
        if scenario['id'] != 'high_temperature_high_entropy_alloy':
            point.pop('Fe', None)
        else:
            # HEA 从区间下界逐次分配剩余总量，不指定某种元素作基体。
            point = {e: bounds[0] for e, bounds in region.items()}
            elements = list(point)
            rng.shuffle(elements)
            remaining = 100 - sum(point.values())
            for i, element in enumerate(elements):
                capacity = region[element][1] - point[element]
                later = sum(region[e][1] - point[e] for e in elements[i + 1:])
                addition = rng.uniform(max(0, remaining - later), min(capacity, remaining))
                point[element] += addition
                remaining -= addition
        try:
            point = complete_composition(scenario, point, region)
        except ValueError:
            continue  # 有总量约束的区间并不保证每次随机采样都可行。
        key = tuple(sorted((e, v) for e, v in point.items() if v))
        if key not in seen:
            result.append({**template, 'composition': point, 'sampled': True})
            seen.add(key)
    return result


def evaluate_designs(designs, config, folder, prefix, evaluate, usage):
    nodes, evidence, links = [], [], []
    for index, design in enumerate(designs, 1):
        cid, pid, eid = (f'{prefix}-{kind}{index:03}' for kind in ('C', 'P', 'M'))
        usage['model_evaluations'] += 1
        save(folder.parent / 'usage.json', usage)
        result = evaluate(config['scenario'], design['composition'], config['space']['protocol'], folder / cid)
        evidence.append({'id': eid, 'kind': 'model', 'status': 'diagnostic',
                         'source': json.dumps(result['tools'], ensure_ascii=False),
                         'summary': '模型预测；不是实测，也不证明机理成立。',
                         'composition': design['composition'], 'protocol': config['space']['protocol'], **result})
        common = {'scenario': config['scenario']['id'], 'version': 1, 'status': 'proposed'}
        nodes.append({**common, 'id': cid, 'kind': 'candidate', 'statement': design['plan']['question'],
                      'composition': design['composition'], 'role': design['role'],
                      'hypothesis_id': design['hypothesis_id'], 'hypothesis_version': 1,
                      'plan_id': pid, 'evidence_ids': [eid], 'sampled': design.get('sampled', False)})
        nodes.append({**design['plan'], **common, 'id': pid, 'kind': 'experiment_plan',
                      'statement': design['plan']['question'], 'candidate_id': cid})
        links.extend([{'source': design['hypothesis_id'], 'target': cid, 'relation': 'tested_by'},
                      {'source': cid, 'target': pid, 'relation': 'planned_by'}])
        save(folder / 'evaluated.json', {'nodes': nodes, 'evidence': evidence, 'links': links})
    return {'nodes': nodes, 'evidence': evidence, 'links': links}


def delivery(review, delta, scenario, minimum):
    """独立于语言模型：提名必须有已接受的预测与完整实验计划。"""
    selected = review.get('recommended_candidate_ids', [])
    nodes, evidence = {n['id']: n for n in delta['nodes']}, {e['id']: e for e in delta['evidence']}
    reasons = []
    recipes = {tuple(sorted((e, v) for e, v in nodes[cid]['composition'].items() if v)) for cid in selected
               if cid in nodes and nodes[cid].get('kind') == 'candidate'}
    if len(recipes) < minimum:
        reasons.append(f'需要至少 {minimum} 个不同成分的候选，当前 {len(recipes)} 个')
    for candidate_id in selected:
        candidate = nodes.get(candidate_id, {})
        plan = nodes.get(candidate.get('plan_id'))
        if candidate.get('kind') != 'candidate' or plan is None:
            reasons.append(f'{candidate_id}: 候选或计划未接受')
            continue
        if not candidate.get('evidence_ids') or not set(candidate['evidence_ids']) <= set(evidence):
            reasons.append(f'{candidate_id}: 预测证据未接受')
            continue
        required = {m['key'] for m in scenario['metrics'] if m.get('target') is not None}
        missing = required - set(plan['measurements'])
        if missing:
            reasons.append(f'{candidate_id}: 最终计划未覆盖目标端点 {sorted(missing)}')
        # 只检查登记为 screening 的名义门槛；diagnostic/measurement 不参与预测筛除。
        metrics = evidence[candidate['evidence_ids'][0]]['metrics']
        for spec in scenario['metrics']:
            if spec.get('role') != 'screening' or spec.get('target') is None:
                continue
            metric = metrics.get(spec['key'], {})
            value = metric.get('value') if isinstance(metric, dict) else metric
            target, op = spec['target'], spec['op']
            passed = value is not None and {'>=': lambda: value >= target, '>': lambda: value > target,
                       '<=': lambda: value <= target, '<': lambda: value < target}[op]()
            if not passed:
                reasons.append(f'{candidate_id}: {spec["key"]} 未满足名义筛选')
    return {'accepted': not reasons, 'reasons': reasons, 'candidate_ids': selected if not reasons else []}


def candidate_pool(delta):
    """本批已接受的候选均可参与后续复审，候选数不受单轮预算限制。"""
    candidates = [n for n in delta['nodes'] if n['kind'] == 'candidate']
    ids = {n['id'] for n in candidates} | {n['plan_id'] for n in candidates}
    evidence_ids = {eid for node in candidates for eid in node['evidence_ids']}
    return {'nodes': [n for n in delta['nodes'] if n['id'] in ids],
            'evidence': [e for e in delta['evidence'] if e['id'] in evidence_ids], 'links': []}


def run(config, output, *, ask=services.ask, literature=services.literature,
        evaluate=evaluate_model, initial_state=None):
    output = Path(output)
    state = store.load() if initial_state is None else initial_state
    base_revision = state['revision']
    instructions, total = prompts(), {'nodes': [], 'evidence': [], 'links': []}
    feedback, usage, nominations = {}, {'llm_calls': 0, 'model_evaluations': 0, 'literature_queries': 0}, []
    status, checks = 'budget_exhausted', {'accepted': False, 'reasons': ['尚未交付']}
    rounds = []
    try:
        for index in range(1, config['rounds'] + 1):
            if usage['llm_calls'] >= config['llm_calls']:
                break
            folder = output / f'round_{index:02}'
            folder.mkdir()
            prefix = f'{config["version"]}-R{index:02}'
            context = store.context(config['scenario']['id'], limit=4, state=state)
            task = {'question': config['question'], 'requested_candidates': config['recommendations'], 'feedback': feedback}
            scene = fields(config['scenario'], 'name composition_unit balance limitations')
            scene['metrics'] = [fields(metric, 'key label unit direction target op role qualification')
                                for metric in config['scenario']['metrics']]
            for metric, spec in zip(scene['metrics'], config['scenario']['metrics']):
                if spec.get('ranking'):
                    metric['ranking'] = fields(spec['ranking'], 'allowed qualification physical_threshold_qualified')
            research = {'scenario': scene, 'space': config['space']}

            def call(stage, extra):
                if usage['llm_calls'] >= config['llm_calls']:
                    raise BudgetExhausted(stage)
                payload = {**task, **extra}
                save(folder / f'{stage}_input.json', payload)
                usage['llm_calls'] += 1
                save(output / 'usage.json', usage)
                answer = ask(instructions[stage], payload)
                save(folder / f'{stage}.json', answer)
                return answer

            sources = []
            queries = []
            if config['literature_queries']:
                questions = [{**fields(node, 'id statement status falsifiers'), 'scope': fields(node['scope'],
                              'conditions composition_ranges_wt_pct material_system_id exposure_protocol process_id unknowns')}
                             for node in context['hypotheses']]
                bridges = [{**fields(link, 'source target relation'),
                            'transfer_contract': fields(link.get('transfer_contract', {}),
                                                        'shared_mechanism source_conditions target_conditions')}
                           for link in context['links']]
                queries = call('search', {**research, 'forest': {'hypotheses': questions, 'links': bridges},
                                         'query_limit': config['literature_queries']})['queries']
            for query in queries[:config['literature_queries']]:
                usage['literature_queries'] += 1
                save(output / 'usage.json', usage)
                sources.extend(literature(query))
            for i, source in enumerate(sources, 1):
                source['id'] = f'{prefix}-L{i:02}'
            save(folder / 'literature.json', sources)
            reading = {'hypotheses': context['hypotheses'], 'links': context['links'],
                       'evidence': literature_reading(context['evidence'])}
            proposal = make_hypotheses(call('hypotheses', {**research, 'forest': reading,
                                      'sources': literature_reading(sources)}), sources, context, config, prefix)
            proposal['evidence'] = cited_evidence(proposal['nodes'], proposal['evidence'])
            source_ids = {sid for node in proposal['nodes'] for sid in node.get('source_hypothesis_ids', [])}
            source_hypotheses = [node for node in reading['hypotheses'] if node['id'] in source_ids]
            admission = call('admission', {**research,
                'proposal': {'nodes': hypothesis_reading(proposal['nodes']),
                             'evidence': literature_reading(proposal['evidence'])},
                'forest': {'hypotheses': source_hypotheses,
                           'evidence': literature_reading(cited_evidence(proposal['nodes'] + source_hypotheses,
                                                                        context['evidence']))}})
            inherited_evidence = {eid for node in proposal['nodes'] for eid in node.get('evidence_ids', [])
                                  if eid in state['evidence']}
            admitted = accepted(proposal, admission, inherited_evidence)
            state = store.apply(state, proposal, {**admission,
                                'accepted_evidence_ids': [e['id'] for e in admitted['evidence']]})
            append_delta(total, admitted)
            if not admitted['nodes']:
                feedback = {'reason': admission['reason'], 'next_question': '修正假设与证据'}
                rounds.append({'round': index, 'status': 'hypotheses_rejected'})
                continue
            if not config['evaluations']:
                feedback = {'reason': admission['reason'], 'next_question': '继续研究假设与证据'}
                rounds.append({'round': index, 'status': 'hypotheses_only'})
                continue
            designs = call('design', {**research, 'hypotheses': hypothesis_reading(admitted['nodes']),
                'literature': literature_reading(cited_evidence(admitted['nodes'], state['evidence'].values())),
                'tools': [{key: value for key, value in tool.items() if key not in {'asset', 'kind'}}
                          for tool in config['scenario']['tools']],
                'evaluations': config['evaluations']})
            points = expand_designs(designs['designs'], admitted['nodes'], config, index)
            evaluated = evaluate_designs(points, config, folder, prefix, evaluate, usage)
            previous_pool = candidate_pool(total)
            prior_ids = {n['hypothesis_id'] for n in previous_pool['nodes'] if n['kind'] == 'candidate'}
            review_hypotheses = [n for n in total['nodes'] if n['id'] in prior_ids or n in admitted['nodes']]
            review = call('review', {'scenario': scene, 'hypotheses': hypothesis_reading(review_hypotheses),
                'literature': literature_reading(cited_evidence(review_hypotheses, state['evidence'].values())),
                **review_reading(evaluated, previous_pool)})
            approved = accepted(evaluated, review)
            state = store.apply(state, evaluated, review)
            append_delta(total, approved)
            nominations = review.get('recommended_candidate_ids', [])
            append_delta(previous_pool, approved)
            checks = delivery(review, previous_pool, config['scenario'], config['recommendations'])
            save(folder / 'delivery.json', checks)
            feedback = {'reason': review['reason'], 'next_question': review.get('next_question'), 'delivery': checks}
            rounds.append({'round': index, 'evaluated': len(points), 'review': review, 'delivery': checks})
            save(output / 'progress.json', {'usage': usage, 'rounds': rounds})
            if review.get('finish') is True and checks['accepted']:
                status = 'frozen_pending_measurement'
                break
    except BudgetExhausted as stage:
        rounds.append({'round': index, 'status': 'budget_exhausted', 'next_stage': str(stage)})
    save(output / 'progress.json', {'usage': usage, 'rounds': rounds})
    review = {'accepted_node_ids': [n['id'] for n in total['nodes']],
              'accepted_evidence_ids': [e['id'] for e in total['evidence']],
              'reason': '各轮科学准入与研究审查的已接受记录', 'reviewer': 'campaign_scientific_review'}
    save(output / 'forest_delta.json', total)
    save(output / 'forest_review.json', review)
    result = {'version': config['version'], 'status': status, 'base_revision': base_revision,
              'usage': usage, 'rounds': rounds, 'nominations': nominations,
              'recommended_candidate_ids': checks.get('candidate_ids', []) if status == 'frozen_pending_measurement' else [],
              'delivery': checks, 'delta': total}
    save(output / 'usage.json', usage)
    save(output / 'result.json', result)
    if config.get('merge') and total['nodes']:
        merged = store.merge(total, review, expected_revision=base_revision)
        result['canonical_merge'] = {'revision': merged['revision'], 'node_count': len(merged['nodes'])}
    save(output / 'result.json', result)
    return result
