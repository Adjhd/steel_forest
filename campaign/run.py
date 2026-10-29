"""命令行只负责读输入、建立版本目录、调用研究流程和写报告。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

from campaign import engine, services
from scenarios import load, validate_ranges, effective_protocol

ROOT = Path(__file__).resolve().parents[1]


def arguments(argv=None):
    parser = argparse.ArgumentParser(description='Steel Forest HF：假设优先的材料研究')
    parser.add_argument('--version', required=True, help='唯一版本，例如 v20261029.001')
    parser.add_argument('--scenario', default='nonmagnetic', choices=['corrosion', 'nonmagnetic', 'hea'])
    question = parser.add_mutually_exclusive_group()
    question.add_argument('--question', help='直接输入自然语言研究问题')
    question.add_argument('--question-file', type=Path, default=ROOT / 'campaign/question.md')
    parser.add_argument('--space', type=Path, default=ROOT / 'campaign/space.json')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--evaluations', type=int, default=12, help='每轮最多预测的成分数')
    parser.add_argument('--literature-queries', type=int, default=2, help='每轮文献查询数；每个已配置服务各调用一次')
    parser.add_argument('--llm-calls', type=int, default=15, help='本批语言模型请求上限，不自动重试')
    parser.add_argument('--recommendations', type=int, default=3, help='最终至少交付的候选数')
    parser.add_argument('--prepare-only', action='store_true', help='仅装配输入，不读取森林、不调用模型与服务')
    parser.add_argument('--keep-private', action='store_true', help='保留研究结果，暂不合入canonical')
    return parser.parse_args(argv)


def configuration(args):
    if not re.fullmatch(r'v\d{8}\.\d{3}', args.version):
        raise ValueError('version 格式必须是 vYYYYMMDD.NNN，例如 v20261029.001')
    datetime.strptime(args.version[1:9], '%Y%m%d')
    for name in ('rounds', 'evaluations', 'literature_queries', 'llm_calls', 'recommendations'):
        if getattr(args, name) < 1:
            raise ValueError(f'{name} 必须大于0')
    if args.llm_calls < 5:
        raise ValueError('一个完整研究轮次需要至少5次LLM请求预算')
    if args.recommendations > args.evaluations:
        raise ValueError('最终候选数不能超过每轮成分评估数')
    scene = load(args.scenario)
    space = json.loads(args.space.read_text())
    if set(space) - {'composition_ranges_wt_pct', 'protocol'}:
        raise ValueError('space 只需 composition_ranges_wt_pct 和 protocol')
    space['composition_ranges_wt_pct'] = validate_ranges(scene, space['composition_ranges_wt_pct'])
    space['protocol'] = effective_protocol(scene, space.get('protocol', {}))
    question = args.question if args.question is not None else args.question_file.read_text()
    if not question.strip():
        raise ValueError('研究问题不能为空')
    return {'version': args.version, 'scenario': scene, 'question': question.strip(), 'space': space,
            'rounds': args.rounds, 'evaluations': args.evaluations, 'literature_queries': args.literature_queries,
            'llm_calls': args.llm_calls, 'recommendations': args.recommendations, 'merge': not args.keep_private}


def write_report(output, config, result):
    """报告只引用真实产物；自由文本科研解释保留在逐轮审查意见里。"""
    delta = result['delta']
    nodes = {node['id']: node for node in delta['nodes']}
    evidence = {record['id']: record for record in delta['evidence']}
    selected = result['recommended_candidate_ids']
    lines = [f'# {config["scenario"]["name"]}候选报告', '',
             f'批次：`{config["version"]}`；状态：`{result["status"]}`。', '',
             config['question'], '', f'通过交付检查的候选：**{len(selected)}**。', '',
             '性能数值均为模型输出。待测计划不代表已取得实测结果；预算结束不代表科学收敛。', '',
             f'实际调用：LLM {result["usage"]["llm_calls"]} 次；文献查询 {result["usage"]["literature_queries"]} 次；成分评估 {result["usage"]["model_evaluations"]} 次。', '',
             '完整输入见 [input.json](input.json)，研究记录见 [result.json](result.json)。', '']
    if not selected:
        lines.extend(['本批没有形成通过交付检查的最终推荐；中间候选保存在各轮目录中。', ''])
    for candidate_id in selected:
        node = nodes[candidate_id]
        diagnostic = evidence[node['evidence_ids'][0]]
        plan = nodes[node['plan_id']]
        hypothesis = nodes[node['hypothesis_id']]
        lines.extend([f'## {candidate_id}', '',
                      f'假设：`{node["hypothesis_id"]}@r{node["hypothesis_version"]}`；角色：`{node["role"]}`。', '',
                      hypothesis['statement'], '',
                      f'适用条件：{hypothesis["scope"].get("conditions", "见完整假设记录")}', '',
                      '成分（wt.%；预测使用下列完整精度）：', '',
                      '；'.join(f'{element} {value!r}' for element, value in node['composition'].items()), '',
                      '| 指标 | 模型输出 | 单位 |', '|---|---:|---|'])
        for key, metric in diagnostic['metrics'].items():
            if isinstance(metric, dict):
                lines.append(f'| {key} | {metric.get("value", "未知")} | {metric.get("unit", "")} |')
        lines.extend(['', '模型预测使用的条件：`' + json.dumps(diagnostic['protocol'], ensure_ascii=False) + '`。', '',
                      f'待实测工艺计划：{plan["process"]}', '', f'研究问题：{plan["question"]}', '',
                      f'干预：{plan["intervention"]}；控制：{plan["controls"]}', '',
                      f'测量：{", ".join(plan["measurements"])}', '',
                      f'支持：{plan["support"]}', '', f'削弱：{plan["weaken"]}', '', f'未决：{plan["unresolved"]}', ''])
        for eid in hypothesis.get('evidence_ids', []):
            record = evidence.get(eid)
            if record:
                lines.extend([f'文献线索 `{eid}`（待原文核验）：{record["source"]}', ''])
    lines.extend(['## 适用边界', ''])
    lines.extend(f'- {item}' for item in config['scenario']['limitations'])
    lines.extend(['', '## 逐轮结论', ''])
    for row in result['rounds']:
        lines.append(f'- 第{row["round"]}轮：{row.get("review", {}).get("reason", row.get("status"))}')
    (output / '候选报告.md').write_text('\n'.join(lines) + '\n')


def main(argv=None):
    args = arguments(argv)
    config = configuration(args)
    output = ROOT / 'outputs' / args.version
    output.mkdir(parents=True, exist_ok=False)
    engine.save(output / 'input.json', config)
    (output / 'question.md').write_text(config['question'] + '\n')
    (output / 'prompts.md').write_text(Path(__file__).with_name('prompts.md').read_text())
    if args.prepare_only:
        engine.save(output / 'status.json', {'status': 'prepared', 'version': args.version})
        print(f'输入已装配：{output}\n该版本已保留；真实运行请使用下一个版本号。')
        return 0
    engine.save(output / 'status.json', {'status': 'running', 'version': args.version})
    try:
        result = engine.run(config, output)
        write_report(output, config, result)
    except Exception as error:
        engine.save(output / 'status.json', {'status': 'failed', 'error': str(error), 'type': type(error).__name__})
        raise
    engine.save(output / 'status.json', {'status': result['status'], 'version': args.version})
    print(f'{result["status"]}：{output / "候选报告.md"}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, FileExistsError, FileNotFoundError, services.ServiceError) as error:
        sys.exit(str(error))
