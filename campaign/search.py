"""可独立编辑的数值搜索：空间覆盖、约束 BO、代表选择。

run 只认识范围、数值指标和 evaluate(space_id, point) 回调；不读写文件，
不创建假设、候选 ID 或实验计划。回调返回 id、metrics 及需要保留的附加信息。
recommend 单独决定建议的形态；当前返回点，未来可在这里改成有观测依据的范围。
"""
from __future__ import annotations

import numpy as np
from scipy.linalg import cho_factor, cho_solve
from scipy.optimize import minimize_scalar
from scipy.spatial.distance import cdist
from scipy.special import ndtr
from scipy.stats import qmc


DEFAULTS = {
    'method': 'auto', 'pool_size': 256, 'initial': 6, 'batch_size': 4,
    'exploration_fraction': 0.25, 'representatives': None, 'seed': 0,
}


def settings(options=None):
    """这些是可调数值参数，不是科研准入条件。"""
    result = {**DEFAULTS, **(options or {})}
    if result['method'] not in ('auto', 'bo', 'space_filling'):
        raise ValueError('search.method 应为 auto、bo 或 space_filling')
    for name in ('pool_size', 'initial', 'batch_size'):
        if not isinstance(result[name], int) or isinstance(result[name], bool) or result[name] < 1:
            raise ValueError(f'search.{name} 应为正整数')
    count = result['representatives']
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        raise ValueError('search.representatives 应为非负整数或 null')
    if not 0 <= result['exploration_fraction'] <= 1:
        raise ValueError('search.exploration_fraction 应在 0–1 之间')
    return result


def _key(point):
    return tuple(sorted((name, round(float(value), 10)) for name, value in point.items()
                        if round(float(value), 10)))


def _features(points, space):
    names = [name for name, (low, high) in space['bounds'].items()
             if high > low and name != space.get('balance')]
    if not names:
        return np.zeros((len(points), 1))
    return np.asarray([[(point.get(name, 0) - space['bounds'][name][0]) /
                        (space['bounds'][name][1] - space['bounds'][name][0])
                        for name in names] for point in points], dtype=float)


def _close(points, low, high, total, balance=None):
    """投影到区间内的总量平面；有余量元素时优先仅调整该元素。"""
    points = np.clip(np.atleast_2d(points), low, high).copy()
    active = np.arange(len(low)) != balance if balance is not None else np.ones(len(low), dtype=bool)
    target = np.full(len(points), total, dtype=float)
    if balance is not None:
        points[:, balance] = np.clip(total - points[:, active].sum(axis=1), low[balance], high[balance])
        target -= points[:, balance]
    values, lower, upper = points[:, active], low[active], high[active]
    left, right = np.full(len(points), -1.0), np.full(len(points), 1.0)
    # 用区间宽度分配修正，固定维保持原值；二分精度只服务于成分闭合。
    for _ in range(48):
        middle = (left + right) / 2
        sums = np.clip(values + middle[:, None] * (upper - lower), lower, upper).sum(axis=1)
        left = np.where(sums < target, middle, left)
        right = np.where(sums >= target, middle, right)
    points[:, active] = np.clip(values + ((left + right) / 2)[:, None] * (upper - lower), lower, upper)
    return points


def space_filling(space, count, rng):
    """LHS 内部点＋可行边界点；后续用 maximin 挑出分散的点。"""
    names = list(space['bounds'])
    low, high = np.asarray(list(space['bounds'].values()), dtype=float).T
    total = space.get('total', 100)
    if low.sum() > total + 1e-8 or high.sum() < total - 1e-8:
        raise ValueError('搜索范围无法闭合到声明的总量')
    balance = names.index(space['balance']) if space.get('balance') else None
    raw = qmc.LatinHypercube(d=len(names), seed=rng).random(count)
    points = list(_close(low + raw * (high - low), low, high, total, balance))
    points.append(_close((low + high) / 2, low, high, total, balance)[0])
    for index in np.flatnonzero(high > low):
        for value in (max(low[index], total - high.sum() + high[index]),
                      min(high[index], total - low.sum() + low[index])):
            edge_low, edge_high = low.copy(), high.copy()
            edge_low[index] = edge_high[index] = value
            points.append(_close((edge_low + edge_high) / 2, edge_low, edge_high, total, balance)[0])
    unique = {}
    for row in points:
        point = dict(zip(names, map(float, row)))
        unique.setdefault(_key(point), point)
    return list(unique.values())


def _in_space(point, space):
    return (all(low - 1e-8 <= point.get(name, 0) <= high + 1e-8
                for name, (low, high) in space['bounds'].items())
            and all(name in space['bounds'] or value == 0 for name, value in point.items()))


def _values(rows, objectives):
    return np.asarray([[row.get('metrics', {}).get(obj['key'], np.nan) for obj in objectives]
                       for row in rows], dtype=float).reshape(len(rows), len(objectives))


