# 启动一次研究

这里的输入只有两部分：`question.md` 描述研究问题；`space.json` 声明 `composition_ranges_wt_pct` 和 `protocol`，也可附上 `notes` 等说明。指标与工具从场景加载，不在问题里重复注册。直接传 `--question '自然语言'` 可以替代文件。

`run.sh` 的参数行是日常入口，`bash campaign/run.sh --help` 查看全部参数。命令末尾同名参数覆盖脚本中的设置。默认问题和范围文件是可编辑的无磁钢示例；切换耐蚀钢或高熵场景时同时传对应范围文件。

| 参数 | 含义 |
|---|---|
| `--version '无磁钢初筛'` | 自定批次名，不要求日期或序号；单个目录名，已存在就停止 |
| `--scenario corrosion/nonmagnetic/hea` | 选择当前场景，也接受完整场景 ID |
| `--question` / `--question-file` | 自然语言要求，二选一 |
| `--space` | 本次元素范围与实际或未知条件 |
| `--rounds 3` | 最多科研轮次 |
| `--evaluations 12` | 每轮最多评估的成分数；`0` 表示只研究假设 |
| `--literature-queries 2` | 每轮查询数；每个已配置文献服务各请求一次；`0` 跳过检索 |
| `--llm-calls 15` | 整批LLM请求上限，可为 `0`；完整一轮最多5次，用完即保存结束，无自动重试 |
| `--recommendations 3` | 本批最终至少交付的不同成分候选数，可跨轮累计；优先于自然语言中的数量描述 |
| `--prepare-only` | 只装配并保存输入，完全离线 |
| `--keep-private` | 研究完成后不合入正式森林 |

代码阅读顺序：`run.py` 读取输入与写报告 → `engine.py` 的 `run()` 查看研究顺序 → 各阶段函数查看具体输入输出 → `services.py` 请求外部服务。固定提示词完整放在 `prompts.md`；每轮实际请求和回答保存在输出中。`forest/store.py` 处理接受记录的最终合并。

阶段函数都在 `engine.py`：`plan_searches` 规划检索 → `retrieve_literature` 获取文献 → `propose_hypotheses` 提出假设 → `review_hypotheses` 准入审查 → `design_candidates` 设计与采样 → `evaluate_designs` 真实模型预测 → `review_candidates` 研究复审。`research_context` 整理共同场景资料；`run()` 管理轮次、预算、反馈和森林更新。调试时直接在所需阶段函数内打断点。

`--recommendations` 是候选数量的唯一口径，程序将它作为 `requested_candidates` 传给各阶段并写入报告。默认问题不固定数量；即使自写问题里保留了其他数量，仍以这个参数为准。它是交付目标，`--evaluations` 等参数是调用上限；预算不足时保存已有成果，不保证凑齐候选。

## 各阶段读什么

system 给出共同规则和本阶段任务，user 提供完成这个任务所需的资料。两者不需要一样长；上下文按阶段选择，不直接发送完整存储对象。

| 阶段 | 提供的研究资料 |
|---|---|
| 检索 | 问题、实际范围与条件、简短假设和关系、待查反馈与查询上限 |
| 生成假设 | 相关森林假设、已有证据、本轮文献片段和出处 |
| 准入 | 提议假设及其引用的新证据、相关来源假设和已接受的证据 |
| 设计 | 准入假设、它们引用的文献、实际范围与预测协议、指标和评估预算 |
| 复审 | 当前及本批此前候选的配方、计划、真实预测、所据假设与文献；共同工具、协议和局限只提供一次 |

原始检索响应、模型输出及研究记录仍保存在批次目录中；发给 LLM 的资料去掉重复包装和存储元数据。文献正文不同时重复出现在 `text` 与 `raw`，相关依据也不只传一个 `evidence_id`。复审保留本批此前全部获准候选，不按最近几条截掉候选池。

## 声明范围与条件

成分均为 wt.%。钢铁范围只需列参与研究的元素，未列出的非Fe元素按 `[0,0]` 处理；配方可省略零含量元素，Fe按余量闭合。HEA显式列出选用元素，不默认Fe基体。假设再声明自身范围，设计和补充采样只能在其中进行。采样使用固定随机种子的有界随机点；它是探索工具，不是贝叶斯优化，也不把采样密度当作科学证据。

无磁钢未知力学条件写 `"mechanical_conditions": {}`，已知条件用该工具支持的数值字段。HEA例如：

```json
{
  "composition_ranges_wt_pct": {"Co":[15,25],"Cr":[15,25],"Fe":[15,25],"Mn":[15,25],"Ni":[15,25]},
  "protocol": {"process_text":"制备工艺尚未确定", "testing":{"test_temperature_C":700}}
}
```

一个批次固定一种预测协议；不同测试温度分别启动批次。HEA四项预测只报告，不按合同硬门槛筛除。探索与对照可按研究问题选择测量端点，最终推荐的待测计划须覆盖场景验收端点；模型能算出数值不代表制造状态已经确定。

## 配置真实服务

所有命令都在本项目根目录执行。先复制配置模板，填写自己的服务地址、模型名和密钥，再导入当前 shell：

```bash
cd /mnt/vepfs/Djinhan/Agentic_data/steel_forest_hf
cp -n .env.example .env    # 已有 .env 时保留，直接编辑
# 用编辑器填写 .env，然后执行：
set -a
source .env
set +a
```

`run.sh` 不自动读取 `.env`，每个新终端都需要重新导入。`.env` 已被 Git 忽略；不要把真实密钥填入 `.env.example`。服务只从当前进程的环境读取凭据，不将其写入研究输出。

