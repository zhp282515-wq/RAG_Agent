"""视觉大模型工具：把单张图片转成文字描述（DashScope 通义千问-VL）。

供 file_tool 等模块调用：提取出文档里的图片字节后，调 describe_image
生成一段可被文本检索命中的图片描述。

要求：无 API key 时不中断（返回占位文本并打 warning）；
同一图片内容只调用一次 VLM（内容级 sha256 缓存）。
"""

import base64
import hashlib
import os
import time

from utils.logger_tool import logger
from utils.config_tool import model_conf



from dotenv import load_dotenv
load_dotenv(override=True)


# 调用参数可经环境变量覆盖
_VLM_MODEL = model_conf["vlm_model"]
_VLM_MAX_TOKENS = int(model_conf["vlm_max_tokens"])
_VLM_RETRIES = int(model_conf["vlm_retries"])

_DEFAULT_PROMPT = (
    "请用中文详细描述这张图片中的所有可见内容：包括图中出现的全部文字、"
    "数据、表格、流程步骤、图表结构与各部分之间的关系。"
    "描述要尽可能完整具体，便于后续通过文字检索到这张图片的信息。"
)

# 图片字节 sha256 -> 描述 的内存缓存
_cache: dict[str, str] = {}


def describe_image(
    img_bytes: bytes,
    mime: str = "png",
    prompt: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> str:
    """把单张图片转成文字描述。

    Args:
        img_bytes: 图片原始字节
        mime: 图片格式（png / jpeg 等），用于构造 data URL
        prompt: 可选的自定义描述要求
        api_key: 显式传入则用之;缺省取「系统配置」DB key,回退 .env

    Returns:
        图片的文字描述；无 API key 或调用失败时返回占位文本，不抛异常。
    """
    if not img_bytes:
        logger.warning("describe_image: 收到空图片字节，跳过")
        return "[图片：空内容，未生成描述]"

    # 内容级缓存：同一张图（如 docx 重复引用）只调用一次 VLM
    img_hash = hashlib.sha256(img_bytes).hexdigest()
    if img_hash in _cache:
        return _cache[img_hash]

    # 未显式传 key → 取当前生效 key(DB 优先,回退 .env)
    if not api_key:
        try:
            from utils.settings_store import get_dashscope_key
            api_key = get_dashscope_key()
        except Exception:
            api_key = ""
    if not api_key:
        logger.warning("describe_image: 缺少 DashScope API Key(系统配置未填),跳过图片描述")
        return "[图片：缺少API密钥，未生成描述]"

    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url=base_url or os.getenv("DASHSCOPE_BASE_URL"),
    )

    b64 = base64.b64encode(img_bytes).decode()
    data_url = f"data:image/{mime};base64,{b64}"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt or _DEFAULT_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]



    for attempt in range(1, _VLM_RETRIES + 1):
        try:
            logger.info(f"describe_image: 调用 VLM，图片 hash: {img_hash}")
            resp = client.chat.completions.create(
                model=_VLM_MODEL,
                messages=messages,
                max_tokens=_VLM_MAX_TOKENS,
            )
            desc = (resp.choices[0].message.content or "").strip()
            if not desc:
                raise ValueError("VLM 返回空内容")
            _cache[img_hash] = desc
            return desc
        except Exception as e:
            logger.error(f"describe_image: VLM 调用失败（第{attempt}/{_VLM_RETRIES}次）: {e}")
            if attempt == _VLM_RETRIES:
                return "[图片：描述失败]"
            time.sleep(1 << attempt)  # 指数退避

    return "[图片：描述失败]"