def _violation(values, objectives):
    """仅对明确目标计算违约；诊断指标没有目标时不充当验收阈值。"""
    result = np.zeros(len(values))
    for index, objective in enumerate(objectives):
        target = objective.get('target')
        if target is None:
            continue
        op = objective.get('op') or ('<=' if objective['direction'] == 'min' else '>=')
        difference = values[:, index] - target if op in ('<', '<=') else target - values[:, index]
        if op in ('<', '>'):
            difference = difference + np.finfo(float).eps * max(abs(target), 1)
        result += np.maximum(difference, 0) / max(abs(target), 1)
    return result


def _gp(x, y, candidates):
    """小样本 Matérn-5/2 GP；拟合一个长度尺度，返回均值与标准差。"""
    centre, spread = float(np.mean(y)), max(float(np.std(y)), 1e-8)
    target = (y - centre) / spread
    distance = cdist(x, x)

    def kernel(distances, length):
        z = np.sqrt(5) * distances / length
        return (1 + z + z * z / 3) * np.exp(-z)

    def fit(log_length):
        matrix = kernel(distance, np.exp(log_length)) + np.eye(len(x)) * 1e-6
        factor = cho_factor(matrix, lower=True)
        alpha = cho_solve(factor, target)
        loss = float(target @ alpha / 2 + np.log(np.diag(factor[0])).sum())
        return loss, factor, alpha

    optimum = minimize_scalar(lambda length: fit(length)[0], bounds=(-3, 2), method='bounded')
    _, factor, alpha = fit(optimum.x)
    cross = kernel(cdist(candidates, x), np.exp(optimum.x))
    mean = centre + spread * (cross @ alpha)
    variance = np.maximum(1 - np.sum(cross * cho_solve(factor, cross.T).T, axis=1), 1e-12)
    return mean, spread * np.sqrt(variance)


def _improvement(mean, deviation, best):
    gain = mean - best
    z = gain / deviation
    return gain * ndtr(z) + deviation * np.exp(-z * z / 2) / np.sqrt(2 * np.pi)


def _acquisition(rows, points, objectives, space, rng):
    values = _values(rows, objectives)
    complete = np.isfinite(values).all(axis=1)
    rows = [row for row, keep in zip(rows, complete) if keep]
    values = values[complete]
    if len(rows) < 3:
        return None, 'space_filling'
    x = _features([row['point'] for row in rows], space)
    future = _features(points, space)
    violations = _violation(values, objectives)
    feasible = violations == 0
    if not feasible.any():
        mean, deviation = _gp(x, -violations, future)
        return _improvement(mean, deviation, -violations.min()), 'feasibility_improvement'

    # 每批重新抽取正权重，避免固定加权和始终偏向同一目标。
    signs = np.asarray([1 if obj['direction'] == 'max' else -1 for obj in objectives])
    scales = np.maximum(np.ptp(values, axis=0), np.maximum(np.abs(values).max(axis=0), 1) * 1e-6)
    weights = rng.dirichlet(np.ones(len(objectives)))
    utility = ((values - values.mean(axis=0)) / scales * signs) @ weights
    mean, deviation = _gp(x, utility, future)
    acquisition = _improvement(mean, deviation, utility[feasible].max())
    for index, objective in enumerate(objectives):
        if objective.get('target') is None:
            continue
        prediction, uncertainty = _gp(x, values[:, index], future)
        direction = objective.get('op') or ('<=' if objective['direction'] == 'min' else '>=')
        distance = objective['target'] - prediction if direction in ('<', '<=') else prediction - objective['target']
        acquisition *= ndtr(distance / uncertainty)
    return acquisition, 'constrained_ei'


def _batch(space, rows, seen, objectives, count, options, rng):
    if seen and all(low == high for low, high in space['bounds'].values()):
        return [], 'exhausted'
    training = list({_key(row['point']): row for row in rows if _in_space(row['point'], space)}.values())
    optimize = space.get('optimize', True) and objectives and options['method'] != 'space_filling'
    if optimize and len(training) < options['initial']:
        count = min(count, options['initial'] - len(training))
    points = [point for point in space_filling(space, options['pool_size'], rng) if _key(point) not in seen]
    if not points:
        return [], 'exhausted'
    x = _features(points, space)
    observed = _features([row['point'] for row in training], space)
    distances = cdist(x, observed).min(axis=1) if len(observed) else np.full(len(points), 1.0)
    acquisition, method = None, 'space_filling'
    if optimize and len(training) >= options['initial']:
        acquisition, method = _acquisition(training, points, objectives, space, rng)
    chosen, available = [], np.ones(len(points), dtype=bool)
    size = min(count, len(points))
    fraction = options['exploration_fraction']
    exploration = int(rng.random() < fraction) if size == 1 else int(np.ceil(size * fraction))
    for index in range(size):
        explore = acquisition is None or index >= size - exploration
        scores = distances if explore else acquisition * np.minimum(distances / 0.1, 1)
        pick = int(np.argmax(np.where(available, scores, -np.inf)))
        chosen.append({'point': points[pick], 'method': 'space_filling' if explore else method})
        available[pick] = False
        distances = np.minimum(distances, cdist(x, x[pick:pick + 1]).ravel())
    return chosen, method


