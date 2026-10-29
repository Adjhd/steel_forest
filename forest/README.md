# 假设森林

`canonical.json` 是这个用户唯一的当前森林。它保存假设、材料和场景节点、候选成分、实验计划、明确关系与文献证据。`store.py` 负责读取、整理研究上下文、审查后的合并。`sources/` 保存证据引用的原文页。

森林保存当前状态，不在活动目录保留版本链。节点的 `revision`、`version_id` 标识当前版本；每次合并会将被替换的整份快照写入项目回收站，再原子替换当前文件。回收站默认不读、不搜，不进入 agent 上下文；只有用户明确要求恢复、历史比较、复现审计或调查误删时才读取。

当前森林包含 274 个待验证假设、190 个候选、190 个成分节点、190 个实验计划，以及材料体系和具体来源场景。3 条已采纳文献记录保留各自的原文、适用范围与限制；190 个实验槽均待实测。这些数量描述初始内容，后续以 `canonical.json` 为准。

## 研究时怎样读取

```python
from forest.store import load, context

state = load()
reading = context("weak_field_nonmagnetic_plate", state=state)
hypothesis = state["nodes"]["某个节点ID"]
```

`context` 默认返回最多 12 张假设卡片，保留完整作用域、预测、证伪条件和关联证据；缺少预测的卡片另带验证计划。跨场景内容沿明确的 `transfer_candidate` 关系读取，保留共同机理、条件差异、验证方法与风险，省略重复登记字段。`source_refs` 中已有的摘录和来源定位会随卡片提供，它们仍是来源线索，不会自动变成已核验的证据或新增 `evidence_ids`。完整科学字段可按节点 ID 从当前森林读取。

`load(path=None)` 返回普通字典：`nodes`、`evidence` 按 ID 索引，`links`、`campaigns` 是列表，`revision` 是森林版本号。默认路径就是本目录的 `canonical.json`。

## 怎样构建、审查与合并

新工作先放在 campaign 的提案里。提案 `delta` 是三个列表：

```json
{
  "nodes": [{
    "id": "H-v001-01", "kind": "hypothesis",
    "scenario": "weak_field_nonmagnetic_plate",
    "statement": "在明确的成分和制造条件下，提出一个可检验的磁响应机制。",
    "scope": {"composition_basis": "wt_pct", "process": "待确认"},
    "predictions": ["写明可观测的预测"],
    "falsifiers": ["写明什么结果会削弱该解释"],
    "verification_plan": {"controls": "匹配对照", "observables": ["磁导率", "相组成"]},
    "evidence_ids": []
  }],
  "evidence": [],
  "links": []
}
```

证据记录需要 `id`、`kind`、`source`、`summary`；campaign 支持 `literature` 和 `model`。关系需要 `source`、`target`、`relation`，跨作用域迁移另需 `transfer_contract`。候选通过 `hypothesis_id`、`hypothesis_version` 指向当前假设版本，通过 `plan_id` 指向对应实验计划；计划的 `candidate_id` 指回候选。这些引用需要在当前森林或同批获准节点中解析，类型和归属须一致；候选与计划不能只接受一半。多节点引用也支持 `hypothesis_ids`、`candidate_ids`。

审查者明确列出接受的 ID，并说明理由：

```python
from forest.store import apply, merge

review = {
    "reviewer": "research-reviewer",
    "reason": "作用域明确，预测可以通过指定对照检验。",
    "accepted_node_ids": ["H-v001-01"],
    "accepted_evidence_ids": [],
}
private_state = apply(state, delta, review)
current_state = merge(delta, review, expected_revision=state["revision"])
```

`apply(state, delta, review)` 只更新内存里的私有状态。`merge(delta, review, expected_revision, path=None)` 才写入 canonical；如果另一场 campaign 已合并，会提示版本变化，需要基于新森林重新审查。节点更新提交完整记录；证据 ID 不覆盖已有来源记录。未获准节点不进入森林，依赖它们的关系也不进入森林。

审查通过表示研究工作可以保留。新假设仍是 `proposed`，实验计划仍是 `planned`，新文献检索记录仍待来源核验，模型输出仍是 `diagnostic`。生成过程不能创建实测证据或把预测写成机制已经成立。
