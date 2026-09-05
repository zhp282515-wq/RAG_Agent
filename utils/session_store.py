"""会话窗口存储：按 MySQL 库 agent_sessions 持久化会话及其消息。

会话(session window)= 一个 session_id 下若干条 user/assistant 消息。一条 session_id
贯穿三层：
  - 引擎记忆:agent 运行 config {"configurable": {"thread_id": session_id}} 即按会话续聊
    (该记忆存于 langgraph checkpointer,即 utils/mysql_tool 的 6 张表);
  - 落库窗口:本模块把"用户可见消息"(user/assistant)写入
    agent_sessions / agent_session_messages 两张业务表;
  - 图片/工具步骤等富信息随消息 payload JSON 一并落库,供后续界面回显。

对外主接口:
    create_session / reuse  -> 返回 {"id","title",...}
    append_message(session_id, role, payload_dict)
    list_sessions / get_session / delete_session / get_session_title
"""

import json
import uuid
from datetime import datetime

from utils.logger_tool import logger
from utils.mysql_tool import get_connection

# 与 agent_sessions 库一致的统一时间格式(排序/去时区坑)
TS_FMT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return datetime.now().strftime(TS_FMT)


def _new_id() -> str:
    return uuid.uuid4().hex


def _ser(d: dict) -> dict:
    """把 datetime 等转成可 JSON 序列化的值。"""
    return {
        k: v.strftime(TS_FMT) if isinstance(v, datetime) else v
        for k, v in d.items()
    }


def _title_from(text: str, limit: int = 20) -> str:
    """从首条文本生成会话标题(截断 + 省略号)。"""
    cleaned = " ".join(str(text).strip().split())
    if not cleaned:
        return "新会话"
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")


def create_session() -> dict:
    """新建一个会话窗口。若已有空会话(没有任何消息)则直接复用,避免堆空会话。"""
    now = _now()
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT s.session_id, s.title, s.updated_at "
            "FROM agent_sessions s LEFT JOIN agent_session_messages m "
            "  ON m.session_id = s.session_id "
            "WHERE m.id IS NULL ORDER BY s.updated_at DESC LIMIT 1"
        )
        existing = cur.fetchone()
        if existing:
            logger.debug(f"create_session: 复用空会话 {existing['session_id']}")
            return _ser(existing)

        session_id = _new_id()
        cur.execute(
            "INSERT INTO agent_sessions (session_id, updated_at, title) "
            "VALUES (%s, %s, %s)",
            (session_id, now, "新会话"),
        )
        logger.info(f"create_session: 新建会话 {session_id}")
        return {"session_id": session_id, "title": "新会话", "updated_at": now}


def get_or_create_session(session_id: str | None) -> dict:
    """指定 session_id 则确保其存在(续聊);为 None/空则新建。返回会话信息。"""
    session_id = (session_id or "").strip()
    if session_id:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT session_id, title, updated_at FROM agent_sessions "
                "WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
        if row:
            return _ser(row)
        # 指定的会话不存在:按显式 id 补齐一行(续聊语义:不存在的会话当新会话)
        now = _now()
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_sessions (session_id, updated_at, title) "
                "VALUES (%s, %s, %s)",
                (session_id, now, "新会话"),
            )
        logger.info(f"get_or_create_session: 会话 {session_id} 不存在,已新建")
        return {"session_id": session_id, "title": "新会话", "updated_at": now}
    return create_session()


def append_message(
    session_id: str,
    role: str,
    payload: dict,
    *,
    title_from: str | None = None,
) -> None:
    """追加一条消息到会话窗口。

    Args:
        session_id: 目标会话
        role: "user" | "assistant"
        payload: 消息体 dict(内部会并入 role/ts 再序列化为 payload JSON 列)。
                 结构参考 utils/image_input_tool 与 agent.py 组装约定。
        title_from: 首条 user 消息可给该文本,用于把"新会话"标题改成首问截断。
    """
    now = _now()
    body = dict(payload)
    body.setdefault("role", role)
    body["ts"] = body.get("ts") or now
    payload_json = json.dumps(body, ensure_ascii=False)

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_session_messages (session_id, role, payload, ts) "
            "VALUES (%s, %s, %s, %s)",
            (session_id, role, payload_json, now),
        )
        # 首条 user 消息时把会话标题设为截断的首问
        if title_from is not None:
            cur.execute(
                "UPDATE agent_sessions SET title = %s, updated_at = %s "
                "WHERE session_id = %s AND title = '新会话'",
                (_title_from(title_from), now, session_id),
            )
        else:
            cur.execute(
                "UPDATE agent_sessions SET updated_at = %s WHERE session_id = %s",
                (now, session_id),
            )


def _decode_payload(raw: str) -> dict:
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {"content": str(obj)}
    except (json.JSONDecodeError, TypeError):
        return {"content": raw}


def list_sessions() -> list[dict]:
    """返回所有会话窗口,按最近更新倒序。"""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT session_id, title, updated_at FROM agent_sessions "
            "ORDER BY updated_at DESC"
        )
        return [_ser(dict(r)) for r in cur.fetchall()]


def get_session(session_id: str) -> dict | None:
    """取单个会话窗口(含按序排好的消息,消息已反序列化 payload)。"""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT session_id, title, updated_at FROM agent_sessions "
            "WHERE session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        session = _ser(dict(row))
        cur.execute(
            "SELECT role, payload, ts FROM agent_session_messages "
            "WHERE session_id = %s ORDER BY ts, id",
            (session_id,),
        )
        messages = []
        for r in cur.fetchall():
            msg = _decode_payload(r["payload"])
            msg.setdefault("role", r["role"])
            msg.setdefault("ts", r["ts"])
            messages.append(msg)
        session["messages"] = messages
        return session


def get_session_title(session_id: str) -> str | None:
    """查会话标题(不存在返回 None)。"""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT title FROM agent_sessions WHERE session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
        return row["title"] if row else None


def rename_session(session_id: str, title: str) -> bool:
    """重命名会话标题(清空后视为回到"新会话")。返回是否更新成功。

    注:不能用 UPDATE rowcount 判断存在性——MySQL 默认 rowcount 只统计"实际变更行",
    新标题与旧标题相同时为 0,会误判为不存在。故先确认行存在再更新。
    """
    cleaned = " ".join(str(title).strip().split())
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agent_sessions WHERE session_id = %s", (session_id,))
        if not cur.fetchone():
            return False
        cur.execute(
            "UPDATE agent_sessions SET title = %s WHERE session_id = %s",
            (cleaned or "新会话", session_id),
        )
        return True


def delete_session(session_id: str) -> bool:
    """删除会话窗口及其消息(agent_session_messages 由外键级联删除)。

    注:langgraph 记忆(thread=session_id 的 6 张表行)由调用方另行清理,
    本函数只负责业务表。
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM agent_sessions WHERE session_id = %s", (session_id,))
        return cur.rowcount > 0


if __name__ == "__main__":
    s = create_session()
    print("created:", s)
    sid = s["session_id"]
    append_message(sid, "user", {"content": "扫地机器人滤网多久清洗一次?"}, title_from="扫地机器人滤网多久清洗一次?")
    append_message(sid, "assistant", {"content": "建议每 1~2 周清洗一次,视使用频率而定。"})
    got = get_session(sid)
    print("get_session title:", got["title"])
    for m in got["messages"]:
        print(" -", m["role"], "|", m.get("content", "")[:40])
    print("deleted:", delete_session(sid))
