"""可插拔工具注册表:内置 8 工具(可启停) + 外部 MCP 工具(注册挂载)。

设计要点:
    - 内置 8 工具(agent_tools)为默认工具,默认全开,可在「系统配置 → 工具」逐个启停。
      引擎耦合工具(search_knowledge_base / get_rerank_retriever / fill_context_for_report)仍可关:
      关闭后不被绑定,中间件 monitor_tool 按名字符串特判的分支因工具不执行而自然不触发,
      无需改中间件。
    - 外部 MCP 工具注册后,经 langchain-mcp-adapters 加载为 BaseTool,再包一层
      ExternalToolWrapper(对齐 @@TOOL_ERROR@@ / 自动重试),统一进 agent。
    - 启停状态 + 外部注册清单持久化到 MySQL tool_registry 表(幂等 DDL + seed)。
    - _rev:进程内单调递增版本号;任何启停/注册变更都 bump。web server 端用其比对,
      变化时重建(丢缓存)缓存 agent → 「下次提问即生效」。
    - 关键进程级缓存:_external_tools_cache[record_id] = [BaseTool,...],MCP server
      每次提问不重复拉起(慢);仅在注册/删除/rev 变化时刷新。
"""
import asyncio
import json
import re
import threading
from datetime import datetime

from utils.mysql_tool import get_connection
from utils.logger_tool import logger


