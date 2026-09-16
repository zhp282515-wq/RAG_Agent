"""保护词典与约束抽取:改写铁律的「规则侧」实现,不耗 token。

设计原则:
  - 抽取函数是**纯函数**,不依赖 LLM。测试里可以直接断言输入输出。
  - 词表全部来自 config/rag.yml(可扩充),本模块只提供默认兜底,不硬编码业务词。
  - 否定词用 set 计数而非「出现次数」:同一词出现两次才算真不一致,避免
    「不提不」这类重复表述被误判(它本来就是个异常,但规则层不该靠计数制造假阳)。

数值抽取刻意保持保守:只认「阿拉伯数字 + 单位/百分号」和 yml 里的时间正则。
中文数字(一百二十)不认 —— 漏判的代价是「这条规则没拦住」,而漏判会被
embedding 相似度兜住;反过来,过度抽取(比如把「一体」当数字)会天天误杀改写。
"""
from __future__ import annotations

import re
from collections import Counter

from utils.query_rewrite import config
from utils.logger_tool import logger

# ---------- 内置词表兜底(rag.yml 未配该键时用) ----------

_DEFAULT_NEGATIONS = ["不再", "不能", "不会", "不要", "不用", "不是", "不", "没有", "没用",
                      "无法", "非", "除", "无", "未", "别", "莫", "免", "勿"]
_DEFAULT_VAGUE = ["怎么样", "如何", "好不好", "行不行", "怎么办", "什么情况",
                  "能不能", "值得吗", "划算吗"]
_DEFAULT_QUANTIFIERS = ["所有", "全部", "部分", "仅", "只", "每个", "任一", "任何", "大多数", "少数"]

