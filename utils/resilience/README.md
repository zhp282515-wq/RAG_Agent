# 统一失败处理组件(重试 / 熔断 / 降级 / 观测)

全项目所有外部调用——LLM、工具、HTTP、向量库——共用这一套。调用点**只提供两样东西**:
降级函数和调用点标识(`site`);重试几次、等多久、什么错该重试、什么时候熔断、怎么记日志,
全部由本组件按 `config/resilience.yml` 统一处理。

纯标准库实现,不引入任何新依赖。

## 一、为什么需要它

接入之前,项目里同一件事有四份实现:

| 调用点 | 接入前的做法 |
|---|---|
| 工具(同步 HTTP) | `retry_util.with_retry` + `http_get_json`,有分类、有退避抖动 |
| 外部 MCP 工具(异步) | 自己一套退避算式(**漏了抖动**)+ 自己的 `_looks_retryable` 文本匹配 |
| 查询改写 LLM | 手写 `for attempt in (1,2)`,**无退避**,调用失败与解析失败混为一谈 |
| 摘要 LLM | **三层叠加**:langchain 自带 `with_retry` + 自建 `_fail_streak` 熔断 + try/except |
| embedding / rerank | **完全没有重试**,超时/限流直接抛 |

后果是退避参数四套、分类口径不一、熔断只覆盖一处、跨调用点看不到任何指标。

## 二、错误分类

分类原则:**重试可能改变结果的 → 瞬时;重试结果一样的 → 确定性。**

| 类别 | 判定 | 处理 |
|---|---|---|
| **瞬时** | 连接错误/重置/DNS 失败/管道断裂、超时、429、408、425、全部 5xx、响应体截断或非 JSON、上游「临时不可用」 | 指数退避 + 随机抖动重试 |
| **确定性** | 400/401/402/403/404/405/409/410/422/451、内容审核拒绝、业务拒绝(余额不足/配额耗尽)、工具参数校验失败 | **不重试**,不等待,不消耗重试次数,直接降级 |
| **未分类** | 上面都匹配不上 | **不重试**(按未知处理),立即降级并单独计数 |

第三条是刻意的:把「不认识」当成「可重试」只会把未知故障放大一遍。这条也与项目原有
契约一致(以前 `with_retry` 只重试 `ToolRetryableError`)。

**告警**:确定性错误里属于**配置/代码问题**的那几类(401/402/403/404、余额不足、内容审核)
会额外触发告警。这类错误重试无用,通常意味着 key 过期、模型名写错或参数越权,必须有人看到。
告警按 `(site, code)` 限流 60 秒,避免一个失效的 key 把日志刷爆。

分类器是**可扩展**的:调用点用 `register_exception_kind()` 注册自己的规则,优先级高于所有内置规则,
不需要改核心。MCP 传输层特征词就是这么接进去的(见 `ReAct/tools/external_tool_wrap.py`)。

## 三、重试与退避

```
delay = min(base * 2**retry_index, max_delay)      # retry_index 从 0 起
sleep = delay + random(0, delay * jitter_ratio)
```

`retry_index` 从 0 起是刻意为之:首次重试的退避等于 `base`,与原 `base * 2**(attempt-1)`
(attempt 从 1 起)数值完全一致,接入时不会改变既有节奏。

**抖动不是可选项**。下游抖动时,一批同时失败的调用若在同一时刻重试,会形成尖峰把它再打垮一次。

**429 的 `Retry-After` 优先于算出来的退避**,并用 `retry_after_cap`(默认 60s)封顶——
上游偶尔会给一个天文数字,不封顶会把交互式问答直接卡死。503 的 `Retry-After` 同样会读。

**分场景重试次数**(都不是写死的,可在 yml 里调):

| 场景 | 重试次数 | 理由 |
|---|---|---|
| 交互式(用户等待) | 1 | 用户在等,重试要克制 |
| 后台 / 批处理 | 3 | 没人等,值得多试 |
| 流式(已开始输出) | 0 | 重放会产生重复 token |

**两条必须知道的限制**:

1. **超时预算**:`timeout_budget` 是单次调用(含全部重试)的总耗时上限,超了立即降级且不再等待。
   - 异步路径用 `asyncio.wait_for`,能**真正取消**在途请求;
   - 同步路径无法中断已经发出的阻塞调用(`requests` / OpenAI SDK 都不支持),预算只能在
     **重试边界**生效。同步调用的硬超时仍然依赖各 SDK 自己的 `timeout` 参数。
