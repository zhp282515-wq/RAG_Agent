"""MySQL 数据层：连接管理、自建 agent_sessions 库与业务表、LangGraph checkpointer。

会话历史按「会话窗口」持久化的存储入口（库名固定为 agent_sessions，由 .env 的
MYSQL_DATABASE 指定默认值）。本模块对外暴露：

    get_connection(database=True)   -> pymysql 连接（autocommit + DictCursor）
    get_checkpointer()              -> langgraph 官方 PyMySQLSaver(MySQL 持久化 checkpointer)
    init_db()                       -> 幂等建库/建表，可反复调用

业务表 agent_sessions / agent_session_messages 存"会话窗口 + 用户可见消息"(详见
utils/session_store.py)；langgraph 官方 saver 所需的 checkpoints/checkpoint_blobs/
checkpoint_writes/checkpoint_migrations 6 张表同建在本库，用于每会话的多轮记忆。
"""

import os

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

from utils.logger_tool import logger

load_dotenv(override=True)

# 会话业务库名：需求明确库名就叫 agent_sessions，.env 的 MYSQL_DATABASE 作为入口默认值
SESSION_DB = os.getenv("MYSQL_DATABASE", "agent_sessions")

# 业务表 DDL（建在 agent_sessions 库内；会话/消息均 JSON 化，避免过多小表）
# 注意:不写死旧 COLLATE——随服务器默认排序规则(MySQL8=utf8mb4_0900_ai_ci)建表,
# 与 pymysql 连接默认排序规则一致,否则 langgraph saver 的 json_table 会报 1267 排序规则冲突。
_BUSINESS_DDL = [
    """
    CREATE TABLE IF NOT EXISTS agent_sessions (
        session_id VARCHAR(64)  NOT NULL,
        updated_at VARCHAR(32)  NOT NULL,      -- 升序续写/排序统一用 strftime 字符串,无时区坑
        title      VARCHAR(100) NOT NULL DEFAULT '新会话',
        PRIMARY KEY (session_id),
        KEY idx_updated_at (updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_session_messages (
        id         BIGINT AUTO_INCREMENT PRIMARY KEY,
        session_id VARCHAR(64)  NOT NULL,
        role       VARCHAR(20)  NOT NULL,      -- user | assistant
        payload    MEDIUMTEXT   NOT NULL,      -- 消息体 JSON(内容/图片/工具步骤),见 session_store
        ts         VARCHAR(32)  NOT NULL,
        KEY idx_session_ts (session_id, ts, id),
        CONSTRAINT fk_agent_messages_session FOREIGN KEY (session_id)
            REFERENCES agent_sessions(session_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS reports (
        report_id  INT AUTO_INCREMENT PRIMARY KEY,
        user_id    VARCHAR(32)  NOT NULL,      -- 报告所属用户(用户id,来自聊天链路提取)
        month      VARCHAR(16)  NOT NULL,      -- 报告月份 YYYY-MM
        title      VARCHAR(255) NOT NULL DEFAULT '',
        content    LONGTEXT     NOT NULL,      -- 报告 Markdown 全文
        created_at VARCHAR(32)  NOT NULL,
        updated_at VARCHAR(32)  NOT NULL,
        KEY idx_reports_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # 全局系统设置(k/v JSON;由 web「系统配置」页读写,服务端运行时常读最新值)
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        skey       VARCHAR(64)  NOT NULL,
        value_json MEDIUMTEXT   NOT NULL,      -- 设置值 JSON(数字/字符串/小结构)
        updated_at VARCHAR(32)  NOT NULL,
        PRIMARY KEY (skey)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    # 可插拔工具注册表:内置(7 默认)可启停 + 外部 MCP 工具注册挂载
    """
    CREATE TABLE IF NOT EXISTS tool_registry (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        tool_key    VARCHAR(64)  NOT NULL,      -- 唯一标识(内置=tool 函数名;外部=注册 key)
        kind        VARCHAR(16)  NOT NULL,      -- builtin | external
        label       VARCHAR(100) NOT NULL,      -- 展示名(中文)
        enabled     TINYINT(1)   NOT NULL DEFAULT 1,
        transport   VARCHAR(16)  NOT NULL DEFAULT '',  -- stdio | http (external only)
        config_json MEDIUMTEXT   NOT NULL,      -- external: {"command"/"url","args":[]} 等
        created_at  VARCHAR(32)  NOT NULL,
        updated_at  VARCHAR(32)  NOT NULL,
        UNIQUE KEY uq_tool_key (tool_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]


def _env_cfg(database: str | None) -> dict:
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": database,
        "charset": "utf8mb4",
    }


def get_connection(database: bool = True) -> pymysql.Connection:
    """返回一个新的短生命周期连接(autocommit + DictCursor)。

    Args:
        database: True 时连 agent_sessions 库;False 时连默认库(不指定库),用于建库。
    """
    cfg = _env_cfg(SESSION_DB if database else None)
    return pymysql.connect(
        **cfg,
        autocommit=True,
        cursorclass=DictCursor,
    )


def _execute_statements(conn: pymysql.Connection, statements: list[str]) -> None:
    with conn.cursor() as cur:
        for stmt in statements:
            if not stmt.strip():
                continue
            try:
                cur.execute(stmt)
            except pymysql.err.OperationalError as e:
                # 幂等重跑:表/列/索引已存在等"结构已就绪"错误直接忽略
                if e.args[0] in (1061, 1060, 1091):
                    continue
                raise


def init_db() -> None:
    """自建 agent_sessions 库并初始化全部表。幂等,可重复调用。"""
    # 1) 建库(连默认库,不指定 database)
    # 用服务器默认字符集/排序规则(MySQL8=utf8mb4_0900_ai_ci)建库,
    # 使库、连接、langgraph saver 三者排序规则一致,避免 1267 排序规则冲突。
    conn = get_connection(database=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{SESSION_DB}` "
                f"DEFAULT CHARACTER SET utf8mb4"
            )
        logger.info(f"init_db: 已确保数据库 `{SESSION_DB}` 存在")
    except Exception as e:
        logger.error(f"init_db: 建库失败: {e}")
        raise
    finally:
        conn.close()

    # 2) 业务表
    conn = get_connection(database=True)
    try:
        _execute_statements(conn, _BUSINESS_DDL)
    finally:
        conn.close()

    # 2.5) 全局系统设置默认值(app_settings 建表后种子化;已有行不覆盖)
    try:
        from utils.settings_store import ensure_defaults
        ensure_defaults()
    except Exception as e:
        logger.warning(f"init_db: 初始化全局设置默认值失败(将回退内置默认):{e}")

    # 3) langgraph checkpointer 表
    # 直接逐条执行官方 MIGRATIONS(每条 DDL 幂等:表用 IF NOT EXISTS,索引/列已存在
    # 报 1061/1060/1091 由 _execute_statements 忽略;MySQL ALTER 单语句原子,不会半途破坏)。
    # 不使用 saver.setup():它按 checkpoint_migrations 版本号重放,重复执行会撞重复索引。
    from langgraph.checkpoint.mysql.base import MIGRATIONS

    conn = get_connection(database=True)
    try:
        _execute_statements(conn, MIGRATIONS)
    finally:
        conn.close()


# 全局单例 checkpointer(长连接由 saver 通过连接工厂按需新建,天然可重连)
_checkpointer = None


def get_checkpointer():
    """返回 langgraph 官方 PyMySQLSaver(会话记忆用 MySQL 持久化)。

    传入"连接工厂"而非单一连接:每次执行都拿一条新连接,避免长连接失效。
    """
    global _checkpointer
    if _checkpointer is None:
        from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

        def _factory():
            return get_connection(database=True)

        _checkpointer = PyMySQLSaver(_factory)
    return _checkpointer


if __name__ == "__main__":
    init_db()
    print(f"init_db done: 库 `{SESSION_DB}` 已就绪(业务表 + langgraph checkpointer 表)")