# 阿拉伯数字 + 常见单位/百分号。含小数与千分位前段。
_NUM_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|倍|个|台|次|分钟|分|小时|时|天|周|月|年|厘米|毫米|米|㎡|平方米|"
    r"升|毫升|克|千克|公斤|度|分贝|dB|dB\(A\)|W|瓦|伏|V|mAh|毫安|Ah|lx|帕|kPa|ppm|mg|Pa)"
)
# 裸数字(无单位):「第 3 代」「档位 2」这类也要保留
_BARE_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_CJK_RE = re.compile(r"[一-鿿]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# 「已消费」占位符。用 Unicode 非字符 U+FFFF 而非空格/零宽字符:
#   - 空格会让「120 分钟」这类带空格的 token 被二次匹配;
#   - 零宽字符在日志/编辑器里不可见,排查时容易误判;
#   - U+FFFF 在合法文本里不可能出现,不会与任何词表/正则冲突。
_MASK = "￿"


def _words(key: str, defaults: list[str]) -> list[str]:
    """取词表:rag.yml 配了就用,否则用内置默认。按长度降序返回,
    使长词优先匹配(「不再」先于「不」),避免计数被短词拆碎。"""
    configured = config.get_list(key)
    words = configured or list(defaults)
    return sorted({w for w in words if w}, key=len, reverse=True)


# ---------- 抽取 ----------

def _count_terms(text: str, terms: list[str]) -> list[str]:
    """统计 text 中命中的词,返回按出现次数展开的列表(便于计数比对)。

    长词优先:命中「不再」后把该位置占掉,后续短词「不」不会再在同一位置重复计数
    —— 否则「不再需要」会被算成 {不再:1, 不:1},与改写成「不需要」的 {不:1}
    计数不同而误判,可两者语义其实一致(都不再/不需要)。
    """
    if not text:
        return []
    remaining = text
    out: list[str] = []
    for term in terms:
        cnt = remaining.count(term)
        if cnt:
            out.extend([term] * cnt)
            # 用等长占位符替换已匹配部分,使短词不会在同一位置重复计数(中文等长,安全)
            remaining = remaining.replace(term, _MASK * len(term))
    return out


def negations(text: str) -> list[str]:
    """否定词列表(多字词优先,已遮蔽重叠)。HardChecker 用它做一一对应比对。"""
    return _count_terms(text, _words("negations", _DEFAULT_NEGATIONS))


def conditions(text: str) -> list[str]:
    return _count_terms(text, _words("conditions", []))


def comparatives(text: str) -> list[str]:
    return _count_terms(text, _words("comparatives", []))


def quantifiers(text: str) -> list[str]:
    return _count_terms(text, _words("quantifiers", _DEFAULT_QUANTIFIERS))


def numbers(text: str) -> list[str]:
    """数字/时间/单位抽取:带单位的数字优先,其余位置再收裸数字。

    归一化只做「去空格」——「120 分钟」与「120分钟」视为同一数字。
    刻意**不做单位换算**:120 分钟 → 2 小时 属于改变了数值表达,规则层应当判死
    (需求明确要求「数字/时间必须完全一致」)。

    实现要点:先收集所有「数字+单位」匹配的区间,再**按区间重建**剩余文本。
    不能简单对原文 replace —— 「续航 120 分钟」去掉「120 分钟」后若按字符数
    等长遮蔽,遮蔽符会覆盖掉数字本身,裸数字正则又会在原文(未遮蔽)上再匹配一次
    「120」,同一个数值被记成两种写法,导致原问题/改写后比对时假阳性。
    """
    if not text:
        return []
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in _NUM_RE.finditer(text):
        out.append(m.group(0).replace(" ", ""))
        spans.append(m.span())
    # 未被子串遮蔽的位置:按区间重建,保留数字之间的分隔符
    if spans:
        rest_parts: list[str] = []
        cursor = 0
        for s, e in spans:
            rest_parts.append(text[cursor:s])
            cursor = e
        rest_parts.append(text[cursor:])
        rest = "".join(rest_parts)
    else:
        rest = text
    out.extend(m.group(0) for m in _BARE_NUM_RE.finditer(rest))
    return out


def time_tokens(text: str) -> list[str]:
    """时间词:按 rag.yml 的 time_patterns 正则抽取。"""
    if not text:
        return []
    out: list[str] = []
    for pat in config.get_list("time_patterns"):
        try:
            out.extend(m.group(0) for m in re.finditer(pat, text))
        except re.error as e:
            # 用户改坏了正则不能让整条链路挂掉
            logger.warning(f"query_rewrite.glossary: 时间正则 {pat!r} 非法,已跳过({e})")
    return out


def numbers_and_times(text: str) -> list[str]:
    """数字 + 时间的合并视图,给 HardChecker 做整体一致性比对。

    返回**去重后的有序列表**(集合语义),原因:numbers() 与 time_tokens() 的
    覆盖面天然重叠 —— yml 里那条 '\\d+\\s*(?:...|分钟|年)' 会同时命中 numbers()
    已收过的「120 分钟」。不去重的话,原问题与改写后的问题只要一方少写一次
    同样的表达(如把「续航 120 分钟」简写),计数就会假阳性。

    校验语义即「数值/时间表达的**集合**必须一致」:同一数值重复出现两次还是
    一次,不改变检索目标。

    同时在合并处做**最后一次空白归一**:numbers() 收的是去空格的 '120分钟',
    而 time_tokens() 的正则直接取原文匹配,会拿到带空格的 '120 分钟'。两者是
    同一个数值的两种写法,必须在比较前统一(否则原问题写「120 分钟」、改写后写
    「120分钟」会被判成不一致 —— 这是纯粹的形式差异)。
    """
    tokens = { "".join(t.split()) for t in (set(numbers(text)) | set(time_tokens(text))) }
    return sorted(tokens)


def domain_terms(text: str) -> list[str]:
    """原问题命中的领域专名(白名单子串命中)。"""
    if not text:
        return []
    return [t for t in _words("domain_terms", []) if t in text]


# ---------- 意图/模糊度启发式 ----------

def _split_count(query: str, seps: tuple[str, ...], max_span: int = 12) -> int:
    """数一下 query 被「实质并列」切开后有几段。

    实质并列 := 找到一个连词,其**前面最近的连词之后**到它之间 ≥3 个汉字,
    且它**之后**到下一个连词之前 ≥3 个汉字,且整段跨度不太大。这样把
    「续航和噪音」判成 2 段,而「扫地和拖地功能的区别」里那个「和」因为右侧
    (拖地功能的区别) 与后续没有别的连词、整体跨度超限而不会被误算成多意图。
    """
    cut = False
    for sep in seps:
        start = 0
        while True:
            i = query.find(sep, start)
            if i < 0:
                break
            start = i + len(sep)
            left, right = query[:i], query[i + len(sep):]
            if len(_CJK_RE.findall(left)) < 3 or len(_CJK_RE.findall(right)) < 3:
                continue
            if len(_CJK_RE.findall(left)) + len(_CJK_RE.findall(right)) > max_span * 2:
                continue
            cut = True
    return 2 if cut else 1


def looks_multi_intent(query: str, max_span: int = 12) -> bool:
    """启发式判断是否「多意图」。

    判定口径:把问题按**并列连词或子句标点**切开,若切出 ≥3 段各含 ≥3 汉字的
    实质子问题,则判为多意图。

    为什么不是「有连词就算」:「扫地和拖地功能的区别」是单一意图(问区别),
    含「和」但只有一个问题,误判会白白丢掉 use_rewritten 的收益。
    为什么不是「≥2 段就算」:「续航和噪音」这类两段式在中文里经常是**一个**
    比较意图(问两者对比),判成多意图会让它丢掉双路检索 —— 而比较类问题恰恰是
    双路检索最有价值的场景(两种表述召回不同分片)。故门槛定在**三段及以上**。

    切分符同时包含标点(逗号/分号/顿号/问号):「续航多久,噪音多大,售后怎么样」
    是典型的多意图,靠连词是切不出来的。

    两个阈值各管一件事:
      - 每段 ≥2 汉字:中文里大量检索对象是双字名词(续航/噪音/滤网),不能因为
        字少就当它不构成意图。取 3 会让「续航以及噪音以及售后」漏判。
      - 段数 ≥3:这是硬门槛。2 段式的「和」在中文里无法与比较意图区分 ——
        「续航和噪音」和「扫地和拖地的区别」都是 2 段,靠切分判不出来。
        因此 2 段一律**不判多意图**,交给 dual_retrieval 处理。
        这个取舍是刻意的:dual 只是"更稳妥"的选项,退成 use_original 也仍然会
        检索、仍然不答非所问;而误判多意图会让多问题串悄悄走上双路检索、
        白花两倍检索成本。
    """
    if not query:
        return False
    seps = ["以及", "还有", "同时", "和", "与", "及", "并且", "而且", "或者", "或",
            ",", ",", ";", ";", "、", "?", "?", "。", "!", "!"]
    pattern = "|".join(re.escape(s) for s in seps)
    substantive = [p for p in re.split(pattern, query) if len(_CJK_RE.findall(p)) >= 2]
    return len(substantive) >= 3


def is_vague(query: str) -> bool:
    """模糊信号:含「怎么样/如何」这类词,且整句很短(没有具体落脚点)。

    长度上限是为了区分「保养怎么样做才对」与「它怎么样」:前者虽然含「怎么样」,
    但它有明确主体和动作,不该触发追问。
    """
    if not query or len(_CJK_RE.findall(query)) > 10:
        return False
    return any(v in query for v in _words("vague_markers", _DEFAULT_VAGUE))


def has_reference(query: str) -> bool:
    """是否含指代词(它/这个/那个…)。指代消解必须有上下文依据,否则保留原样。"""
    if not query:
        return False
    return any(p in query for p in ("它", "这个", "那个", "这", "那"))


# ---------- 文本相似度工具(供 HardChecker 用) ----------

def jaccard(a: str, b: str) -> float:
    """字符级 Jaccard 相似度,用于「LLM 声称要追问时,改写是否其实没动」的校验。

    只用 1-gram:2-gram 对中文短问句过于稀疏(「它怎么样」vs「它怎么样」之外几乎全为 0),
    会让「实质未改写」的判定失真。字符串相同时直接返回 1.0,避免空交集/空并集的边界。
    """
    a, b = (a or "").strip(), (b or "").strip()
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def cosine(v1: list[float], v2: list[float]) -> float:
    """余弦相似度。维度不符/空向量返回 -1(调用方据此判为不可用而跳过该检查)。"""
    if not v1 or not v2 or len(v1) != len(v2):
        return -1.0
    dot = sum(x * y for x, y in zip(v1, v2))
    n1 = sum(x * x for x in v1) ** 0.5
    n2 = sum(y * y for y in v2) ** 0.5
    if n1 == 0 or n2 == 0:
        return -1.0
    return dot / (n1 * n2)


def clarify_is_plausible(original: str, rewritten: str, changed: bool) -> bool:
    """LLM 给 ask_clarify 时,判断这个追问是否合理。

    合理的追问(如「它怎么样」)特点:改写**几乎没动** —— 原问题本身就缺信息,
    没什么可改的。不合理的追问特点:模型实质改写了问题(说明它已经理解了意图),
    却回头说要追问,这是答非所问,应当降级为 use_original。

    用 Jaccard 而非 embedding:这里判的是「字面几乎没动」;emb 反而会让
    「换了个说法但意思没变」也通过,与「实质改写」的语义相悖。阈值可配
    (rag.yml 的 jaccard_clarify)。
    """
    if not changed:
        return True
    return jaccard(original, rewritten) >= float(config.get("jaccard_clarify") or 0.6)


def counter_diff(orig: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """返回 (原问题多出来的, 改写多出来的)。两侧都空则 ([]) 表示一一对应。"""
    co, cn = Counter(orig), Counter(new)
    return sorted((co - cn).elements()), sorted((cn - co).elements())


def looks_non_chinese(query: str) -> bool:
    """几乎不含汉字(纯英文/符号)——改写与规则校验对这类输入收益低。

    用于让 HardChecker 对「英文产品名原样查询」放宽:这类查询本来就该直通。
    """
    return not _CJK_RE.search(query or "") and bool(_LATIN_RE.search(query or ""))


# 口语/寒暄标记:这些是改写的主要收益来源(删口语、换书面语),命中就不该跳过改写。
# 刻意**不含**「吗/么」—— 那是疑问句的结构标记(「…怎么清洗吗」),不是可删的填充词,
# 把它算进来会让大量正常问句被误判为需要改写。
_COLLOQUIAL = ("咋", "啥", "麻烦", "请问", "帮我", "我想", "那个", "那种", "这玩意",
               "呀", "吧", "呢", "嘛", "呗", "哈", "哩", "哦", "咯",
               "啊", "啦", "哎", "哟", "喔", "唉", "欸")


def looks_no_rewrite_needed(query: str, min_cjk: int = 8) -> bool:
    """启发式判断「这条问题不需要改写」——用于**跳过 LLM 调用**,直接检索原问题。

    依据实测:真实运行里 10/17 次改写最终都落在 use_original(改写成果未被采用),
    每次白花一次模型调用。这类的典型样子是:
      「扫地机器人耗电快怎么办」「扫地机器人耗电快怎么办」——本身就是规范的检索句。

    判定为「无需改写」须**同时**满足(保守口径,宁可多调一次也别丢掉改写收益):
      1. 已含领域专名(说明未用「机器人/它/这玩意」这类模糊指代)
      2. 不含指代词(它/这个/那个…,指代消解必须结合上下文,规则判不了)
      3. 不含口语/寒暄标记(删口语正是改写的价值所在)
      4. 长度足够(短问题如「怎么配对」多半是模糊问题,交给 LLM 走 ask_clarify 更合适)

    返回 True 时调用方直接用原问题检索,零 LLM 延迟、零成本。
    """
    q = (query or "").strip()
    if not q:
        return False
    if not domain_terms(q):
        return False
    if has_reference(q):
        return False
    if any(c in q for c in _COLLOQUIAL):
        return False
    return len(_CJK_RE.findall(q)) >= int(min_cjk)