LLM 使用兼容 Chat Completions 的接口，需要填写三项：

| 环境变量 | 填写内容 |
|---|---|
| `LITELLM_API_BASE` | 网关给出的 API base，例如 `https://你的网关/v1`；程序追加 `/chat/completions`，也接受以该路径结尾的完整地址 |
| `LITELLM_API_KEY` | 该网关的访问密钥 |
| `STEEL_FOREST_LLM_MODEL` | 网关中实际可用的模型 ID，不是物性预测模型名 |

模型需要输出一个 JSON 对象。默认请求含 `response_format={"type":"json_object"}`；若网关不支持这个请求字段，可设置 `STEEL_FOREST_LLM_RESPONSE_FORMAT=none`。程序也接受完整包在 Markdown JSON 代码围栏中的对象；无效或截断的 JSON 不作为研究结果。

启用文献检索时至少配置一个服务：`BOHR_ACCESS_KEY` 使用 LKM，`DPRAG_API_KEY` 使用 DPRAG；不使用的密钥留空。campaign 使用 `auto`，两项都有值时会依次请求两者，任一调用失败都会停止当前批次，并非自动切换备用服务。返回内容是文献线索或片段，不代表完成原文核验。`--literature-queries 0` 不发起搜索规划或文献请求，可仅凭当前森林研究；没有依据时允许不提出假设。

可选配置已列在 `.env.example`：LLM 超时默认 180 秒；本地不设输入字符上限，如需自己限制，可显式设置 `STEEL_FOREST_MAX_REQUEST_CHARS`，留空则关闭。模型自身的上下文容量仍由服务端决定。LKM、DPRAG 的超时分别固定为 60、180 秒。`LITELLM_SOCKS5_PROXY` 只配置 LLM 的专用代理，文献接口不读取它；通用 `HTTP_PROXY` / `HTTPS_PROXY` 等环境变量仍由 `requests` 处理。默认文献地址直接取自当前代码，只有服务方提供其他地址时才覆盖 `LKM_API_BASE` / `DPRAG_API_BASE`。

## 逐项调试

先单独验证接口。以下两段会产生真实服务请求，但不读取森林、不运行物性模型、不创建 campaign 输出。

```bash
campaign_python="${STEEL_HF_PYTHON:-/mnt/vepfs/Djinhan/conda_env/steel/bin/python}"

# 只发起一次 LLM 请求；正常结果应为 {"ok": true}。
"$campaign_python" -B - <<'PYTHON'
from campaign.services import ask
print(ask('请返回 {"ok": true}。', {"task": "连接测试"}))
PYTHON

# 检索一个问题；auto 请求所有已配置的文献服务。
"$campaign_python" -B - <<'PYTHON'
from campaign.services import literature
rows = literature('austenitic nonmagnetic steel manganese nitrogen', provider='auto', top_k=2)
print('线索数：', len(rows))
for row in rows:
    print(row['provider'], row['title'], row['url'], row['text'][:160])
PYTHON
```

单测一个文献服务时将 `provider='auto'` 改成 `'lkm'` 或 `'dprag'`。返回零条线索表示请求成功但未检索到结果，可换检索词；HTTP 401/403 时检查对应凭据与权限，404 时检查地址拼接和网关路径，JSON 错误时检查模型输出及 `response_format` 支持情况。服务错误不会打印密钥或响应正文，也不会自动重试。

接口通过后，先离线检查，再用一轮、小预算运行真实链路。版本号是示例，每次执行都必须使用未占用的新版本：

```bash
bash campaign/run.sh --version debug-input --prepare-only

bash campaign/run.sh --version debug-1 \
  --rounds 1 --evaluations 2 --literature-queries 1 \
  --llm-calls 5 --recommendations 1 --keep-private \
  --question '在给定范围内研究无磁钢的强塑性与低磁性，形成一个有文献依据的候选及待实测计划。'
```

真实运行会把研究问题、选取的森林假设和工具结果发送给已配置的服务；`--keep-private` 仍读取当前森林，但不将结果合入 `canonical.json`。这个预算用于检查链路，可能正常结束为 `budget_exhausted`，不保证产生合格推荐。中间结果在 `outputs/<version>/`，最终文档在 `outputs/reports/<version>/候选报告.md`。

## 调试与结束

LLM 预算逐次计算：例如 `--llm-calls 1` 可只运行第一步，已完成的阶段仍落盘；`0` 不调用 LLM。`--evaluations 0` 保留假设研究与准入，跳过设计和预测。预测预算按本轮设计顺序执行，超出的设计保留在 `design.json` 中但不执行。达到预算正常结束为 `budget_exhausted`；已接受记录可合入森林，调试时使用 `--keep-private`。

先用新版本执行 `--prepare-only` 检查范围和协议。正式运行失败时保留输入、已完成的各阶段JSON、逐候选原始输出和 `status.json`；修正输入或代码后开新版本重跑。没有隐式续跑、自动加预算或切换模型的分支。

每轮选取4张当前假设卡、它们引用的全部证据以及可见的显式关系，再按阶段职责组织输入；复审可选本轮候选和本批此前全部获准候选，记录不会混入其他campaign。正式森林合并在批次结束时进行，采用启动时的森林版本；若其他批次先提交，会停止合并，保留本批delta与审查结果，待基于当前森林重新审查。

假设可引用本轮检索或森林中已接受的证据。准入只接受本轮的新证据；既有证据保留原记录，引用不会重复写入森林。

最终报告展示实际预测输入条件和待实测工艺计划。两者分别记录，不能把计划中的新工艺写成已经进入模型计算。成分按实际预测精度输出，不先圆整后复用旧预测值。
