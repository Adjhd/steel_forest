# 启动一次研究

这里的输入只有两部分：`question.md` 描述研究问题；`space.json` 声明 `composition_ranges_wt_pct` 和 `protocol`，可选 `protocol_cases` 配置多组条件、`search` 配置搜索，也可附上 `notes` 等说明。指标与工具从场景加载，不在问题里重复注册。直接传 `--question '自然语言'` 可以替代文件。

`run.sh` 的参数行是日常入口，`bash campaign/run.sh --help` 查看全部参数。命令末尾同名参数覆盖脚本中的设置。默认问题和范围文件是可编辑的无磁钢示例；切换耐蚀钢或高熵场景时同时传对应范围文件。

| 参数 | 含义 |
|---|---|
| `--version '无磁钢初筛'` | 自定批次名，不要求日期或序号；单个目录名，已存在就停止 |
| `--scenario corrosion/nonmagnetic/hea` | 选择当前场景，也接受完整场景 ID |
| `--question` / `--question-file` | 自然语言要求，二选一 |
| `--space` | 本次元素范围与实际或未知条件 |
| `--rounds 3` | 最多科研轮次 |
| `--evaluations 12` | 每轮最多评估的成分设计数，每个设计评估全部条件组；同配方不同计划可分别计数；`0` 表示只研究假设 |
| `--literature-queries 2` | 每轮研究问题数；每个已配置文献服务分别检索，临时故障最多尝试3次；`0` 跳过检索 |
| `--llm-calls 15` | 整批 LLM 请求上限，可为 `0`；无重试时一轮最多 5 次，重试与输出纠正也计数，用完保存结束 |
| `--recommendations 3` | 本批最终至少交付的不同成分候选数，可跨轮累计；优先于自然语言中的数量描述 |
| `--prepare-only` | 只装配并保存输入，完全离线 |
| `--keep-private` | 研究完成后不合入正式森林 |
| `--allow-unreviewed` | 调试时允许复审预算不足仍执行预测；未审结果仅保存为中间产物 |

代码阅读顺序：`run.py` 读取输入与写报告 → `engine.py` 的 `run()` 查看研究顺序 → 各阶段函数查看具体输入输出。`search.py` 负责选点和搜索建议，`services.py` 请求外部服务。固定提示词完整放在 `prompts.md`；每轮实际请求和回答保存在输出中。`forest/store.py` 处理接受记录的最终合并。

阶段函数都在 `engine.py`：`plan_searches` 规划检索 → `retrieve_literature` 获取文献 → `propose_hypotheses` 选用已有假设或提出新假设 → `review_hypotheses` 审查新假设 → `design_candidates` 设计假设内的研究空间 → `search_candidates` 自适应选点并调用真实模型 → `review_candidates` 研究复审。`research_context` 整理共同场景资料；`request_stage` 管理阶段请求、重试和输出纠正；`run()` 管理轮次、预算、反馈和森林更新。调试时直接在所需阶段函数内打断点。

`--recommendations` 是候选数量的唯一口径，程序将它作为 `requested_candidates` 传给各阶段并写入报告。默认问题不固定数量；即使自写问题里保留了其他数量，仍以这个参数为准。它是交付目标，`--evaluations` 等参数是调用上限；预算不足时保存已有成果，不保证凑齐候选。

## 各阶段读什么

system 给出共同规则和本阶段任务，user 提供完成这个任务所需的资料。两者不需要一样长；上下文按阶段选择，不直接发送完整存储对象。

| 阶段 | 提供的研究资料 |
|---|---|
| 检索 | 问题、实际范围与条件、简短假设和关系、待查反馈与查询上限 |
| 生成假设 | 相关森林假设、可直接沿用的假设 ID、已有证据、本轮文献片段和出处 |
| 准入 | 提议假设及其引用的新证据、相关来源假设和已接受的证据 |
| 设计 | 新准入或选用的已有假设、它们引用的文献、实际范围与各预测条件组、指标和评估预算 |
| 复审 | 当前及本批此前候选的配方、计划、逐条件预测、搜索建议、所据假设与文献；共同工具、协议和局限只提供一次 |

