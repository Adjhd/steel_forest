# Steel Forest HF

用一片持续积累的假设森林研究材料、设计成分候选，并给出可执行的验证计划。HF 是 human friendly：研究者能找到输入、读懂流程、看到每轮发生了什么。代码由钢铁推荐项目整理而来。

## 从哪里开始

1. 在 [`campaign/question.md`](campaign/question.md) 写自然语言研究问题。
2. 在 [`campaign/space.json`](campaign/space.json) 明确元素范围与条件。随附无磁钢示例可直接装配，正式研究前应按任务编辑；它不是通用默认研究窗。
3. 编辑 [`campaign/run.sh`](campaign/run.sh) 的版本号、场景和预算，再运行。

```bash
cd /mnt/vepfs/Djinhan/Agentic_data/steel_forest_hf

# 离线检查输入：不读取森林，不调用物性模型、文献服务或LLM。
bash campaign/run.sh --version input-check --prepare-only

# 正式研究使用一个新版本。运行前需配置文献和LLM服务凭据。
bash campaign/run.sh --version '无磁钢初筛' \
  --question '在给定范围内研究无磁钢的强塑性与低磁性，给出可证伪假设和对照实验。'
```

版本就是你自定的批次名，例如 `trial-1`、`无磁钢初筛`、`第 2 轮`，不要求日期或固定格式。名称须是非空的单个目录名，不能是 `.`、`..` 或包含 `/`、空字符；带空格时在命令中加引号。同名批次或报告目录存在时不覆盖；准备检查也占用其名称。最终报告位于 `outputs/reports/<version>/候选报告.md`。运行参数和详细说明见 [`campaign/README.md`](campaign/README.md)。

## 目录各做一件事

| 目录 / 文件 | 看什么、改什么 |
|---|---|
| [`scenarios/`](scenarios/README.md) | 耐蚀钢、无磁钢、高温高熵合金的指标、元素、比较条件和工具；每场景一个目录、一份活动登记 |
| [`forest/`](forest/README.md) | 假设森林的读取、审查合并；`canonical.json` 是唯一正式森林，`sources/` 是其文献原文 |
| [`campaign/`](campaign/README.md) | Bash入口、问题和范围输入、统一提示词、研究流程；`search.py` 集中实现空间填充、贝叶斯优化和代表选择 |
| [`outputs/`](outputs/README.md) | 每批的输入、中间结果、预测与审查；最终文档集中在 [`outputs/reports/`](outputs/reports/README.md) |
| `recycle_bin/` | 隔离被替代的文件、口径、模型登记和森林快照；默认不读、不搜 |
| [`recycle.sh`](recycle.sh) | 将本项目中的指定文件或目录移入回收站 |
| [`AGENTS.md`](AGENTS.md) | 后续共同开发必须遵守的简洁性与科学边界 |

流程按顺序展开：**文献问题 → 检索 → 条件化假设 → 准入审查 → 假设内设计与自适应搜索 → 真实工具诊断 → 研究审查与交付检查 → 反馈下一轮**。搜索可用空间填充覆盖范围，也可根据已经得到的预测用贝叶斯优化选择下一批。批次结束，把审查接受的研究记录合入正式森林。`--keep-private` 可先保留批次成果供调试。无论是否形成最终推荐，研究假设都不会因为模型预测而自动变成已证实结论。

只有经过交付检查的待测方案标为 `frozen_pending_measurement`。`budget_exhausted` 表示预算用完，报告保留中间发现而不包装成正式推荐。每轮候选均绑定假设版本、实际预测条件和待实测计划。

`--recommendations` 指定本批最终至少交付的不同成分候选数，优先于自然语言中的数量描述；评估、检索和 LLM 预算分别限制实际调用，不能保证达成交付目标。

一个批次可以声明多组命名预测条件，各条件分别登记工具、预测和证据。`--evaluations` 限制每轮成分设计数，每个设计都评估声明的条件组；模型调用量随条件组数增加。搜索建议和科学候选分别记录：代表选择帮助复审，只有实际预测过且通过复审的成分才能进入最终候选。

## 一个口径

场景事实只在 `scenarios/*/scene.json` 定义，代理只看到当前登记工具及其适用边界。自然语言输入描述科研问题；模型版本选择由程序完成。场景 README 是简短导览，不另建持续膨胀的主控或审计文档。

LLM 输入按检索、假设、准入、设计和复审分别组织，只传本阶段需要的内容。设计与复审同时提供相关证据正文和出处，共同工具与协议集中呈现，逐候选保留实际结果和差异。完整原始记录保存在输出中，不直接作为每个阶段的提示词。

森林只保留一份当前状态；合并前把被替换的快照写入回收站。普通读取按场景取假设和显式跨场景关系，不自动拿已有候选配方作为新研究答案。这种读取策略不等于严格盲态实验，也不证明森林已产生跨场景收益。

回收示例：`bash recycle.sh outputs/input-check '结束输入调试'`。回收记录保存在回收站内部，不在活动文档追述。只有你明确要求恢复、历史比较、复现审计或排查误删时才允许读取其中内容；`.gitignore`、`.rgignore` 与开发规则共同排除普通检索。它是上下文访问约定，不是操作系统权限隔离。

## 环境与依赖

主 Python 默认 `/mnt/vepfs/Djinhan/conda_env/steel/bin/python`，可通过 `STEEL_HF_PYTHON` 覆盖；外部服务使用 `requests[socks]`，搜索使用 NumPy 和 SciPy。研究控制器与森林管理独立实现。`scenarios/tools.py` 是唯一物性接点，复用工作区中的模型代码、权重和专用运行环境，具体路径与限制见场景说明。将本目录复制到另一台机器时，还需配置这些物性资产。

真实 LLM 和文献服务的变量模板见 [`.env.example`](.env.example)，复制成 `.env` 后填入自己的地址、模型名和凭据，并按 [调试步骤](campaign/README.md#配置真实服务)加载。`.env` 不自动加载，也不会提交到 Git。

## Git 管理

源码仓库是 [Adjhd/steel_forest](https://github.com/Adjhd/steel_forest)。本目录独立管理 Git，保留仓库的 MIT 许可证；提交源码、场景登记、提示词和说明。必要验证临时在项目外完成，不在项目中保留测试代码。`forest/canonical.json`、`forest/sources/`、各批 `outputs/`、凭据与回收站是本地研究数据，已排除提交。唯一正式森林仍在本目录，并由森林合并和回收机制管理。

因此，单独 clone 源码不会取得研究数据、外部物性实现或模型权重。当前机器已有这些资产；迁移机器时需另外配置并提供当前 canonical。真实 campaign 需要相应的 LLM 环境变量；启用检索时还需至少一个文献服务，见 campaign 说明。

持续开发时优先改现有职责明确的文件，删掉失效路径；不为每次修复、模型更替或实验新建 Python 文件。保留能约束真实风险的输入、证据、预算和并发检查，避免加入历史兼容与重复审计框架。