def run_coro_in_loop(coro) -> any:
    """在任意上下文(含已运行的事件循环,如 FastAPI async 端点)执行协程。

    asyncio.run 在已有 loop 时抛 RuntimeError;此 helper 若检测到当前线程有运行中
    loop,则丢到一个后台线程里新建 loop 执行并等结果 —— 供 MCP 同步加载/探测用。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中 loop:直接建新 loop
        return asyncio.run(coro)

    # 已在 loop 内:换线程跑
    result: dict = {}

    def _runner():
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            result["error"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")

# ---------------- 内置工具清单 ----------------
# 从 agent_tools 引已 @tool 包装后的对象;key 即其工具名(与中间件/DB 一致)
def _builtin_inventory() -> list[dict]:
    from ReAct.tools import agent_tools as at
    specs = [
        ("search_knowledge_base", "知识库检索(改写闭环)", "检索", "传原始问题,系统自动改写+自检+分级路由后检索,改写不可靠时自动回退原问题", True),
        ("get_rerank_retriever", "知识库检索(直通)", "检索", "给定检索词直接向量召回+rerank精排,不做改写", True),
        ("get_weather", "天气查询", "外部数据", "按城市查实时天气/湿度等", True),
        ("get_user_location", "IP定位城市", "外部数据", "自动定位用户所在城市", True),
        ("get_current_month", "当前月份", "系统", "取系统当前月份/日期", True),
        ("get_user_id", "用户核对", "系统", "核对指定用户ID是否存在", True),
        ("fetch_external_data", "外部使用数据", "外部数据", "按用户+月份取外部业务数据", True),
        ("fill_context_for_report", "报告上下文", "报告", "报告场景注入上下文并触发报告提示词", True),
    ]
    out = []
    for key, label, group, desc, default_on in specs:
        fn = getattr(at, key, None)
        if fn is None:
            logger.warning(f"tool_registry: 内置工具 {key} 在 agent_tools 中不存在,跳过")
            continue
        out.append({
            "key": key,
            "kind": "builtin",
            "label": label,
            "group": group,
            "description": desc,
            "default_enabled": default_on,
            "fn": fn,
        })
    return out


BUILTIN_TOOLS = _builtin_inventory()

# 保留键 = 全部内置 key(外部注册不得重名/占用)
def reserved_keys() -> set[str]:
    return {t["key"] for t in BUILTIN_TOOLS}

# 记录(展示)层暴露,不暴露 fn
def builtin_meta() -> list[dict]:
    return [{k: t[k] for k in ("key", "kind", "label", "group", "description", "default_enabled")}
            for t in BUILTIN_TOOLS]

# 引擎耦合工具的 soft 提示(UI 展示;仍允许关)
ENGINE_SOFT_NOTES = {
    "search_knowledge_base": "关闭将禁用知识库检索(推荐入口,含改写闭环)",
    "get_rerank_retriever": "关闭将禁用直通检索(改写闭环仍可用)",
    "fill_context_for_report": "关闭将禁用报告自动生成流程",
}

# ---------------- 进程内状态 ----------------
_rev = 0
_rev_lock = threading.Lock()
# 外部工具加载缓存:key = tool_registry.id(int) -> {"records":..., "tools": list[BaseTool]}
_external_cache: dict[int, dict] = {}

TS_FMT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return datetime.now().strftime(TS_FMT)


def _rev_now() -> int:
    global _rev
    with _rev_lock:
        _rev += 1
        return _rev


def get_rev() -> int:
    with _rev_lock:
        return _rev


# ---------------- 注册表持久化(MySQL tool_registry) ----------------

def _row_to_dict(r) -> dict:
    try:
        cfg = json.loads(r["config_json"] or "{}")
    except (TypeError, ValueError):
        cfg = {}
    return {
        "id": r["id"],
        "key": r["tool_key"],
        "kind": r["kind"],
        "label": r["label"],
        "enabled": bool(r["enabled"]),
        "transport": r["transport"] or "",
        "config": cfg,
    }


def ensure_registry() -> None:
    """幂等建表 + 种子全部内置行(须在 init_db 之后调用)。

    已存在行(用户改过 enabled 或曾删过外部)不动;只补缺失内置行。
    """
    try:
        # 确保表存在(独立于 mysql_tool.init_db 也能用)
        from utils.mysql_tool import _BUSINESS_DDL, _execute_statements
        conn = get_connection()
        try:
            _execute_statements(conn, [ddl for ddl in _BUSINESS_DDL if "tool_registry" in ddl])
        finally:
            conn.close()

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                for t in BUILTIN_TOOLS:
                    cur.execute(
                        "INSERT IGNORE INTO tool_registry "
                        "(tool_key, kind, label, enabled, transport, config_json, created_at, updated_at) "
                        "VALUES (%s, 'builtin', %s, %s, '', '{}', %s, %s)",
                        (t["key"], t["label"], 1 if t["default_enabled"] else 0, _now(), _now()),
                    )
        finally:
            conn.close()
        _rev_now()  # 初始版本号 ≥1
        logger.info(f"tool_registry: 内置 {len(BUILTIN_TOOLS)} 个默认工具已就绪")
    except Exception as e:
        logger.error(f"tool_registry: 初始化工具注册表失败:{e}")


def list_tools() -> list[dict]:
    """全量工具列表(内置+外部),enabled 取当前 DB 状态。"""
    rows = _query_all()
    merged: list[dict] = []
    # 内置按 BUILTIN_TOOLS 顺序;外部在其后
    for meta in builtin_meta():
        row = rows.get(meta["key"])
        d = {**meta,
             "enabled": bool(row["enabled"]) if row else meta["default_enabled"],
             "config": {}, "transport": ""}
        merged.append(d)
    for r in rows.values():
        if r["kind"] == "external":
            merged.append(r)
    return merged


def _query_all() -> dict[str, dict]:
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM tool_registry")
            rows = cur.fetchall()
        return {r["tool_key"]: _row_to_dict(r) for r in rows}
    except Exception as e:
        logger.error(f"tool_registry: 查询工具注册表失败:{e}")
        return {}


def set_builtin_enabled(key: str, enabled: bool) -> bool:
    """开/关一个内置工具(仅内置可改)。返回 False 表示 key 非法/不存在。"""
    if key not in reserved_keys():
        return False
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tool_registry SET enabled = %s, updated_at = %s WHERE tool_key = %s AND kind = 'builtin'",
                (1 if enabled else 0, _now(), key),
            )
        _rev_now()
        return True
    except Exception as e:
        logger.error(f"tool_registry: 开关内置工具失败 {key}:{e}")
        return False


def _set_external_enabled(key: str, enabled: bool) -> bool:
    """开/关一个外部工具(仅外部可改)。返回 False 表示不存在。"""
    if key in reserved_keys():
        return False
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE tool_registry SET enabled = %s, updated_at = %s "
                "WHERE tool_key = %s AND kind = 'external'",
                (1 if enabled else 0, _now(), key),
            )
            if cur.rowcount == 0:
                return False
        _rev_now()
        return True
    except Exception as e:
        logger.error(f"tool_registry: 开关外部工具失败 {key}:{e}")
        return False


_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def validate_external_key(key: str) -> str | None:
    """外部 key 校验:非空、允许字符、非保留、不与已有重复。返回错误消息或 None。"""
    if not key or not _KEY_RE.match(key):
        return "工具 key 仅允许字母数字下划线(1-64 字符)"
    if key in reserved_keys():
        return f"「{key}」是内置工具名,不可用作外部工具 key"
    if key in _query_all():
        return f"工具 key「{key}」已存在"
    return None


def _normalize_external(transport: str, config: dict) -> tuple[str, dict]:
    """校验并规范化外部工具 transport/config;非法抛 ValueError。返回 (transport, cfg)。"""
    if transport not in ("stdio", "http"):
        raise ValueError("transport 须为 stdio 或 http")
    if transport == "http":
        url = str(config.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("http 外部工具须提供有效 url(http/https)")
        return transport, {"url": url}
    cmd = str(config.get("command") or "").strip()
    if not cmd:
        raise ValueError("stdio 外部工具须提供 command")
    args = [str(a) for a in (config.get("args") or []) if str(a).strip()]
    return transport, {"command": cmd, "args": args}


def add_external_tool(*, key: str, label: str, transport: str, config: dict) -> dict:
    """新增外部 MCP 工具注册行(不在此加载;加载在 resolve_enabled_tools)。返回注册行。"""
    err = validate_external_key(key)
    if err:
        raise ValueError(err)
    transport, cfg = _normalize_external(transport, config)
    now = _now()
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tool_registry "
                "(tool_key, kind, label, enabled, transport, config_json, created_at, updated_at) "
                "VALUES (%s, 'external', %s, 1, %s, %s, %s, %s)",
                (key, (label or key)[:100], transport, json.dumps(cfg, ensure_ascii=False), now, now),
            )
            new_id = cur.lastrowid
        _rev_now()
        return {"id": new_id, "key": key, "kind": "external", "label": label or key,
                "enabled": True, "transport": transport, "config": cfg}
    except Exception as e:
        logger.error(f"tool_registry: 注册外部工具失败:{e}")
        raise


def update_external_tool(*, key: str, label: str | None = None,
                         transport: str | None = None, config: dict | None = None,
                         enabled: bool | None = None) -> dict | None:
    """更新外部工具(label/transport/config/enabled;仅提供者才改)。

    key 为已存在外部工具的 key;transport/config 同时提供时一起规范化(校验通过才落)。
    返回更新后的外部行 dict;不存在/是内置返回 None。
    """
    if key in reserved_keys():
        return None
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM tool_registry WHERE tool_key = %s AND kind = 'external'", (key,))
            row = cur.fetchone()
        if row is None:
            return None
    except Exception as e:
        logger.error(f"tool_registry: 更新外部工具查询失败 {key}:{e}")
        return None

    updates: dict[str, object] = {"updated_at": _now()}
    if label is not None:
        updates["label"] = (str(label).strip() or key)[:100]
    if enabled is not None:
        updates["enabled"] = 1 if enabled else 0
    if transport is not None or config is not None:
        # 两者须同时给(transport 决定 config 结构);只给一个则沿用旧的另一项
        cur_t = (transport or row["transport"] or "stdio")
        cur_cfg = config if config is not None else (json.loads(row["config_json"] or "{}") if row["config_json"] else {})
        _t, norm_cfg = _normalize_external(cur_t, cur_cfg)
        updates["transport"] = _t
        updates["config_json"] = json.dumps(norm_cfg, ensure_ascii=False)
    try:
        sets = ", ".join(f"{k} = %s" for k in updates)
        params = list(updates.values()) + [key]
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE tool_registry SET {sets} WHERE tool_key = %s AND kind = 'external'", tuple(params))
        _external_cache.clear()      # 配置变了,旧加载结果作废 → 下次懒加载
        _rev_now()
        # 返回更新后的行
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM tool_registry WHERE tool_key = %s AND kind = 'external'", (key,))
            nr = cur.fetchone()
        return _row_to_dict(nr) if nr else None
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"tool_registry: 更新外部工具失败 {key}:{e}")
        return None


def remove_external_tool(key: str) -> bool:
    """删除外部工具(内置不可删)。返回 False=不存在/是内置。"""
    if key in reserved_keys():
        return False
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM tool_registry WHERE tool_key = %s AND kind = 'external'", (key,))
            deleted = cur.rowcount > 0
        if deleted:
            _external_cache.clear()  # 整体失效:某外部被删,懒加载下次重建
            _rev_now()
        return deleted
    except Exception as e:
        logger.error(f"tool_registry: 删除外部工具失败:{e}")
        return False


# ---------------- 外部 MCP 工具加载 ----------------
def _build_connection(transport: str, config: dict) -> dict:
    if transport == "http":
        return {"transport": "http", "url": config.get("url", "")}
    return {"transport": "stdio", "command": config.get("command", ""),
            "args": list(config.get("args") or [])}


async def _load_mcp_tools_async(transport: str, config: dict) -> list:
    """异步加载一个外部 MCP server 的全部工具(adapter 转换 BaseTool)。"""
    from langchain_mcp_adapters.tools import load_mcp_tools
    connection = _build_connection(transport, config)
    return await load_mcp_tools(None, connection=connection, handle_tool_errors=False)


def _load_external_record(rec: dict) -> list:
    """加载单条外部工具注册记录的 BaseTool(带进程级缓存)。失败返回 []。"""
    rid = rec.get("id")
    cached = _external_cache.get(rid) if rid is not None else None
    if cached is not None:
        return cached["tools"]
    try:
        raw = run_coro_in_loop(_load_mcp_tools_async(rec["transport"], rec["config"]))
        tools = []
        for t in raw:
            from ReAct.tools.external_tool_wrap import wrap_external_tool
            tools.append(wrap_external_tool(t, tool_label=rec.get("label") or rec["key"]))
        if rid is not None:
            _external_cache[rid] = {"tools": tools, "records": rec}
        return tools
    except Exception as e:
        logger.error(f"tool_registry: 加载外部工具 {rec['key']}({rec.get('transport')})失败:{e}")
        return []


def probe_external(transport: str, config: dict) -> tuple[bool, str, list[dict]]:
    """注册前连通探测:起一次 MCP 会话并列出工具名。返回 (ok, msg, tools_summary)。"""
    try:
        raw = run_coro_in_loop(_load_mcp_tools_async(transport, config))
        from ReAct.tools.external_tool_wrap import describe_external_tool
        summary = [describe_external_tool(t) for t in raw]
        return True, f"连接成功,发现 {len(summary)} 个工具", summary
    except Exception as e:
        return False, f"连接失败:{type(e).__name__}: {str(e)[:160]}", []


# ---------------- 组装可用工具 ----------------
def resolve_enabled_tools() -> list:
    """返回当前应绑定到 agent 的工具列表(启用内置 + 启用的外部已加载)。

    每次 agent 构建调用(重建不频繁);内部启用外部工具时逐条懒加载并缓存,
    加载失败的外部工具跳过(agent 仍可用,仅少该外部能力)。
    """
    rows = _query_all()
    tools = []

    # 1) 启用的内置(按 BUILTIN_TOOLS 顺序)
    for meta in builtin_meta():
        row = rows.get(meta["key"])
        enabled = bool(row["enabled"]) if row else meta["default_enabled"]
        if enabled:
            t = next(b for b in BUILTIN_TOOLS if b["key"] == meta["key"])
            tools.append(t["fn"])

    # 2) 启用的外部(注册先后序)
    for r in rows.values():
        if r["kind"] == "external" and r["enabled"]:
            loaded = _load_external_record(r)
            tools.extend(loaded)
    return tools


def enabled_count_log() -> str:
    """日志辅助:当前启用集摘要。"""
    names = [getattr(t, "name", "") for t in resolve_enabled_tools()]
    return f"{len(names)} 个工具:{names}"


def _builtin_label(key: str) -> str:
    """内置工具 key → 中文标签。"""
    for m in builtin_meta():
        if m["key"] == key:
            return m["label"]
    return key


def enabled_capability_block() -> tuple[str, list[str]]:
    """生成追加到系统提示词的「当前已启用工具清单 + 处理规则」,及已停用内置清单。

    目的:提示词里静态写全了 8 个内置工具的能力说明(供模型理解"有哪些能力")。
    但**停用某工具后**,必须让模型明确知道该能力当前不可用,才不会假装调用后中断。
    本函数据 tool_registry 当前启停状态生成两段追加文字:
        - 已启用的工具清单(内置 + 外部),模型只能调用这里列出的;
        - 处理规则:问到未启用/未绑定的能力 → 直接告诉用户"该功能未启用/暂不可用"。
    返回 (appendix_text, disabled_builtin_labels);disabled_builtin_labels 供界面/日志用。
    """
    rows = _query_all()
    enabled_builtin: list[str] = []
    disabled_builtin: list[str] = []
    for meta in builtin_meta():
        row = rows.get(meta["key"])
        on = bool(row["enabled"]) if row else meta["default_enabled"]
        if on:
            enabled_builtin.append(f"- {meta['label']}({meta['key']})")
        else:
            disabled_builtin.append(meta["label"])

    # 启用的外部工具
    external_names: list[str] = []
    for r in rows.values():
        if r["kind"] == "external" and r["enabled"]:
            external_names.append(f"- {r.get('label') or r['key']}({r['key']})")

    lines: list[str] = []
    if enabled_builtin:
        lines.append("## 当前已启用的内置能力")
        lines.extend(enabled_builtin)
    if external_names:
        lines.append("## 当前已接入的外部能力(MCP 工具)")
        lines.extend(external_names)
    lines.append("")
    lines.append("## 关于未启用/不可用能力的处理规则(重要)")
    lines.append("- 只能调用上面「已启用」清单里列出的工具;系统提示词正文中其它能力说明仅作参考,不代表当前可用。")
    if disabled_builtin:
        lines.append(f"- 以下内置能力**当前已停用**,不得调用,也不得假装调用或假装已执行:【{'、'.join(disabled_builtin)}】。")
        lines.append("- 若用户询问的是已停用能力(如实时天气、某类数据查询、报告生成等),直接如实告知:"
                    "「该功能当前未启用,暂不可用」,不要尝试调用、不要编造结果、不要用其它工具冒充。")
    lines.append("- 若用户请求不在上述任何能力范围内,说明没有对应能力/未接入该数据源,而不是模拟执行。")
    return "\n".join(lines), disabled_builtin


def append_capability_block(system_prompt: str) -> str:
    """在静态系统提示词末尾追加当前能力清单块(agent 构建时调用)。"""
    block, _ = enabled_capability_block()
    if not block:
        return system_prompt
    return system_prompt.rstrip() + "\n\n" + block + "\n"
