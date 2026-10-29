# 场景注册

每个场景只有一份活动定义 `scene.json`，它统一规定指标、单位、比较协议、元素集合和当前工具版本。`load("nonmagnetic")` 读取它；完整场景 ID 也可使用。研究问题和本次元素范围写在 campaign 输入中，注册表不会自动替研究者选配方。

| 短名 | 场景与版本 | 主要指标 |
| --- | --- | --- |
| `corrosion` | 固定轮周浸耐蚀钢，`catalog-v3` | 累计损失厚度 μm；相对 Q345B 实测质量损失比 ≤0.5；相对 3Ni 节省 ≥1500 元/t；EL >20%；YS、UTS 报告 |
| `nonmagnetic` | 弱场无磁钢板，`catalog-v1.2` | 距钢 2 cm 场强 ≤0.5 nT；实测 μr ≤1.02；YS ≥500 MPa；UTS ≥800 MPa；EL ≥30%；YS/UTS <0.85；PREN >20 |
| `hea` | 高温高熵合金，`catalog-v1.12` | 密度 ≤8.2 g/cm³；室温 UTS ≥1100 MPa、断后 A ≥8%；700 °C YS ≥600 MPa、断后 A ≥15% |

`metrics.role` 表示允许的用途，不是证据等级：`screening` 可用于研究排序或名义筛选，`diagnostic` 只报告，`measurement` 是实验确认要求。模型输出始终不等于实测。HEA 四工具全为 `diagnostic`，不用于候选评分或合同阈值淘汰。

腐蚀累计损失厚度、材料 Br、模型 μr 的 `role=screening`、`target=null`，仅允许在同一比较条件下按预测值做名义研究排序，没有通过阈值，也没有物理验收资格。三个指标各自的 `ranking` 写明允许用途、资格与活动注册字段来源；`qualification` 保持模型诊断。Br 的排序不使用 0.5 nT 阈值，模型 μr 的排序不使用实测 μr≤1.02 作通过判断。

## 元素与条件

- **耐蚀钢**：Fe、C、Si、Mn、P、S、Cr、Ni、Cu、Mo、Nb、V、Ti、Sb、Sn、Al，Fe 余量。本次范围只需列出参与研究的元素，未列非 Fe 元素按 `[0, 0]` 处理。固定 45 °C 溶液、34 °C 环境、63%RH、0.5% NaCl、168 h、60×40×6 mm 试样。μm 厚度输出和相对 Q345B 质量损失比是两个量。
- **无磁钢**：上述元素加 Zn、Mg、Re、N，Fe 余量。范围同样只列参与研究的元素，未列非 Fe 元素为零。参考任务窗保存在 `nonmagnetic/scene.json` 的 `reference_ranges_wt_pct`，供阅读；每次启动必须显式选择范围。已知力学条件写入 `protocol.mechanical_conditions`，例如 `test_temperature_C`、`solution_temperature_C`、`solution_time_h`、`cold_reduction_pct`、`plate_thickness_mm`。未知项不补造成已知条件。
- **高熵合金**：`hea/scene.json` 的 `elements` 是四工具共同支持的元素集合，本次研究另行指定子集和范围。成分明确为 wt.% 且合计 100，没有默认 Fe 余量。必须给出工艺文本和 0–700 °C 的测试温度；各温度分别评价。模型报告 EL 与合同断后 A 的定义不等同。

## 当前工具

| 场景 | 工具 / 版本 | 作用 |
| --- | --- | --- |
| 耐蚀钢 | `material_candidate_evaluation@v3-ni-gated-20261026` | Ni 分段四专家累计腐蚀厚度、固定基准成本、CorrMP-v1 力学诊断 |
| 无磁钢 | `property-lgbm-v1-remanence@v1`；`property-lgbm-v1-relative-permeability@v1` | Br（T）和 μr 诊断；Br 不代替 2 cm 场强 |
| 无磁钢 | `nms-plate-mechanical@20261021_135640` | 同输入 YS / UTS / EL，派生屈强比和 PREN |
| 高熵 | `hea-reported-density-nist38-lightgbm@hea-reported-density-nist38-v1-20261026` | 报告密度；CPU，36 元素坐标加两个密度描述符 |
| 高熵 | `hea-reported-yield-strength-lightgbm`、`hea-reported-tensile-strength-lightgbm`，均 `hea-strength-ratio-v1-20261022` | 工艺、成分和测试温度条件下的 YS / UTS |
| 高熵 | `hea-reported-elongation-lightgbm@hea-el-le700-v1-20261023` | 报告 EL，0–700 °C，仅诊断 |
| 全部 | `lkm_claim_search@api-v2`、`dprag_context_search@api-v1` | 文献来源线索和上下文；线索需核对原文后才能形成证据 |

## 代码与外部资产

`__init__.py` 只负责读取定义、检查输入与组合协议。`tools.py` 是唯一连接现有物性实现的文件：它按需载入 `steel_forest/src/steel_forest` 中当前物性模块，跳过旧包的初始化，不载入旧 campaign、森林或研究审查流程。这样保持现有预测、特征转换与模型版本的含义。新项目的研究主流程不依赖旧 campaign。

模型是外部计算依赖，不复制权重。每项工具的 `asset` 指向工作区下当前绑定文件。无磁力学使用 `/internfs/zhangyuanwang/model_registry/non-magnetic-steel/` 的三个注册权重；高熵力学使用本地 Qwen3-Embedding-0.6B；腐蚀、无磁力学、高熵各使用绑定文件指定的 Python 环境。默认 GPU 分别为腐蚀 2、无磁力学 2、高熵力学 3；密度在 CPU。项目须能访问这些路径，普通系统 Python 不代表已具备全部预测依赖。Python 主环境建议 `/mnt/vepfs/Djinhan/conda_env/steel/bin/python`。

```python
from scenarios import load, validate_ranges, evaluate

scene = load("nonmagnetic")
ranges = validate_ranges(scene, request["composition_ranges_wt_pct"])
result = evaluate(scene, composition, request["protocol"], candidate_output_dir)
# result: metrics 字典、tools 列表、limitations 列表
```

每个候选的目录保存原始物性输出及 `prediction.json`；保留缺值和适用性限制。成分字典中省略的非余量元素表示含量为零，钢铁的 Fe 可省略并自动补足至 100 wt.%。调用钢铁模型前会补齐全部零坐标；本次范围要求含量大于零的元素仍必须给出。HEA 仅使用显式选择的元素，所给含量须合计 100 wt.%，没有自动补足的基体元素。`effective_protocol` 允许补充未知条件，但不覆盖场景固定比较条件。

更换场景或模型时更新这份活动注册和对应实际调用；退役内容进入回收站，活动定义只描述当前口径。任何默认读取、运行或错误回退均不访问回收站。
