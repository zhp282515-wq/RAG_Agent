"""全局系统设置存储(MySQL app_settings 表,k/v JSON)。

「系统配置」页(检索三件套 / 对话模型+温度 / 工具)的持久化层。与 config/*.yml
的区别:本层存**运行时可改**的项,供服务端在每次提问/检索前现读,做到
「下次提问即生效」;yml 是静态兜底默认。

约定:
    - skey 即设置键(如 retrieval.top_k),一行一值,value_json 存该值的 JSON。
    - 读:get_json(key, default),每次现读(本地单用户,往返开销可忽略),
      DB 不可用/未初始化时优雅回退 default,不抛断工具链路。
    - 写:set_json(key, value),INSERT ... ON DUPLICATE KEY UPDATE(幂等)。
    - ensure_defaults():把默认值幂等种进表(不覆盖用户已改的值)。
"""
import hashlib
import json
import os
from datetime import datetime

from utils.mysql_tool import get_connection
from utils.config_tool import model_conf
from utils.logger_tool import logger

# ---------------- 默认值(与 config/*.yml 及 agent_tools 实测校准一致) ----------------
# 检索三件套:向量召回候选 / rerank 精排数 / 高相关阈值 / 达标(注入)线
RETRIEVAL_DEFAULTS: dict[str, float | int] = {
    "retrieval.top_k": 20,
    "retrieval.rerank_n": 5,
    "retrieval.score_high": 0.85,
    "retrieval.score_min": 0.60,
}
# 对话模型:默认模型(回退 model.yml model)/ 温度(默认 0.7)
MODEL_DEFAULTS: dict[str, object] = {
    "model.default": model_conf.get("model", "").split(":", 1)[-1] or "",
    "model.temperature": 0.7,
}

_DEFAULTS = {**RETRIEVAL_DEFAULTS, **MODEL_DEFAULTS}

TS_FMT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return datetime.now().strftime(TS_FMT)


def _db_unavailable(e: Exception) -> None:
    logger.warning(f"settings_store: 数据库不可用,将使用默认值:{e}")


def get_json(key: str, default=None):
    """读一条设置(现读 DB)。未存/DB 不可用 → 返回 default。"""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value_json FROM app_settings WHERE skey = %s", (key,))
            row = cur.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, ValueError):
            return default
    except Exception as e:
        _db_unavailable(e)
        return default


def set_json(key: str, value) -> None:
    """写一条设置(upsert)。"""
    raw = json.dumps(value, ensure_ascii=False)
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_settings (skey, value_json, updated_at) "
                "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE value_json = VALUES(value_json), "
                "updated_at = VALUES(updated_at)",
                (key, raw, _now()),
            )
    except Exception as e:
        logger.error(f"settings_store: 写设置失败 key={key}:{e}")
        raise


def get_int(key: str, default: int) -> int:
    try:
        return int(get_json(key, default))
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float) -> float:
    try:
        return float(get_json(key, default))
    except (TypeError, ValueError):
        return default


def get_str(key: str, default: str = "") -> str:
    v = get_json(key, default)
    return str(v) if v is not None else default


def ensure_defaults() -> None:
    """把默认设置幂等种进表(INSERT IGNORE;已有行不动 → 用户改值保留)。

    返回后所有 _DEFAULTS 键在 DB 均有行,get_* 读不到缺省的情况消失。
    """
    try:
        rows = [(k, json.dumps(v, ensure_ascii=False), _now()) for k, v in _DEFAULTS.items()]
        with get_connection() as conn, conn.cursor() as cur:
            for k, raw, ts in rows:
                cur.execute(
                    "INSERT IGNORE INTO app_settings (skey, value_json, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (k, raw, ts),
                )
    except Exception as e:
        _db_unavailable(e)


def all_settings() -> dict:
    """读全部设置(供 GET /api/settings),返回 {skey: 值}。"""
    out: dict = {}
    for k, v in _DEFAULTS.items():
        out[k] = get_json(k, v)
    return out


