# 用户问题改写 + 一致性自检 + 分级路由

RAG 检索前的**闭环**前处理模块。核心不是"把问题改写得更专业",而是**带自检和回退**:
改写可能跑偏,所以每一步都要能发现自己不可靠,并且永远有一条「用原问题检索」的兜底路径。

```
用户原问题
   │
   ├─ Rewriter     1 次轻量 LLM 调用 → 改写 + 自检 + 决策(严格 JSON)
   │
   ├─ HardChecker  0 token,纯规则 → 否定词/数字/实体/相似度 逐条复核
   │                规则判定优先于模型自检,任一不过 → 强制 use_original
   │
   ├─ Router       按最终 decision 分支 → 检索(或追问,不检索)
   │
   └─ 评估日志     logs/rewrite_YYYY-MM-DD.jsonl
```

---

## 调用方式

主入口只有一个函数:

```python
from utils.query_rewrite import plan_query

plan = plan_query(
    "那它续航多久",          # 用户原始问题原文 —— 不要先自己改写
    history=[("user", "扫地机器人续航一般多久"), ("assistant", "约 120 分钟。")],
    top_k=20, rerank_n=5,     # 由调用方现读设置后传入(保持「下次提问即生效」)
)

plan.decision    # 最终决策(已被规则层复核)
plan.rewritten   # 改写后的问题
plan.hits        # 检索结果 [{pk,text,score,file_name,...}];ask_clarify 时为空
plan.clarify     # ask_clarify 分支的追问文案(其余分支为空)
plan.fell_back   # 最终决策是否与 LLM 判断不同(即发生过回退/纠正)
```

在 agent 里由工具 `search_knowledge_base` 使用(见
`ReAct/tools/agent_tools.py`)。**该工具接收用户原问题原文**,闭环在其内部完成。

需要更细的控制时可直接组装:

```python
from utils.query_rewrite.pipeline import QueryRewritePipeline, clear_cache
from utils.query_rewrite.rewriter import Rewriter
from utils.query_rewrite.router import Router

pipe = QueryRewritePipeline(rewriter=Rewriter(), router=Router(), embedder=emb_model.embed_query)
clear_cache()   # 改词典/调阈值后清一次决策缓存
```

---

## 四个分支的行为

| decision | 行为 | 检索次数 | 何时出现 |
|---|---|---|---|
| `use_rewritten` | 只检索**改写后**的问题 | 1 | 自检三项全 true 且 `confidence >= conf_high` |
| `dual_retrieval` | **原问题与改写后的问题都检索**,按 `pk` 合并去重取高分 | 2(并行) | 置信度落在 `[conf_mid, conf_high)`,且非多意图 |
| `use_original` | 只检索**原问题**(兜底路径) | 1 | 规则拦下 / 置信度低于 `conf_mid` / 自检有 false / 未做改写 / 任何环节失败 |
| `ask_clarify` | **不检索**,返回追问文案 | 0 | 原问题模糊、指代无依据、多意图导致无法确定检索目标 |

**原问题永远可用**:上表里 `use_original` 是所有失败路径的终点。

### 规则硬校验(HardChecker)

在 LLM 之后运行,**不耗 token**。任一条不通过即覆盖 LLM 的 decision 为 `use_original`:

- **否定词数量一一对应** —— `不/非/除/无…` 计数必须一致(多字词优先,`不再` 不会被拆成 `不`)
- **数字/时间完全一致** —— 合法空白差异(「120 分钟」vs「120分钟」)归一;**单位换算判死**(120 分钟 ≠ 2 小时)
- **实体全部出现在改写结果里** —— 子串匹配(LLM 做部分归一化时不误杀);领域专名由规则层**从原问题重新抽取**,不依赖 LLM 自报
- **embedding 相似度 ≥ `sim_min`** —— 语义漂移兜底
- **多意图闸门** —— 三段及以上并列(「续航多久,噪音多大,售后怎么样」)禁止 `dual_retrieval`

### 降级与容错

每一层失败都能退回原问题,且 LLM/embedding 的抖动不会打断问答:

