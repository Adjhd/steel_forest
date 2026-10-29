"""按研究阶段组织普通函数；run 负责轮次、森林更新和结束判断。"""
from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path

from forest import store
from scenarios import complete_composition, validate_ranges, evaluate as evaluate_model
from campaign import search, services


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
                nodes.append(fields(node, 'id kind composition role hypothesis_id hypothesis_version plan_id evidence_ids sampled protocol_case_ids'))
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


def search_spaces(designs, hypotheses, config):
    """把科研设计转成数值空间；身份和计划留在模板中，不交给搜索算法。"""
    scenario = config['scenario']
    ids = {n['id']: n for n in hypotheses}
    spaces, templates = [], {}
    for index, design in enumerate(designs[:config['evaluations']], 1):
        if design['hypothesis_id'] not in ids:
            raise ValueError('候选必须绑定本轮已准入假设')
        if design['role'] not in ('target', 'control', 'discrimination_probe'):
            raise ValueError('候选角色必须是 target/control/discrimination_probe')
        if design['role'] == 'discrimination_probe':
            competitors = design['plan'].get('competing_hypothesis_ids', [])
            if len(set(competitors)) < 2 or not set(competitors) <= ids.keys() or not design['plan'].get('contrasting_predictions'):
                raise ValueError('区分探针需要至少两个已准入竞争假设及不同预测')
        validate_plan(design['plan'])
        bounds = ids[design['hypothesis_id']]['scope']['composition_ranges_wt_pct']
        item = {**design, 'composition': complete_composition(scenario, design['composition'], bounds)}
        region = {e: [value, value] for e, value in item['composition'].items()}
        if design.get('search_ranges_wt_pct'):
            varied = set(design['plan'].get('vary_elements', []))
            if not varied:
                raise ValueError('补充搜索必须声明 plan.vary_elements')
            region.update(design['search_ranges_wt_pct'])
            if scenario.get('balance'):
                region[scenario['balance']] = bounds[scenario['balance']]
            for element, interval in region.items():
                low, high = bounds.get(element, [0, 0])
                if len(interval) != 2 or not low <= interval[0] <= interval[1] <= high:
                    raise ValueError('候选搜索范围必须在已准入假设范围内')
                if interval[0] != interval[1] and element != scenario.get('balance') and element not in varied:
                    raise ValueError('补充搜索只能改变计划列出的元素')
            region = validate_ranges(scenario, region)
            # 显式种子也须属于它声明的搜索空间。
            complete_composition(scenario, item['composition'], region)
        sid = f'D{index:03}'
        templates[sid] = item
        spaces.append({'id': sid, 'bounds': region, 'seeds': [item['composition']],
                       'balance': scenario.get('balance'), 'total': 100,
                       'optimize': item['role'] == 'target'})
    return spaces, templates


def search_objectives(config):
    """每个条件独立建模；只有登记允许排序的指标参与 BO 和 Pareto。"""
    return [{**fields(metric, 'direction target op'), 'key': f'{name}/{metric["key"]}'}
            for name, case in config['space']['protocol_cases'].items()
            for metric in config['scenario']['metrics']
            if metric['key'] in case['metrics'] and metric['role'] == 'screening'
            and metric.get('ranking', {}).get('allowed', True)]


