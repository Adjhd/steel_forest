# Campaign 产出

每批一个 `vYYYYMMDD.NNN/`，不覆盖、不混写。自然语言问题、有效场景、工具版本、范围和预算保存在 `input.json`；实际使用的提示词也一并保存。

典型内容：

```text
v20261029.002/
  input.json、question.md、prompts.md
  status.json、usage.json
  round_01/
    search_input.json、search.json、literature.json
    hypotheses_input.json、hypotheses.json、admission.json
    design.json、evaluated.json、review.json、delivery.json
    <候选ID>/prediction.json及物性工具原始输出
  round_02/ ...
  forest_delta.json、forest_review.json
  result.json
  候选报告.md
```

每阶段还保留完整请求输入。`result.json` 区分提名和通过交付检查的最终候选；`status.json` 区分 prepared、running、failed、budget_exhausted 与 frozen_pending_measurement。

`v20261028.001/` 和 `v20261028.002/` 是已形成的无磁钢、耐蚀钢候选报告及其直接引用材料，导入说明见批次内 `import.json`。它们不是新控制器运行结果，也没有据此更改森林或补造科研过程。
