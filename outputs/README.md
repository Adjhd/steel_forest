# Campaign 产出

中间结果按自定的 `<version>/` 分批；最终报告和交付表格集中到 `reports/<version>/`。两处使用同一版本号，不覆盖、不混写。自然语言问题、有效场景、工具版本、范围和预算保存在批次的 `input.json`；实际使用的提示词也一并保存。

典型内容：

```text
outputs/
  trial-1/
    input.json、question.md、prompts.md
    status.json、usage.json
    round_01/
      queries_input.json、queries.json、literature.json
      hypotheses_input.json、hypotheses.json、admission.json
      design.json、search.json、evaluated.json、review.json、delivery.json
      <候选ID>/case_01/prediction.json及该条件组的物性工具原始输出
    round_02/ ...
    forest_delta.json、forest_review.json、result.json
  reports/
    trial-1/
      候选报告.md
```

每阶段还保留完整请求输入。`queries.json` 是文献查询问题；`search.json` 是数值搜索的观测、建议和选点轨迹。各条件组的实际协议、工具和结果独立记录在 `evaluated.json`，与 `input.json` 中条件组的声明顺序对应；复用预测保留原证据 ID，不重复生成物性工具输出。`result.json` 区分提名和通过交付检查的最终候选；`status.json` 区分 prepared、running、failed、budget_exhausted 与 frozen_pending_measurement。

`literature.json` 从检索开始就存在，每取得一条文献结果就分配 ID 并保存；同一查询的后续服务或下一查询失败时，已经保存的结果仍保留。失败批次的该文件可能为空或只有部分结果；具体失败的查询序号与服务名见 `status.json`，不能仅凭文件存在判断检索已完成。

无磁钢、耐蚀钢的已导入报告分别位于 `reports/v20261028.001/`、`reports/v20261028.002/`；直接引用的JSON材料仍在对应批次目录，导入说明见批次内 `import.json`。它们不是新控制器运行结果，也没有据此更改森林或补造科研过程。