def evaluate_candidate(design, composition, config, folder, prefix, index, evaluate, usage, cache):
    """一个实际成分对应多条条件证据；完全相同的本批调用可引用已有结果。"""
    cid, pid = (f'{prefix}-{kind}{index:03}' for kind in ('C', 'P'))
    evidence, values = [], {}
    cases = config['space']['protocol_cases']
    usage['candidate_evaluations'] += 1
    save(folder.parent / 'usage.json', usage)
    versions = {tool['id']: tool['version'] for tool in config['scenario']['tools']}
    for case_index, (name, case) in enumerate(cases.items(), 1):
        eid = f'{prefix}-M{index:03}-K{case_index:02}'
        coordinates = {element: float(value) for element, value in composition.items() if value}
        key = json.dumps([coordinates, case['protocol'], {tid: versions[tid] for tid in case['tools']}], sort_keys=True)
        if key in cache:
            record = {**deepcopy(cache[key]), 'reused_from': cache[key]['id']}
        else:
            usage['model_evaluations'] += 1
            save(folder.parent / 'usage.json', usage)
            result = evaluate(config['scenario'], composition, case['protocol'], folder / cid / f'case_{case_index:02}',
                              tool_ids=case['tools'])
            record = {**result, 'kind': 'model', 'status': 'diagnostic',
                      'source': json.dumps(result['tools'], ensure_ascii=False),
                      'summary': '模型预测；不是实测，也不证明机理成立。',
                      'composition': composition, 'protocol': case['protocol']}
        record.update(id=eid, protocol_case_id=name)
        cache.setdefault(key, deepcopy(record))
        evidence.append(record)
        for metric, value in record['metrics'].items():
            value = value.get('value') if isinstance(value, dict) else value
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                values[f'{name}/{metric}'] = value
    common = {'scenario': config['scenario']['id'], 'version': 1, 'status': 'proposed'}
    candidate = {**common, 'id': cid, 'kind': 'candidate', 'statement': design['plan']['question'],
                 'composition': composition, 'role': design['role'], 'hypothesis_id': design['hypothesis_id'],
                 'hypothesis_version': 1, 'plan_id': pid, 'evidence_ids': [e['id'] for e in evidence],
                 'sampled': composition != design['composition'], 'protocol_case_ids': list(cases)}
    plan = {**design['plan'], **common, 'id': pid, 'kind': 'experiment_plan',
            'statement': design['plan']['question'], 'candidate_id': cid, 'protocol_case_ids': list(cases)}
    delta = {'nodes': [candidate, plan], 'evidence': evidence,
             'links': [{'source': design['hypothesis_id'], 'target': cid, 'relation': 'tested_by'},
                       {'source': cid, 'target': pid, 'relation': 'planned_by'}]}
    return delta, {'id': cid, 'metrics': values}


def search_candidates(designs, hypotheses, config, folder, prefix, evaluate, usage, cache, prior, round_index,
                      searcher=search.run):
    """数值搜索与科研记录的唯一接点；建议的形态不参与节点构造。"""
    spaces, templates = search_spaces(designs, hypotheses, config)
    bounds = {space['id']: space['bounds'] for space in spaces}
    delta = {'nodes': [], 'evidence': [], 'links': []}
    count = 0

    def predict(space_id, composition):
        nonlocal count
        if count >= config['evaluations']:
            raise ValueError('搜索回调超过本轮评估预算')
        composition = complete_composition(config['scenario'], composition, bounds[space_id])
        count += 1
        addition, observation = evaluate_candidate(templates[space_id], composition, config, folder, prefix,
                                                  count, evaluate, usage, cache)
        append_delta(delta, addition)
        save(folder / 'evaluated.json', delta)
        return observation

    options = {'seed': round_index, **config['space'].get('search', {})}
    result = searcher(spaces, predict, search_objectives(config), config['evaluations'], options=options, prior=prior)
    save(folder / 'search.json', result)
    save(folder / 'evaluated.json', delta)
    return delta, result


def delivery(review, delta, config):
    """按实际预测条件检查候选；不同条件的指标不能拼成一次达标。"""
    selected = review.get('recommended_candidate_ids', [])
    nodes, evidence = {n['id']: n for n in delta['nodes']}, {e['id']: e for e in delta['evidence']}
    scenario, cases = config['scenario'], config['space']['protocol_cases']
    reasons = []
    recipes = {tuple(sorted((e, v) for e, v in nodes[cid]['composition'].items() if v)) for cid in selected
               if cid in nodes and nodes[cid].get('kind') == 'candidate'}
    if len(recipes) < config['recommendations']:
        reasons.append(f'需要至少 {config["recommendations"]} 个不同成分的候选，当前 {len(recipes)} 个')
    screening = [m for m in scenario['metrics'] if m['role'] == 'screening' and m.get('target') is not None]
    available = {key for case in cases.values() for key in case['metrics']}
    missing = {m['key'] for m in screening} - available
    if missing:
        reasons.append(f'本批工具组合未覆盖名义筛选指标 {sorted(missing)}')
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
        by_case = {evidence[eid]['protocol_case_id']: evidence[eid] for eid in candidate['evidence_ids']}
        for name, case in cases.items():
            record = by_case.get(name)
            if (record is None or record['composition'] != candidate['composition']
                    or record['protocol'] != case['protocol']
                    or {t['id'] for t in record['tools']} != set(case['tools'])):
                reasons.append(f'{candidate_id}/{name}: 缺少相符成分、条件和工具的预测证据')
                continue
            for spec in screening:
                if spec['key'] not in case['metrics']:
                    continue
                metric = record['metrics'].get(spec['key'])
                value = metric.get('value') if isinstance(metric, dict) else metric
                target, op = spec['target'], spec['op']
                passed = value is not None and {'>=': lambda: value >= target, '>': lambda: value > target,
                           '<=': lambda: value <= target, '<': lambda: value < target}[op]()
                if not passed:
                    reasons.append(f'{candidate_id}/{name}: {spec["key"]} 未满足名义筛选')
    return {'accepted': not reasons, 'reasons': reasons, 'candidate_ids': selected if not reasons else []}