def pareto_indices(values):
    """所有列均按越大越好输入；返回不被其他行支配的下标。"""
    values = np.asarray(values, dtype=float)
    return [index for index, row in enumerate(values)
            if not np.any(np.all(values >= row, axis=1) & np.any(values > row, axis=1))]


def recommend(observations, objectives, count, spaces):
    """当前建议为点：先可行 Pareto/指标极值，再以成分距离保留多样性。

    这里只选择研究代表，不决定科学准入。范围建议应在此函数中形成，
    同时通过 observation_ids 保留其实际预测依据。
    """
    if not observations or count <= 0:
        return []
    groups = {}
    for row in observations:
        groups.setdefault(_key(row['point']), []).append(row)
    rows = [group[0] for group in groups.values()]
    coverage_spaces = {space['id']: space for space in spaces if not space.get('optimize', True)}
    coverage = [index for index, group in enumerate(groups.values())
                if any(row['space_id'] in coverage_spaces for row in group)]
    explicit = {sid: {_key(point) for point in space.get('seeds', [])}
                for sid, space in coverage_spaces.items()}
    coverage_seeds = [index for index in coverage
                      if any(_key(row['point']) in explicit.get(row['space_id'], set())
                             for row in groups[_key(rows[index]['point'])])]
    bounds = {}
    for space in spaces:
        for name, (low, high) in space['bounds'].items():
            previous = bounds.get(name, [low, high])
            bounds[name] = [min(low, previous[0]), max(high, previous[1])]
    x = _features([row['point'] for row in rows], {'bounds': bounds})
    priorities = np.zeros(len(rows), dtype=int)
    preferred = []
    if objectives:
        values = _values(rows, objectives)
        complete = np.isfinite(values).all(axis=1)
        priorities[~complete] = 3
        known = np.flatnonzero(complete)
        if len(known):
            violations = _violation(values[known], objectives)
            feasible = known[violations == 0]
            priorities[known] = 2
            pool = feasible if len(feasible) else known[violations == violations.min()]
            priorities[pool] = 1
            signs = np.asarray([1 if obj['direction'] == 'max' else -1 for obj in objectives])
            front = pool[pareto_indices(values[pool] * signs)]
            priorities[front] = 0
            preferred = list(dict.fromkeys(int(front[np.argmax(values[front, index] * sign)])
                                          for index, sign in enumerate(signs)))
    # 对照/探针空间不按目标达标程度淘汰；明确设计优先，其余按成分覆盖。
    priorities[coverage] = 0
    preferred = list(dict.fromkeys([*coverage_seeds, *preferred]))
    selected = []
    for priority in sorted(set(priorities)):
        available = [index for index in range(len(rows)) if priorities[index] == priority]
        for index in preferred:
            if index in available and len(selected) < count:
                selected.append(index)
                available.remove(index)
        while available and len(selected) < count:
            distances = (cdist(x[available], x[selected]).min(axis=1) if selected
                         else np.linalg.norm(x[available] - x.mean(axis=0), axis=1))
            pick = available.pop(int(np.argmax(distances)))
            selected.append(pick)
    return [{'kind': 'point', 'point': rows[index]['point'], 'space_id': rows[index]['space_id'],
             'observation_ids': [row['id'] for row in groups[_key(rows[index]['point'])]]}
            for index in selected]


def run(spaces, evaluate, objectives, budget, *, options=None, prior=()):
    """公平轮转各设计空间；预算限制本轮回调次数，原流程负责实际模型复用。

    space: id/bounds/seeds/balance/total/optimize；objective: key/direction/target/op。
    prior 必须是相同预测条件的观测，只用于搜索，不计为本轮新研究结果。
    """
    options = settings(options)
    rng = np.random.default_rng(options['seed'])
    observations, trace = [], []
    seen = {space['id']: set() for space in spaces}

    def observe(space, point, method):
        row = {**evaluate(space['id'], point), 'point': point, 'space_id': space['id']}
        observations.append(row)
        seen[space['id']].add(_key(point))
        trace.append({'observation_id': row['id'], 'space_id': space['id'], 'method': method})

    # 显式设计优先；不同假设/实验身份可保留同一个成分点。
    for space in spaces:
        for point in space.get('seeds', []):
            if len(observations) >= budget:
                break
            if _key(point) not in seen[space['id']]:
                observe(space, point, 'explicit')
    pending = {space['id']: [] for space in spaces}
    exhausted = set()
    while len(observations) < budget and len(exhausted) < len(spaces):
        for space in spaces:
            sid = space['id']
            if sid in exhausted or len(observations) >= budget:
                continue
            if not pending[sid]:
                remaining, active = budget - len(observations), len(spaces) - len(exhausted)
                share = (remaining + active - 1) // active
                pending[sid], _ = _batch(
                    space, [*prior, *observations], seen[sid], objectives,
                    min(options['batch_size'], share), options, rng)
            if not pending[sid]:
                exhausted.add(sid)
                continue
            choice = pending[sid].pop(0)
            observe(space, choice['point'], choice['method'])
    count = options['representatives']
    return {'observations': observations,
            'recommendations': recommend(observations, objectives, budget if count is None else count, spaces),
            'trace': trace}