原始检索响应、模型输出及研究记录仍保存在批次目录中；发给 LLM 的资料去掉重复包装和存储元数据。文献正文不同时重复出现在 `text` 与 `raw`，相关依据也不只传一个 `evidence_id`。复审保留本批此前全部获准候选，不按最近几条截掉候选池。

假设阶段可通过 `reuse_hypothesis_ids` 选用输入 `reusable_hypothesis_ids` 中的节点：它们属于当前场景，具有有效数值成分范围，且与任务范围有可行交集。沿用时保留原 ID 和实际版本，设计只在范围交集中进行，不复制成新假设；没有新假设就跳过准入。只有文字作用域或跨场景启发的卡片仍需形成适用于本任务的新假设。

`literature_retrieval` 提供各轮检索摘要，包含成功请求数、所得线索数和未完成的服务查询；本轮后续阶段及下一轮均能看到。服务未完成不等于没有相关文献，也不是假设的反证。全部文献服务暂时不可用时，仍可依据上下文中已有的森林证据研究；依据不足时允许返回空假设。

## 声明范围与条件

成分均为 wt.%。钢铁范围只需列参与研究的元素，未列出的非Fe元素按 `[0,0]` 处理；配方可省略零含量元素，Fe按余量闭合。HEA显式列出选用元素，不默认Fe基体。假设再声明自身范围，设计和搜索只能在其中进行。

无磁钢未知力学条件写 `"mechanical_conditions": {}`，已知条件用该工具支持的数值字段。HEA例如：

```json
{
  "composition_ranges_wt_pct": {"Co":[15,25],"Cr":[15,25],"Fe":[15,25],"Mn":[15,25],"Ni":[15,25]},
  "protocol": {"process_text":"制备工艺尚未确定", "testing":{"test_temperature_C":700}}
}
```

`protocol` 是共同条件。不填 `protocol_cases` 时，全部登记工具使用该条件；多条件时按下面的结构添加到 `space.json`：

```json
{
  "protocol_cases": {
    "magnetic": {
      "tools": ["property-lgbm-v1-remanence", "property-lgbm-v1-relative-permeability"]
    },
    "room": {
      "protocol": {"mechanical_conditions": {"test_temperature_C": 25}},
      "tools": ["nms-plate-mechanical"]
    },
    "hot": {
      "protocol": {"mechanical_conditions": {"test_temperature_C": 300}},
      "tools": ["nms-plate-mechanical"]
    }
  }
}
```

每组 `protocol` 递归覆盖共同条件，省略时继承共同条件；省略 `tools` 表示该场景全部工具。每个设计都使用同一组条件；条件组记录其工具实际提供的指标。示例中的温度只是输入写法，是否适合研究由模型适用范围和科学复审判断。无磁钢磁性工具只使用成分，力学工具使用声明的条件；将磁性单列一组可避免在不同温度下重复计算同一成分描述。耐蚀工具仍使用登记的固定比较条件。HEA 的强度与延伸率工具需要工艺和测试温度；单独调用密度工具可使用空协议，该模型不使用工艺或测试温度。

screening 指标在各自条件组检查；不能拿 room 的强度和 hot 的延伸率拼成同一条件下达标。measurement 指标进入待实测计划，diagnostic 指标只报告、不参与 BO 或 Pareto 排名。HEA 当前四项指标均为 diagnostic，默认使用空间填充。探索与对照可按研究问题选择测量端点，最终推荐的待测计划须覆盖场景验收端点；模型能算出数值不代表制造状态已经确定。

## 搜索与代表选择

LLM 设计给出明确成分点与实验计划。需要扩大探索时，同时给出 `search_ranges_wt_pct` 和 `plan.vary_elements`；只在该范围内改变这些元素，其余非余量元素保持不变。没有搜索范围的设计只评估明确点，算法不会自行扩大研究范围。

`space.json` 可选的搜索设置如下。未配置 `search` 时，随机种子使用轮次；配置后，省略的字段采用下列默认值：