def candidate_pool(delta):
    """本批已接受的候选均可参与后续复审，候选数不受单轮预算限制。"""
    candidates = [n for n in delta['nodes'] if n['kind'] == 'candidate']
    ids = {n['id'] for n in candidates} | {n['plan_id'] for n in candidates}
    evidence_ids = {eid for node in candidates for eid in node['evidence_ids']}
    return {'nodes': [n for n in delta['nodes'] if n['id'] in ids],
            'evidence': [e for e in delta['evidence'] if e['id'] in evidence_ids], 'links': []}


def research_context(config):
    """各阶段共用的场景指标、元素范围与实际条件。"""
    scene = fields(config['scenario'], 'name composition_unit balance limitations')
    scene['metrics'] = [fields(metric, 'key label unit direction target op role qualification')
                        for metric in config['scenario']['metrics']]
    for metric, spec in zip(scene['metrics'], config['scenario']['metrics']):
        if spec.get('ranking'):
            metric['ranking'] = fields(spec['ranking'], 'allowed qualification physical_threshold_qualified')
    return {'scenario': scene, 'space': config['space']}


def plan_searches(call, research, context, limit):
    """让 LLM 从简短假设与反馈提出文献问题；零预算时跳过。"""
    if not limit:
        return []
    questions = [{**fields(node, 'id statement status falsifiers'), 'scope': fields(node['scope'],
                  'conditions composition_ranges_wt_pct material_system_id exposure_protocol process_id unknowns')}
                 for node in context['hypotheses']]
    bridges = [{**fields(link, 'source target relation'),
                'transfer_contract': fields(link.get('transfer_contract', {}),
                                            'shared_mechanism source_conditions target_conditions')}
               for link in context['links']]
    answer = call('queries', {**research, 'forest': {'hypotheses': questions, 'links': bridges},
                             'query_limit': limit})
    return answer['queries'][:limit]


def retrieve_literature(queries, literature, usage, folder, prefix):
    """执行查询、记录实际调用，为原始文献结果分配本轮 ID。"""
    sources = []
    for query in queries:
        usage['literature_queries'] += 1
        save(folder.parent / 'usage.json', usage)
        sources.extend(literature(query))
    for index, source in enumerate(sources, 1):
        source['id'] = f'{prefix}-L{index:02}'
    save(folder / 'literature.json', sources)
    return sources


def propose_hypotheses(call, research, context, sources, config, prefix):
    """结合森林与文献提出假设，只把被引用的新证据纳入提案。"""
    reading = {'hypotheses': context['hypotheses'], 'links': context['links'],
               'evidence': literature_reading(context['evidence'])}
    answer = call('hypotheses', {**research, 'forest': reading, 'sources': literature_reading(sources)})
    proposal = make_hypotheses(answer, sources, context, config, prefix)
    proposal['evidence'] = cited_evidence(proposal['nodes'], proposal['evidence'])
    return proposal