2. **幂等**:`idempotent=False` 时有效重试次数被强制为 0。有副作用的调用(写库、发消息、扣费)
   重放一次可能造成真实损失,宁可降级也不冒这个险。强制生效时会打一条 `idempotent_guard` 日志,
   免得日后有人纳闷「为什么没重试」。

## 四、熔断

按 `site` 一个实例。状态机:`CLOSED → OPEN → HALF_OPEN → CLOSED`。

- **滑动窗口**内瞬时失败达阈值(默认 5 次 / 60s)→ 打开;
- 打开期间**直接降级,`fn` 根本不被调用**;
- 冷却(默认 30s)到期放探针;探针成功 → 关闭并复位;探针失败 → 回 OPEN 且冷却
  按因子指数延长(封顶),避免在持续故障期被反复试探;
- **确定性错误不触发熔断**——重试结果一样的错误,熔断也改变不了什么,只告警;
- 计时用**注入的单调时钟**,不是墙上时间,防止 NTP 校时回拨让失败时间戳「来自未来」而窗口永不清理。

熔断状态是**进程内**的。多 worker 部署(如 `uvicorn --workers N`)会各自熔断,不做跨进程共享。

## 五、降级

**每个调用点必须提供降级函数**,签名 `fallback(exc, attempts) -> 兜底值`。
没有降级路径的调用点在签名层面就无法成立,这是刻意的:失败不允许把异常甩给用户。

降级方式按调用点需要选:返回兜底值、走缓存、退回规则逻辑(如查询改写退回原问题)、
或返回一个明确可处理的错误标识(如摘要返回 `None` 表示本轮不压缩)。

两条约定:
- 熔断短路时,降级函数收到的是 `CircuitOpenError` 且 `attempts=0`——让降级函数能区分
  「调用真的失败了」和「压根没调用」;
- **降级函数自己抛错属于编程错误**,组件记一条 `degrade_error` 后原样抛出,不静默吞掉。
  否则「降级也是坏的」这种状态永远发现不了。

## 六、可观测性

**指标**(进程内、尽力而为、不持久化),按 `site` 维度聚合,传 `labels` 可再按
模型/租户/工具切分:

```python
from utils.resilience import metrics_snapshot
metrics_snapshot()["sites"]["rewriter"]
# calls / attempts / successes / failures / success_rate / failure_rate
# transient / deterministic / unknown / 各占比
# retries / retry_histogram / avg_backoff / avg_duration
# degradations / degradation_reasons
# cache_hits / cache_misses / breaker_opens / breaker_rejections
# deterministic_by_code
```

**结构化日志**,每次调用/重试/降级/熔断/告警各一行,便于 grep:

```
【resilience】 event=retry site=get_weather attempt=1 total=3 kind=transient code=http_503 wait=1.2 elapsed=0.0 err=ToolRetryableError
【resilience】 event=breaker_open site=summarization failures=5/5 window=60.0 cooldown=300.0
【resilience】 event=degrade site=rewriter reason=timeout attempts=2 err=APITimeoutError
【resilience】 event=alert site=rewriter code=auth status=401 message="bad api key"
```

**脱敏**:降级与告警日志里的消息会剥掉 `sk-…`、`Bearer …`、`api_key=…`、长 hex/base64,
避免凭据随日志外泄。

**告警**默认写独立文件 `logs/resilience_alert_<日期>.log`(与常规日志分开,因为它是「需要人看的」信号)。
还可以挂回调接邮件/webhook:

```python
from utils.resilience import register_alert_hook
register_alert_hook(lambda site, code, msg, detail: send_to_slack(f"[{site}] {code}: {msg}"))
```

## 七、怎么用

### 直接调用

```python
from utils.resilience import call

def my_fallback(exc, attempts):
    return "兜底值"

result = call(fetch_price, "扫地机A", site="price_api", fallback=my_fallback)
```

策略参数一个都不传也能跑——全从 `config/resilience.yml` 按 `site` 取。
需要覆盖时显式传,显式传的优先级最高:

```python
result = call(fn, site="batch_job", fallback=fb,
              retries=3, base=0.5, timeout_budget=60, idempotent=False)
```

### 装饰器

```python
from utils.resilience import guard

@guard(site="price_api", fallback=my_fallback, retries=2)
def fetch_price(sku: str) -> float: ...
```

自动识别同步/异步,并用 `functools.wraps` 保住 `__name__`/`__doc__`/`__signature__`——
这对本项目很关键:`agent_tools.py` 是 `@tool` 套在重试装饰器**外层**,签名丢了 langchain
的工具 schema 会直接崩。

### 并发:部分成功

```python
from utils.resilience import map_partial

values, errors = map_partial([f1, f2, f3], max_workers=3)
```