# ================= API Key(敏感项:加密落库,不进 all_settings / 不回传前端) =================
# 用户可在「系统配置 → 对话模型」填自己的 DashScope API Key,供全部模型服务
# (对话/embedding/rerank/VLM)使用。**key 完全由前端填存 DB,不走 .env**——
# 环境变量不再作为来源,避免残留 key 与用户填写 key 串用。
# 为便于多用户/多租户演进:get_dashscope_key(tenant=None) 预留维度参数。
KEY_SKEY = "provider.dashscope_api_key"
# 加密用的 Fernet key:优先 .env SETTINGS_ENC_KEY;否则用首次生成的本地文件密钥(data/.enc_key)
_enc_key: bytes | None = None


def _fernet_key() -> bytes:
    global _enc_key
    if _enc_key:
        return _enc_key
    from cryptography.fernet import Fernet
    from utils.path_tool import get_abs_path
    import base64
    from_env = os.getenv("SETTINGS_ENC_KEY", "").strip()
    if from_env:
        _enc_key = from_env.encode() if len(from_env) == 44 else base64.urlsafe_b64encode(hashlib.sha256(from_env.encode()).digest())
    else:
        key_path = get_abs_path("data\\.enc_key")
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                _enc_key = f.read().strip()
        else:
            _enc_key = Fernet.generate_key()
            with open(key_path, "wb") as f:
                f.write(_enc_key + b"\n")
    return _enc_key


def _encrypt(plain: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_fernet_key()).encrypt(plain.encode()).decode()


def _decrypt(token: str) -> str:
    from cryptography.fernet import Fernet
    return Fernet(_fernet_key()).decrypt(token.encode()).decode()


def set_dashscope_key(key: str | None) -> None:
    """写/清空 DashScope API Key(加密落库)。key 为空则删除该行。"""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            if key:
                cur.execute(
                    "INSERT INTO app_settings (skey, value_json, updated_at) "
                    "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE value_json = VALUES(value_json), "
                    "updated_at = VALUES(updated_at)",
                    (KEY_SKEY, json.dumps({"enc": _encrypt(key)}), _now()),
                )
            else:
                cur.execute("DELETE FROM app_settings WHERE skey = %s", (KEY_SKEY,))
    except Exception as e:
        logger.error(f"settings_store: 写 API Key 失败:{e}")
        raise


def get_dashscope_key(tenant: str | None = None) -> str:
    """取当前生效的 DashScope API Key:仅读 DB(前端「系统配置」填写,加密存储)。

    刻意**不回退 .env** —— 需求上 API key 完全由前端管理,避免环境变量里残留的
    key 与用户填的 key 串用(曾因此出现"填错仍能用"的假象)。
    预留 tenant:多租户时在此按租户取各自 key(现单租户返回全局)。
    """
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value_json FROM app_settings WHERE skey = %s", (KEY_SKEY,))
            row = cur.fetchone()
        if row and row["value_json"]:
            obj = json.loads(row["value_json"])
            if obj.get("enc"):
                return _decrypt(obj["enc"])
            return ""
    except Exception as e:
        logger.error(f"settings_store: 读取 DB API Key 失败:{e}")
    return ""


def api_key_configured() -> bool:
    """是否已配置可用 key(仅 DB;无 .env 兜底)。供前端聊天门槛 / 建模型用。"""
    return bool((get_dashscope_key() or "").strip())


def has_db_key() -> bool:
    """是否在 DB 里自设过 API Key(而非仅 .env 兜底)。供前端区分「清除/来源」展示。"""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value_json FROM app_settings WHERE skey = %s", (KEY_SKEY,))
            row = cur.fetchone()
        return bool(row and row["value_json"])
    except Exception:
        return False


if __name__ == "__main__":
    ensure_defaults()
    print("settings_store: 默认设置已种子化")
    print(all_settings())
    print("api_key_configured:", api_key_configured())