def review_hypotheses(call, research, proposal, context):
    """审查提议假设及其新旧依据，返回准入决定。"""
    source_ids = {sid for node in proposal['nodes'] for sid in node.get('source_hypothesis_ids', [])}
    source_hypotheses = [node for node in context['hypotheses'] if node['id'] in source_ids]
    return call('admission', {**research,
        'proposal': {'nodes': hypothesis_reading(proposal['nodes']),
                     'evidence': literature_reading(proposal['evidence'])},
        'forest': {'hypotheses': source_hypotheses,
                   'evidence': literature_reading(cited_evidence(proposal['nodes'] + source_hypotheses,
                                                                context['evidence']))}})


def design_candidates(call, research, hypotheses, evidence, config):
    """提出明确设计、对照和允许探索的范围，数值搜索单独执行。"""
    answer = call('design', {**research, 'hypotheses': hypothesis_reading(hypotheses),
        'literature': literature_reading(cited_evidence(hypotheses, evidence)),
        'tools': [{key: value for key, value in tool.items() if key not in {'asset', 'kind'}}
                  for tool in config['scenario']['tools']],
        'evaluations': config['evaluations']})
    return answer['designs']


def review_candidates(call, research, hypotheses, evidence, evaluated, previous_pool, search_result):
    """结合文献与真实预测，复审本轮结果及本批此前接受的候选。"""
    return call('review', {'scenario': research['scenario'],
        'protocol_cases': research['space']['protocol_cases'],
        'search': {'recommendations': search_result['recommendations'], 'trace': search_result['trace']},
        'hypotheses': hypothesis_reading(hypotheses),
        'literature': literature_reading(cited_evidence(hypotheses, evidence)),
        **review_reading(evaluated, previous_pool)})


def run(config, output, *, ask=services.ask, literature=services.literature,
        evaluate=evaluate_model, initial_state=None, searcher=search.run):
    """顺序执行研究阶段，管理森林增量、预算退出和交付结束。"""
    output = Path(output)
    state = store.load() if initial_state is None else initial_state
    base_revision = state['revision']
    instructions, total = prompts(), {'nodes': [], 'evidence': [], 'links': []}
    feedback, usage, nominations = {}, {'llm_calls': 0, 'candidate_evaluations': 0,
                                      'model_evaluations': 0, 'literature_queries': 0}, []
    status, checks = 'budget_exhausted', {'accepted': False, 'reasons': ['尚未交付']}
    rounds, search_history, cache = [], [], {}
    try:
        for index in range(1, config['rounds'] + 1):
            if usage['llm_calls'] >= config['llm_calls']:
                break
            folder = output / f'round_{index:02}'
            folder.mkdir()
            prefix = f'{config["version"]}-R{index:02}'
            context = store.context(config['scenario']['id'], limit=4, state=state)
            task = {'question': config['question'], 'requested_candidates': config['recommendations'], 'feedback': feedback}
            research = research_context(config)

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

            queries = plan_searches(call, research, context, config['literature_queries'])
            sources = retrieve_literature(queries, literature, usage, folder, prefix)
            proposal = propose_hypotheses(call, research, context, sources, config, prefix)
            admission = review_hypotheses(call, research, proposal, context)
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
            designs = design_candidates(call, research, admitted['nodes'], state['evidence'].values(), config)
            evaluated, search_result = search_candidates(designs, admitted['nodes'], config, folder, prefix,
                evaluate, usage, cache, search_history, index, searcher)
            search_history.extend(search_result['observations'])
            previous_pool = candidate_pool(total)
            prior_ids = {n['hypothesis_id'] for n in previous_pool['nodes'] if n['kind'] == 'candidate'}
            hypotheses = [n for n in total['nodes'] if n['id'] in prior_ids or n in admitted['nodes']]
            review = review_candidates(call, research, hypotheses, state['evidence'].values(),
                                       evaluated, previous_pool, search_result)
            approved = accepted(evaluated, review)
            state = store.apply(state, evaluated, review)
            append_delta(total, approved)
            nominations = review.get('recommended_candidate_ids', [])
            append_delta(previous_pool, approved)
            checks = delivery(review, previous_pool, config)
            save(folder / 'delivery.json', checks)
            feedback = {'reason': review['reason'], 'next_question': review.get('next_question'), 'delivery': checks}
            rounds.append({'round': index, 'evaluated': len(search_result['observations']), 'review': review, 'delivery': checks})
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