单个失败不影响其他,能用的先用,永不抛异常。

## 八、接入一个新的调用点

三步:

**1. 在 `config/resilience.yml` 加自己的 site 段**(可选,不加就用全局默认):

```yaml
sites:
  my_new_feature:
    max_retries: 2
    base_delay: 0.3
    breaker_cooldown_seconds: 60
```

**2. 写降级函数。** 问自己一句:这个调用失败时,调用链路上最合理的「退一步」是什么?
返回空值?退回规则逻辑?还是明确告诉上层「不可用」?**不能没有**。

**3. 包一层 `call` / `guard`:**

```python
from utils.resilience import call

def _fallback(exc, attempts):
    logger.warning(f"my_new_feature 不可用({type(exc).__name__}): {exc}")
    return None                      # 上层据此走它的兜底分支

def _do():
    return client.do_something(...)  # 失败就让它抛,交给组件分类

result = call(_do, site="my_new_feature", fallback=_fallback)
```

要点:
- **不要自己写 try/except 吞异常**——那正是这套组件要消除的重复。让异常抛出来,
  组件负责分类、重试、降级、记指标。
- **不要自己写退避/sleep**。
- 如果这个调用点有特殊的错误类型需要特殊归类,用 `register_exception_kind()` 注册,别改核心。
- 有副作用就传 `idempotent=False`。

## 九、调参

全部在 `config/resilience.yml`,优先级从低到高:

```
内置默认 < yml 顶层键 < yml 的 sites.<site> 段 < 环境变量 RESILIENCE_<键名> < 调用处显式 kwargs
```

环境变量对临时调试特别顺手(不用改文件、不用动调用点):

```bash
# 全局:让所有调用点的首次退避变成 0.1s
RESILIENCE_BASE_DELAY=0.1 python start.py

# 单个调用点:把改写的重试彻底关掉
RESILIENCE_SITES_REWRITER_MAX_RETRIES=0 python start.py
```

常用旋钮与「往哪边拧」:

| 想达到的效果 | 调什么 |
|---|---|
| 交互链路更跟手(宁可少重试) | 调小该 site 的 `max_retries`;调小 `base_delay` |
| 泛洪下游,给它喘息 | 调大 `base_delay` / `jitter_ratio` |
| 不要在故障期白等 | 调小 `timeout_budget`;调小 `breaker_cooldown_seconds` 让探针更快试 |
| 减少误熔断 | 调大 `breaker_failure_threshold` 或 `breaker_window_seconds` |
| 某调用点数据实时性强,不能缓存 | 保持 `cache_enabled: false`(默认即是) |

配置**不做运行时热加载**:改完重启生效(与 `model.yml` / `rag.yml` 一致)。
`resilience.yml` 整个文件缺失也不会出事——loader 会退回内置默认,因为「兜底层」自己不该先炸。

## 十、各调用点的接入现状

| 调用点 | site | 说明 |
|---|---|---|
| 检索工具 | `get_rerank_retriever` / `search_knowledge_base` | 经 `retry_util.with_retry` 适配,**调用点零改动** |
| 天气 / 定位工具 | `get_weather` / `get_user_location` | 同上 |
| 外部 MCP 工具 | `mcp:<工具名>` | `ExternalToolWrapper._arun` |
| 查询改写 LLM | `rewriter` | 缓存关闭(决策缓存已由 `pipeline` 按 query 做) |
| 上下文压缩 LLM | `summarization` | 冷却 300s 保持原行为 |
| 主对话流式链路 | — | **尚未接入**,见下方「已知边界」 |

### 已知边界

- **主对话流式链路(`ReAct_Agent/agent.py`)未接入**。流式已开始吐 token 后不能重放,
  重试语义与其它调用点不同,需要单独设计。
- **embedding / rerank 未接入**。`modelfactory.py` 目前超时/限流直接抛。接入时要特别注意:
  `embed_documents` 按 2 条分批是模型硬约束(超 2 条报 **400**),这个 400 属于确定性错误,
  绝不能被重试放大成批量失败风暴。
- **熔断状态是进程内的**,多 worker 部署各自为政。
- **指标不持久化**,进程重启即清零。

## 十一、跑测试

```bash
python -m pytest tests/resilience -v      # 本组件(约 120+ 用例,毫秒级)
python -m pytest tests/ -q                # 全量,含既有查询改写测试
```

测试全部基于假可调用对象与构造异常,不碰任何真实外部服务;`sleep`/抖动/时钟都做了打桩,
所以既瞬时又确定。装置见 `tests/resilience/conftest.py`。