```json
{
  "search": {
    "method": "auto",
    "pool_size": 256,
    "initial": 6,
    "batch_size": 4,
    "representatives": null,
    "seed": 0,
    "exploration_fraction": 0.25
  }
}
```

`auto` 在存在可排序 screening 指标时使用 BO，否则用 `space_filling`；也可显式选择 `bo` 或 `space_filling`。`pool_size` 是每次生成的内部候选点数，另加可行边界点；`initial` 是启动 BO 所需的空间内已观测点数，`batch_size` 是每次更新后选择的点数。`exploration_fraction` 为每批保留一部分空间覆盖点；`representatives` 控制供复审参考的代表数量，`null` 表示不额外限制，`0` 表示不提供代表建议。`seed` 控制可重复的采样。实际预测始终受 `--evaluations` 限制，不要求预算大于初始点数。

空间填充使用拉丁超立方内部点、可行边界点与最大最小距离选点。BO 使用本批已产生的预测选择下一批；多目标按各条件的指标分别比较，代表选择兼顾 Pareto 前沿、指标极值和成分多样性，对照与区分探针保留空间覆盖，不按达标程度淘汰。它们是选点与研究排序方法，不增加预测的证据等级。复审仍读取完整候选池，代表数量不裁剪科学记录，也不代替 `--recommendations`。

每轮 `search.json` 保存观测、搜索建议和选点轨迹。`search.py` 的 `run(spaces, evaluate, objectives, budget, *, options=None, prior=())` 只依赖通用搜索输入和评价回调，不读取场景、森林或文件；`recommend()` 单独决定建议形态。当前建议以 `kind: "point"`、`point`、`observation_ids`、`space_id` 表示，流程透传给复审。以后可在这一层实现范围建议；科学候选仍绑定实际预测点，范围不能因为内部几个点合格就被视为整体合格。

预测消耗分别记录为 `candidate_evaluations`（成分设计数）与 `model_evaluations`（实际调用的成分 × 条件组数）。例如每轮 10 个设计、3 组条件，最多执行 30 次模型组调用；同批完全相同的成分、协议和工具组合复用已有结果并保留原证据引用，不读取其他批次候选。

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

模型需要输出一个 JSON 对象。默认请求含 `response_format={"type":"json_object"}`；若网关不支持这个请求字段，可设置 `STEEL_FOREST_LLM_RESPONSE_FORMAT=none`。程序也接受完整包在 Markdown JSON 代码围栏中的对象。无效或截断的输出由 `services.LLMOutputError` 标明，campaign 将格式或记录问题反馈给模型纠正，不把无效内容作为研究结果。

每个 LLM 阶段最多请求 3 次，包括初次请求、重试和纠正，全部计入 `llm_calls`。HTTP 429、500、502、503、504，以及连接或超时错误按尝试次序等待 2 秒、4 秒后重试；输出纠正直接重发。有效记录与坏记录混在同一回答时，先请求纠正；额度或尝试次数用尽后仍可保留有效部分。`llm_requests.json` 记录阶段、请求次数、结果和错误。三次尝试仍无可用回答时以 `llm_stage_failed` 保存此前成果；先用尽调用额度则以 `budget_exhausted` 结束。SSL、鉴权、配置错误立即停止。

启用文献检索时至少配置一个服务：`BOHR_ACCESS_KEY` 使用 LKM，`DPRAG_API_KEY` 使用 DPRAG；不使用的密钥留空。campaign 使用 `auto`，两项都有值时会依次请求两者。每个服务对每个问题最多尝试 3 次；临时故障类型及等待时间与 LLM 相同。尝试次数耗尽后记录检索缺口，继续其他服务和问题。文献服务的 SSL、鉴权、配置及响应格式错误立即报错并停止批次。

返回内容是文献线索或片段，不代表完成原文核验。`--literature-queries 0` 不发起搜索规划或文献请求，可仅凭当前森林研究；没有依据时允许不提出假设。`usage.json` 中 `literature_queries` 统计研究问题数，`literature_requests` 统计服务请求尝试数，包含重试。

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
rows = list(literature('austenitic nonmagnetic steel manganese nitrogen', provider='auto', top_k=2))
print('线索数：', len(rows))
for row in rows:
    print(row['provider'], row['title'], row['url'], row['text'][:160])
