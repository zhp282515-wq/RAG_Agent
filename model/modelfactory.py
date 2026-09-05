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

class BaseModelService(ABC):
    """模型服务的基类"""
    @abstractmethod
    def get_model_service(self):
        pass

class ChatModelService(BaseModelService):
    """聊天模型服务"""
    def get_model_service(self, model=model_conf["model"], api_key=os.getenv("DASHSCOPE_API_KEY"), base_url=os.getenv("DASHSCOPE_BASE_URL")) -> _ConfigurableModel:
        return init_chat_model(
            model=model or None,
            api_key=api_key or None,
            base_url=base_url or None,
        )


def get_chat_model(model: str) -> _ConfigurableModel:
    """按模型名建聊天模型(web 前端可在 qwen3.x 系列间切换)。

    模型名可能是裸名(qwen3.8-flash)或带 provider(openai:qwen3.8-flash)。
    DashScope 走 OpenAI 兼容端点,裸名无法被 init_chat_model 推断 provider,
    这里统一补 openai: 前缀,确保真正切到目标模型而非静默回退默认。
    """
    name = str(model or "").strip()
    if not name:
        raise ValueError("model 不能为空")
    if ":" not in name:
        name = f"openai:{name}"
    return ChatModelService().get_model_service(model=name)

class EmbeddingModelService(BaseModelService):
    """嵌入模型服务"""
    def get_model_service(self) -> OpenAI:
        return self.client

    def __init__(self, model: str = model_conf["emb_model"], dimensions: int = 1024):
        self.client = OpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url=os.getenv("DASHSCOPE_BASE_URL"),
        )
        self.model = model
        self.dimensions = dimensions

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """为文档列表生成向量;按 ≤2 条分批,保持与输入同序。

        实测该模型单请求条数上限为 2,超出报 400(batch size invalid)。
        """
        vectors: List[List[float]] = []
        for i in range(0, len(texts), 2):
            batch = texts[i:i + 2]
            response = self.client.embeddings.create(
                model=self.model,
                input=batch,
                dimensions=self.dimensions,
            )
            vectors.extend(item.embedding for item in response.data)
        return vectors

    def embed_query(self, text: str) -> List[float]:
        """为查询文本生成向量"""
        response = self.client.embeddings.create(
            model=self.model,
            input=text,
            dimensions=self.dimensions,
        )
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
            "Authorization": f"Bearer {os.getenv('DASHSCOPE_API_KEY')}",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        results = data["output"]["results"]
        return [(r["index"], r["relevance_score"]) for r in results]

chat_model = ChatModelService().get_model_service()
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