| 失败点 | 行为 |
|---|---|
| 无 API key / 建不出改写模型 | `use_original` |
| LLM 超时 / 返回空 | 重试 1 次 → 仍失败则 `use_original`(恰好 2 次调用,不无限重试) |
| JSON 解析失败 | 同上;解析层容忍 ```json 围栏与前后夹带的解释文字 |
| 未知 decision 值 | 按 `use_original` 处理(不猜) |
| embedding 不可用 | **跳过**相似度检查,其余规则照跑(不因此判死) |
| 双路其中一路检索失败 | 用另一路结果;仅两路全败才上抛给重试机制 |
| 检索基础设施故障 | `plan.error` 记录,由工具层转 `ToolRetryableError` 重试 |

### 缓存

进程内 LRU(`query → {改写结果, 硬校验, 原问题向量, 改写后向量}`),默认容量 512。
命中时**复用已算好的向量**,省掉一次 DashScope embedding 调用。键做了空白/大小写归一。

进程级、无 TTL、**不落库**(缓存键是用户原问题,持久化涉及隐私且收益有限)——
与项目既有的 `_agent_cache` / `vlm_tool._cache` 风格一致。`clear_cache()` 手动清空。

---

## 如何调阈值

所有阈值都在 [config/rag.yml](../../config/rag.yml) 的 `rewrite:` 段,**没有写死在代码里**。
改完重启生效(与 `model.yml` / `vector_store.yml` 一致)。

| 键 | 默认 | 调大的后果 | 调小的后果 |
|---|---|---|---|
| `conf_high` | `0.9` | 更多问题走双路(更稳,更慢) | 更多问题只用改写(更快,风险高) |
| `conf_mid` | `0.75` | 更多问题退回原问题 | 更多问题走双路 |
| `sim_min` | `0.75` | 更容易拦下改写(更保守) | 更容易放过改写(更激进) |
| `jaccard_clarify` | `0.6` | 追问更难通过,更多降级为原问题检索 | 更多追问 |
| `history_turns` | `3` | 指代消解上下文更多(费 token) | 更省 token,长距离指代可能消解不了 |
| `cache_size` | `512` | 内存占用上升 | 命中率下降 |
| `enabled` | `true` | — | `false` = 完全不调 LLM 与 embedding,直通原问题 |

调参工作流:

1. 跑一段真实问答,收集 `logs/rewrite_*.jsonl`
2. 看 `scores.similarity` 的分布定 `sim_min`;看 `violations` 里哪条规则最常拦下改写
   (如果全是「否定词数量不一致」,说明 prompt 该加强,而不是该放宽阈值)
3. 看 `llm_decision` vs `decision` 的偏离率:偏高说明阈值与模型判断不一致,需对齐
4. 改 `rag.yml` → 重启 → 复跑对比

**环境变量可临时覆盖阈值**(不用改文件,便于评测脚本):

```bash
REWRITE_SIM_MIN=0.9 REWRITE_CONF_HIGH=0.95 python -m utils.query_rewrite.pipeline "滤网多久换一次"
```

**运行时总开关**:`rag.yml` 的 `rewrite.enabled` 与 MySQL `app_settings` 的
`rag.rewrite_enabled` 是「与」关系,任一为 `false` 即关闭(关闭后零 LLM、零 embedding,
直接检索原问题)。DB 开关可在「系统配置」页所在的 `app_settings` 表直接改。

### 保护词典

同样在 `rag.yml`,可用 eval 脚本发现新专名后持续扩充:

- `negations` / `conditions` / `comparatives` / `quantifiers` —— 受保护的逻辑约束词
- `time_patterns` —— 时间词正则
- `domain_terms` —— 领域专名白名单(当前语料是扫地/扫拖机器人,含品类/部件/耗材/材料/
  技术/参数/品牌七类)
- `vague_markers` —— 模糊信号词

---

## 评估日志

每次调用追加一行 JSON 到 `logs/rewrite_YYYY-MM-DD.jsonl`(`log_enabled: false` 可关)。
字段:原问题、改写、`changed`、`changes`、各检测分数(`scores.similarity` /
`confidence` / `jaccard` / 各项自检)、`llm_decision`、重算后的 `decision`、
`fell_back` + `fallback_reason`、`violations`、`hit_count`、各阶段耗时。

按天切分便于用 pandas 直接读:

```python
import pandas as pd, glob
df = pd.read_json(sorted(glob.glob("logs/rewrite_*.jsonl"))[-1], lines=True)
df.decision.value_counts()                    # 各分支占比
df[df.fell_back].fallback_reason.value_counts()  # 回退原因分布
```

---

## 测试

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

50 个用例,**零新依赖**(标准库 unittest)、**不联网、不耗 token** ——
LLM / embedding / 检索全部替换为桩。覆盖:

- 指代消解(**有**上下文依据 → 采用改写;**无**依据时不得凭空补实体)
- 否定词保留(`不含甲醛` → `含甲醛` 必须被拦下)
- 数字保留(`120 分钟` → `2 小时` 必须被拦下)
- 多意图(三段并列不得走双路检索)
- 模糊问题(触发追问且**不检索**;模型实质改写了却要追问 → 降级)
- 边界:缓存命中不再调模型、开关关闭零调用、LLM 异常/坏 JSON 降级、
  embedding 失败跳过相似度、阈值可配且**真的生效**、双路一路失败降级

---

## 延迟:两个已定位并修掉的问题

从 `logs/rewrite_*.jsonl` 汇总真实运行数据(637 次改写),曾出现**改写 17~20 秒、
近半数失败**的情况。拆成平级阶段后才暴露出来。根因有两个,都已修:

### 问题 1:思维链把 `max_tokens` 吃光了(主要问题)

失败调用的真实报错(在 `logs/agent_*.log` 里搜「改写不可用」可见):

```
LengthFinishReasonError: ... completion_tokens=1500,
reasoning_tokens=1500          ← 全部预算被"思考"吃掉,JSON 正文无 token 可写
```

- **失败与输入长度强相关**:失败样本输入 11~33 字,成功样本 4~11 字 —— 问题越长,
  模型思考越久,越容易顶到 `max_tokens`。
- **`max_tokens: 1500` 全被推理 token 占用**,留给结构化 JSON 的是 0,于是每次
  都超时/解析失败,白等 ~19 秒(重试 2 次)。

**修法:关掉思维链**(`rag.yml` 的 `rewrite.disable_thinking: true`)。改写本质是
「照铁律机械执行」,规则和 few-shot 已把要求写死,模型不需要独立思考。忠实度由
HardChecker 的规则硬校验保证,不依赖模型的思考过程。实测:

| 问题 | 修复前 | 修复后 |
|---|---|---|
| 在我所在地区的天气下机器人如何保养 | 20.6s **失败** | 3.4s 成功 |
| 我的机器人耗电太快咋办 | 17.5s | 2.4s(**7×**) |
| 那它续航多久 | ~15s | 1.6s |

**注意 `extra_body` 必须构造期传**。实测把它放进 `invoke(..., config={"extra_body":…})`
会被 DashScope 忽略,思考照旧发生、延迟毫无改善 —— 这个坑踩过,所以
`modelfactory.get_chat_model` 专门加了 `extra_body` 参数(见
[model/modelfactory.py](../../model/modelfactory.py))。

### 问题 2:大量调用最终没采用改写(白花一次 LLM)

真实数据里 **10/17 次**改写的最终决策是 `use_original` —— 改写成果没被采用,
每次白等十几秒。这类问题的典型样子是「扫地机器人耗电快怎么办」:本身就是规范检索句。

**修法:规则前置直通**(`rag.yml` 的 `rewrite.skip_when_no_rewrite_needed: true`)。
对同时满足「已含领域专名 + 无指代词 + 无口语寒暄 + 长度足够」的问题,纯规则判定为
**无需改写**,直接跳过 LLM 用原问题检索 —— 零延迟零成本。上面两个失败样本现在都是
**0.0s**。

判定口径刻意保守(见 `glossary.looks_no_rewrite_needed`):宁可多调一次模型,也不要
因为误判丢掉指代消解/口语规范化的收益。带「咋/啥/那种」、含「它/这个」、或过短的
问题仍然照常走模型。

### 还能怎么压

延迟现在主要由**需要改写的那些问题**决定(2~3.5s)。想进一步压:

1. 换更小的非思维链模型(在 `rag.yml` 配 `rewrite.model`)
2. 调小 `rewrite.timeout`(默认 15s)—— 超时不会答错(降级 `use_original`),
   只是慢问题拿不到改写收益
3. 依赖决策缓存:同一问题的第二次提问改写耗时为 0

## 已知限制

### 一个踩过的坑:内部调用的流式回调必须切断

改写在**工具执行期间**调用 LLM,而工具跑在 agent 的 RunnableConfig 上下文里。
`langchain_core.runnables.config.ensure_config` 会从 `var_child_runnable_config`
继承配置,所以**不显式清空 callbacks 的话,改写的 invoke 会继承 agent 的推流回调**
—— 改写模型的 token 会被 `stream_mode="messages"` 当成回答推给前端并落库,
用户会看到一整段改写 JSON 夹在回答里。

已在真实环境复现(库里落了 4 条污染消息,内容形如
`正在为您检索...\n{"rewritten": ..., "self_check": {{...}}`,其中 `{{` 是 few-shot
原样未 format 的痕迹)。现在有两道防线:

1. **第一道**(`rewriter._invoke`):显式传
   `config={"callbacks": [], "metadata": {"lc_source": LC_SOURCE}}`,切断继承来的
   推流回调。代价:该次调用不再上报 LangSmith 等外部 tracing(token 用量仍由
   `_report_usage` 手动统计,计费口径不受影响)。
2. **第二道**(`ReAct/ReAct_Agent/agent.py` 的 `_is_internal_chunk`):流式循环里
   按 **`langgraph_node`** 丢弃工具节点产出的分片。

**第二道为什么看 node 而不是 lc_source —— 这是实测踩出来的**:真 DashScope + 真
agent 下打印流式分片的 meta,`metadata` 是**空的**(自定义 `lc_source` 不会透到
`stream_mode="messages"` 的 meta 里),所以只按 lc_source 过滤**实际拦不住**
(第一版就是这么写的,真机验证时泄漏依旧)。而 node 稳定可辨:

```
node='tools'  content='{"rewrit' ...  ← 工具内辅助调用的文本,必须丢弃
node='model'  content='正式回答。'      ← agent 主模型的正式回答
```

工具节点不会产出用户可见回答,所以"非 model 节点的文本一律丢"是安全的。

**改这两处前请先看测试**:`tests/test_rewrite_stream_leak.py` 里有两条会把泄漏
**故意复现**出来 —— `test_inner_llm_output_leaks_when_callbacks_not_cleared` 证明
泄漏路径真实存在,`test_leaked_chunks_come_from_tools_node` 证明泄漏分片的 node
就是 `'tools'`。如果哪天前者不再泄漏、或后者发现泄漏来自 `model` 节点,说明框架的
行为变了,这两道防线的判据都得重新评估,而不是庆幸问题消失了。

### 其余限制

1. **追问会多一次模型往返**。本架构是 LangGraph ReAct 循环,工具无法直接与用户对话 ——
   `ask_clarify` 只能把追问文案交回主模型转述,用户在**下一轮**才答复。要真正做到
   工具内中断并等待,需要的是 `interrupt()` 之类的人机交互机制,与「低延迟」目标冲突,
   故未采纳。

2. **改写模型复用 DashScope 的 base_url**。项目没有独立的兼容端点配置
   (`DASHSCOPE_BASE_URL` 为空时走 SDK 默认的
   `dashscope.aliyuncs.com/compatible-mode/v1`)。可在 `rag.yml` 用 `rewrite.base_url`
   显式覆盖;若该端点不可用,改写失败会降级为 `use_original`(不影响可用性,只是失去改写收益)。

3. **不做答案级校验**。需求里的第 4 项(生成答案后再判断是否回应了原问题)需要拿到
   「已检索到的资料」,而本模块运行在检索**之前**,拿不到;要做就必须再加一次 LLM 调用,
   与「省 token / 低延迟」直接冲突。建议作为独立主题,在 `agent.stream_output` 层用一次
   廉价检查实现。

4. **`dual_retrieval` 的双路 rerank 是额外成本**。两路各自要跑一次 embedding + rerank,
   已用并行把墙钟压到≈单路,但 API 调用仍是两次。默认阈值下触发频率不高。

5. **多意图检测是启发式**。两段式(「续航和噪音」)在中文里无法与比较意图区分,故一律
   不判多意图、保留 `dual_retrieval` 的机会。这个取舍偏向"宁可多花一次检索,不要误杀
   正常的比较类问题"。

---

## 工作流面板上的呈现

`search_knowledge_base` 属于**自管阶段**的工具(中间件的 `SELF_PHASED_TOOLS`):
`monitor_tool` 不为它发通用容器阶段,改由工具内部按业务顺序自己发两个**平级阶段**:

```
✓ 模型        理解问题,规划检索方案与调用工具
✓ 问题改写    补全/消歧/术语规范化,并自检是否忠实于原问题
             原问题:我的机器人耗电太快咋办 → 扫地机器人耗电快怎么办
             · 决策 采用改写 · 置信度 0.95 · 相似度 0.88
✓ 知识库检索  在知识库中向量召回 → rerank 精排 → 达标过滤
             检索词:扫地机器人耗电快怎么办 · 命中 5 条
✓ 模型        整合检索资料,生成最终回答
```

回退时会额外显示 `⚠ 违规项` 与 `已回退:<原因>`,便于一眼看出这次为什么没用改写。

为什么要"工具自己发阶段":改写发生在工具**内部**,而容器阶段在 `handler()` 之前
就发了,工具无法在其前面插入阶段 —— 否则面板会显示成「检索 → 问题改写」的语义倒序
(实测 `buildStages` 按事件顺序 append,确实会倒)。

阶段回调由 `plan_query(..., phase_emit=...)` 暴露;不注入时全程空操作,
CLI / 单测不受影响。