PYTHON
```

直接调用 `ask()` 只请求一次；LLM 重试与输出纠正在 campaign 的 `request_stage` 内执行。单测一个文献服务时将 `provider='auto'` 改成 `'lkm'` 或 `'dprag'`。`literature()` 返回迭代器，每个服务的结果在请求下一服务前逐条交给调用方；示例用 `list()` 收集结果，campaign 则逐条保存。独立调用默认在重试和最终失败时发出警告，也可通过 `on_event` 回调接收请求状态；调试时传 `max_attempts=1` 可关闭重试。

零条线索可能来自成功但无结果，也可能来自临时故障耗尽，应结合请求状态判断。HTTP 401/403 时检查对应凭据与权限，404 时检查地址拼接和网关路径；LLM JSON 错误时检查模型输出及 `response_format` 支持情况，文献响应格式错误时检查对应服务。错误标明 LLM、LKM 或 DPRAG；campaign 还记录文献查询序号。不会打印密钥或响应正文。

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

LLM 预算逐次计算：例如 `--llm-calls 1` 可只运行第一步，已完成的阶段仍落盘；`0` 不调用 LLM。`--evaluations 0` 保留假设研究，跳过设计和预测。设计阶段默认为复审预留一次 LLM 请求，预算不足时不会启动昂贵的预测；`--allow-unreviewed` 可用于调试预测，未审结果保存在本轮 `evaluated.json`，不会因此进入森林或推荐列表。预测先考虑明确设计，再在剩余预算内搜索；超出预算的设计保留在 `design.json` 中但不执行。预算或轮次用完以 `budget_exhausted` 结束；已接受记录可合入森林，调试时使用 `--keep-private`。

预测逐条件更新 `evaluated.json`。单次预测的可恢复错误记入候选的 `evaluation_errors` 和对应 `case_*/error.json`，继续其他条件和候选；已成功的条件保留真实证据，失败条件不伪造数值。资产缺失、环境配置、工具版本等整体错误仍终止运行，并保留此前的条件结果。复审能看到预测缺口，交付检查仍要求每个必要条件的证据完整。

先用新版本执行 `--prepare-only` 检查范围和协议。正式运行失败时保留输入、已完成的各阶段JSON、逐候选原始输出和 `status.json`；文献结果逐条分配 ID 并写入 `literature.json`，后续服务或查询失败不会丢掉已取得的结果。每次文献请求先以 `running` 写入 `literature_requests.json`，结束后更新为 `retrying`、`succeeded` 或 `failed`；失败记录包含查询序号、服务和错误。`result.json` 与简报告保留检索缺口，不能仅凭 `literature.json` 存在或批次正常结束判断检索完整。修正输入或代码后开新版本重跑。没有隐式续跑、自动加预算或切换模型的分支。

每轮按研究问题的文字相关性读取假设，默认最多 12 张，可在 `forest.store.context` 的 `limit` 调整；同时读取它们引用的证据和显式关系。相关性相同时优先本场景，跨场景只沿带迁移条件的关系选入，并保留本场景关联假设，不预留固定名额。复审可选本轮候选和本批此前全部获准候选，记录不会混入其他 campaign。正式森林合并在批次结束时进行，采用启动时的森林版本；若其他批次先提交，会停止合并，保留本批 delta 与审查结果，待基于当前森林重新审查。

假设可引用本轮检索或森林中已接受的证据。准入只接受本轮的新证据；既有证据保留原记录，引用不会重复写入森林。

交付逐候选检查：不合格提名单独说明原因，其余合格候选保留。数量不足时报告仍列出这些部分成果与缺口，不把整批标为完成；研究复审同意结束且达到目标数量后才标为 `frozen_pending_measurement`。最终报告展示实际预测输入条件和待实测工艺计划。两者分别记录，不能把计划中的新工艺写成已经进入模型计算。成分按实际预测精度输出，不先圆整后复用旧预测值。
