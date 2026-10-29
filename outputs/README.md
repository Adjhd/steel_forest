# Campaign 产出

中间结果按 `vYYYYMMDD.NNN/` 分批；最终报告和交付表格集中到 `reports/<version>/`。两处使用同一版本号，不覆盖、不混写。自然语言问题、有效场景、工具版本、范围和预算保存在批次的 `input.json`；实际使用的提示词也一并保存。

典型内容：

```text
outputs/
  v20261029.002/
    input.json、question.md、prompts.md
    status.json、usage.json
    round_01/
      search_input.json、search.json、literature.json
      hypotheses_input.json、hypotheses.json、admission.json
      design.json、evaluated.json、review.json、delivery.json
      <候选ID>/prediction.json及物性工具原始输出
    round_02/ ...
    forest_delta.json、forest_review.json、result.json
  reports/
    v20261029.002/
      候选报告.md
```

每阶段还保留完整请求输入。`result.json` 区分提名和通过交付检查的最终候选；`status.json` 区分 prepared、running、failed、budget_exhausted 与 frozen_pending_measurement。

无磁钢、耐蚀钢的已导入报告分别位于 `reports/v20261028.001/`、`reports/v20261028.002/`；直接引用的JSON材料仍在对应批次目录，导入说明见批次内 `import.json`。它们不是新控制器运行结果，也没有据此更改森林或补造科研过程。
