from abc import ABC, abstractmethod
from langchain.chat_models import init_chat_model

from dotenv import load_dotenv
from openai import OpenAI
from typing import List

load_dotenv(override=True)
from langchain.chat_models.base import _ConfigurableModel
from utils.config_tool import model_conf

import os
import json
import urllib.request

# 当前生效的 DashScope API Key:DB「系统配置」填的 key 优先,否则回退 .env。
# 每个服务在真正要发请求时才读(而非 import 时读死),使改 key「下次提问即生效」。
def dashscope_api_key() -> str:
    from utils.settings_store import get_dashscope_key
    return get_dashscope_key()


def _report_usage(kind: str, resp) -> None:
    """把 embedding/rerank 的 token 消耗上报给中间的用量累加器。

    resp 是 openai 响应对象(有 .usage)或 DashScope 原生返回的 dict(含 usage)。
    仅当当前处于工具调用期间才真正累加(见 agent_middleware.usage_add),
    因此文档入库、调试检索等场景自动是空操作。
    这里延迟 import:modelfactory 被 middleware 间接引用,顶层 import 会成环。
    """
    try:
        from ReAct.middleware.agent_middleware import usage_add
        if isinstance(resp, dict):
            usage = resp.get("usage") or {}
        else:
            usage = getattr(resp, "usage", None)
            if usage is None:
                return
            usage = usage if isinstance(usage, dict) else usage.model_dump()
        tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        if tokens:
            usage_add(kind, tokens)
    except Exception:
        pass

class BaseModelService(ABC):
    """模型服务的基类"""
    @abstractmethod
    def get_model_service(self):
        pass

class ChatModelService(BaseModelService):
    """聊天模型服务"""
    def get_model_service(self, model=model_conf["model"],
                          api_key: str | None = None,
                          base_url: str | None = None,
                          temperature: float | None = None) -> _ConfigurableModel:
        # key 未显式传入 → 取当前生效 key(DB 优先,回退 .env)
        if api_key is None:
            api_key = dashscope_api_key()
        if base_url is None:
            base_url = os.getenv("DASHSCOPE_BASE_URL")
        kw = {"model": model or None, "api_key": api_key or None, "base_url": base_url or None}
        if temperature is not None:
            kw["temperature"] = temperature
        return init_chat_model(**kw)


def get_chat_model(model: str, temperature: float | None = None) -> _ConfigurableModel:
    """按模型名建聊天模型(web 前端可在 qwen3.x 系列间切换)。

    模型名可能是裸名(qwen3.8-flash)或带 provider(openai:qwen3.8-flash)。
    DashScope 走 OpenAI 兼容端点,裸名无法被 init_chat_model 推断 provider,
    这里统一补 openai: 前缀,确保真正切到目标模型而非静默回退默认。
    temperature: 可选,透传给 init_chat_model(系统配置页可改)。
    """
    name = str(model or "").strip()
    if not name:
        raise ValueError("model 不能为空")
    if ":" not in name:
        name = f"openai:{name}"
    return ChatModelService().get_model_service(model=name, temperature=temperature)

class EmbeddingModelService(BaseModelService):
    """嵌入模型服务"""
    def get_model_service(self) -> OpenAI:
        return self._client()

    def __init__(self, model: str = model_conf["emb_model"], dimensions: int = 1024):
        self._model = model
        self._dimensions = dimensions
        self._client_cache: tuple[str, OpenAI] | None = None  # (key, client)

    def _client(self) -> OpenAI:
        """按当前生效 key 取客户端(key 变化时重建,使改 key 后无需重启)。"""
        key = dashscope_api_key()
        if self._client_cache is None or self._client_cache[0] != key:
            self._client_cache = (key, OpenAI(
                api_key=key or "",
                base_url=os.getenv("DASHSCOPE_BASE_URL"),
            ))
        return self._client_cache[1]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """为文档列表生成向量;按 ≤2 条分批,保持与输入同序。

        实测该模型单请求条数上限为 2,超出报 400(batch size invalid)。
        """
        vectors: List[List[float]] = []
        for i in range(0, len(texts), 2):
            batch = texts[i:i + 2]
            response = self._client().embeddings.create(
                model=self._model,
                input=batch,
                dimensions=self._dimensions,
            )
            _report_usage("embedding", response)
            vectors.extend(item.embedding for item in response.data)
        return vectors

    def embed_query(self, text: str) -> List[float]:
        """为查询文本生成向量"""
        response = self._client().embeddings.create(
            model=self._model,
            input=text,
            dimensions=self._dimensions,
        )
        _report_usage("embedding", response)
        return response.data[0].embedding

class RerankModelService(BaseModelService):
    """重排序模型服务。

    OpenAI 兼容模式没有 rerank 端点,走 DashScope 原生 API。
    实测请求体须把 query/documents 包在 input 下,返回 results 数组,
    每项含 index(原文档下标)与 relevance_score(0~1,降序)。
    """
    _RERANK_URL = os.getenv("RERANK_BASE_URL")

    def __init__(self, model: str = model_conf.get("rerank_model", "qwen3.7-text-rerank")):
        self.model = model

    def get_model_service(self):
        return self

    def rerank(self, query: str, documents: List[str], top_n: int = 5) -> List[tuple[int, float]]:
        """对 documents 按与 query 的相关度精排,返回 [(原文档下标, 相关度分数)] 降序。"""
        body = json.dumps({
            "model": self.model,
            "input": {"query": query, "documents": documents},
            "parameters": {"top_n": top_n, "return_documents": False},
        }).encode("utf-8")
        req = urllib.request.Request(self._RERANK_URL, data=body, headers={
            "Authorization": f"Bearer {dashscope_api_key()}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        _report_usage("rerank", data)
        results = data["output"]["results"]
        return [(r["index"], r["relevance_score"]) for r in results]

# 模块级默认聊天模型改为「惰性获取」:不在 import 时构建(否则无 API key 时
# 一 import 就抛 Missing credentials,服务器连启动都不行)。真正要建默认模型时
# 才读当前 key;无 key 由调用方/门槛统一提示。
def get_default_chat_model(temperature: float | None = None):
    """按 model.yml 默认模型名构建聊天模型(读当前生效 key)。无 key 时抛错由上层处理。"""
    name = (model_conf.get("model") or "").split(":", 1)[-1] or ""
    return get_chat_model(name or "qwen3.8-flash", temperature=temperature)


def get_summary_chat_model():
    """构建上下文压缩(摘要)专用模型:读 model.yml 的 summary_model。

    与对话模型分开:摘要不需要同档能力,用小模型更快更省。
    temperature=0 让摘要稳定可复现;max_tokens 封顶限制单次压缩成本。
    注意 qwen3.7-flash 是思维链模型,该上限把推理 token 也算在内,故不可设得过小。
    """
    name = (model_conf.get("summary_model") or "").split(":", 1)[-1] or ""
    max_tokens = int(model_conf.get("summary_max_tokens", 4000) or 4000)
    m = get_chat_model(name or "qwen3.7-flash", temperature=0)
    return m.bind(max_tokens=max_tokens)


emb_model = EmbeddingModelService()
rerank_model = RerankModelService()


if __name__ == '__main__':
    # res = chat_model.invoke("你好")
    # print(res.content)
    # print(model_conf["model"])
    # res = emb_model.embed_query("你好")
    res = emb_model.embed_documents(["你好","我好","他也好"])
    for r in res:
        print(len(r),end=" ")
        print(r)

    # print(res)