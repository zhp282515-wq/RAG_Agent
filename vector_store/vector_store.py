from langchain_text_splitters import RecursiveCharacterTextSplitter
from utils.config_tool import vector_store_conf
from pymilvus import MilvusClient, DataType

from utils.logger_tool import logger
from utils.logger_tool import fmt_duration
from utils.file_tool import (
    get_txt_document,
    get_markdown_document,
    get_pdf_document,
    get_docs_document,
    get_csv_document,
    get_excel_document,
)
from model.modelfactory import emb_model, rerank_model
import hashlib
import os
import time
from rich import print as rprint


# 文件扩展名 -> file_tool 解析函数 的映射
_PARSER_BY_EXT = {
    "txt": get_txt_document,
    "md": get_markdown_document,
    "markdown": get_markdown_document,
    "pdf": get_pdf_document,
    "docx": get_docs_document,
    "doc": get_docs_document,
    "csv": get_csv_document,
    "xlsx": get_excel_document,
    "xls": get_excel_document,
}


# Milvus collection 标量字段名(与建 schema / 各方法共用)
_PK_FIELD = "pk"
_TEXT_FIELD = "text"
_VEC_FIELD = "vector"
_FILE_PATH_FIELD = "file_path"
_FILE_MD5_FIELD = "file_md5"
_FILE_NAME_FIELD = "file_name"
_FILE_TYPE_FIELD = "file_type"
_FILE_SIZE_FIELD = "file_size"

_COLLECTION_NAME = vector_store_conf.get("collection_name", "document_chunks")
_VEC_DIM = vector_store_conf.get("dim", 1024)
_MILVUS_URI = vector_store_conf.get("milvus_uri", "http://127.0.0.1:19530")


def _file_md5(file_path: str) -> str:
    """计算文件内容的 md5 十六进制串;文件不存在或读取失败返回空串(调用方按未取到处理)。"""
    try:
        h = hashlib.md5()
        with open(file_path, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest()
    except Exception as e:
        logger.error(f"_file_md5: 计算文件 {file_path} 的 md5 失败: {e}")
        return ""


def _milvus_str(value: str) -> str:
    """把字符串值转成可安全嵌入 Milvus filter 表达式的字面量。

    Windows 路径含反斜杠,直接拼进 filter 会破坏表达式解析,这里统一转义。
    """
    return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\""


class VectorStoreService:

    def __init__(self, chunk_size: int = vector_store_conf["chunk_size"], chunk_overlap: int = vector_store_conf["chunk_overlap"], separators: list = vector_store_conf["separators"]):
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=separators,
            length_function=len,
        )
        self._client: MilvusClient | None = None
        logger.debug(f"VectorStoreService: 初始化完成 chunk_size={chunk_size} chunk_overlap={chunk_overlap}")

    # ---------- Milvus 连接与建库基建 ----------

    def _get_client(self) -> MilvusClient:
        """惰性建立并返回 MilvusClient;首次调用时若 collection 不存在则创建。"""
        if self._client is None:
            logger.info(f"_get_client: 连接 Milvus {_MILVUS_URI}")
            self._client = MilvusClient(uri=_MILVUS_URI)
        self._ensure_collection()
        return self._client

    def _ensure_collection(self) -> None:
        """若 collection 不存在则建表 + 建向量索引,并 load;已存在则直接 load。"""
        if self._client is None:
            return
        if self._client.has_collection(_COLLECTION_NAME):
            state = self._client.get_load_state(_COLLECTION_NAME)
            if state and state.get("state") != "Loaded":
                logger.info(f"_ensure_collection: collection {_COLLECTION_NAME} 未加载,正在 load")
                self._client.load_collection(_COLLECTION_NAME)
            else:
                logger.debug(f"_ensure_collection: collection {_COLLECTION_NAME} 已就绪")
            return

        schema = self._client.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field(field_name=_PK_FIELD, datatype=DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field(field_name=_FILE_PATH_FIELD, datatype=DataType.VARCHAR, max_length=512)
        schema.add_field(field_name=_FILE_MD5_FIELD, datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name=_FILE_NAME_FIELD, datatype=DataType.VARCHAR, max_length=255)
        schema.add_field(field_name=_FILE_TYPE_FIELD, datatype=DataType.VARCHAR, max_length=16)
        schema.add_field(field_name=_FILE_SIZE_FIELD, datatype=DataType.INT64)
        schema.add_field(field_name=_TEXT_FIELD, datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name=_VEC_FIELD, datatype=DataType.FLOAT_VECTOR, dim=_VEC_DIM)

        index_params = self._client.prepare_index_params()
        index_params.add_index(field_name=_VEC_FIELD, index_type="HNSW", metric_type="COSINE",
                               params={"M": 16, "efConstruction": 200})
        index_params.add_index(field_name=_FILE_MD5_FIELD, index_type="INVERTED")
        index_params.add_index(field_name=_FILE_PATH_FIELD, index_type="INVERTED")

        self._client.create_collection(
            collection_name=_COLLECTION_NAME,
            schema=schema,
            index_params=index_params,
        )
        self._client.load_collection(_COLLECTION_NAME)
        logger.info(f"_ensure_collection: 已创建并加载 collection {_COLLECTION_NAME} (dim={_VEC_DIM})")

    def _query_file_md5s(self, file_path: str) -> set[str]:
        """从 collection 查 file_path 对应块的 file_md5 值集合(去重后)。"""
        rows = self._client.query(
            collection_name=_COLLECTION_NAME,
            filter=f"{_FILE_PATH_FIELD} == {_milvus_str(file_path)}",
            output_fields=[_FILE_MD5_FIELD],
        )
        return {r[_FILE_MD5_FIELD] for r in rows if r.get(_FILE_MD5_FIELD)}

    # ---------- md5 去重:md5 作为 collection 字段,随向量块增删 ----------

    def get_md5_hex(self, file_path: str) -> str:
        """计算并返回 file_path 当前内容的 md5 十六进制串(不对库做判断)。"""
        return _file_md5(file_path)

    def check_md5_hex(self, file_path: str) -> bool:
        """判断 file_path 是否已入库:库中是否已有与该文件 md5 相同的块。"""
        md5 = self._file_md5_of_file(file_path)
        if not md5:
            logger.warning(f"check_md5_hex: 无法计算 {file_path} 的 md5,按未入库处理")
            return False
        exists = self._collection_has_md5(md5)
        logger.debug(f"check_md5_hex: {file_path} md5={md5} 入库状态={exists}")
        return exists

    def _file_md5_of_file(self, file_path: str) -> str:
        return _file_md5(file_path)

    def _collection_has_md5(self, md5: str) -> bool:
        count = self._client.query(
            collection_name=_COLLECTION_NAME,
            filter=f"{_FILE_MD5_FIELD} == \"{md5}\"",
            output_fields=["count(*)"],
        )
        return bool(count and count[0].get("count(*)"))

    def save_md5_hex(self, file_path: str) -> bool:
        """文件内容已成功写入 collection 后调用,记录其 md5 去重状态。

        md5 作为 file_md5 字段随向量块一起持久化在 Milvus,这里校验该文件
        确实已有块落库,若没有则提示(真正落库在 load_document 完成)。
        """
        md5 = _file_md5(file_path)
        if not md5:
            logger.error(f"save_md5_hex: 计算 {file_path} 的 md5 失败,无法记录")
            return False
        if not self._collection_has_md5(md5):
            logger.warning(f"save_md5_hex: {file_path} (md5={md5}) 尚未在向量库中找到对应块,请先调用 load_document")
            return False
        logger.debug(f"save_md5_hex: 确认 {file_path} md5={md5} 已随向量块入库")
        return True

    def del_md5_hex(self, md5_hex: str) -> bool:
        """删除某 md5 对应的全部向量块(入参是文件的 md5 值)。

        md5 作为 file_md5 字段存于 Milvus,删除这些块的瞬间其 md5 一并清除。
        """
        if self._client is None:
            logger.error("del_md5_hex: Milvus 客户端未初始化,无法删除")
            return False
        res = self._client.delete(
            collection_name=_COLLECTION_NAME,
            filter=f"{_FILE_MD5_FIELD} == \"{md5_hex}\"",
        )
        deleted = res.get("delete_count", 0)
        if deleted:
            self._client.flush(_COLLECTION_NAME)
            logger.info(f"del_md5_hex: 已删除 md5={md5_hex} 的 {deleted} 个向量块及其 md5 记录")
        else:
            logger.debug(f"del_md5_hex: md5={md5_hex} 在向量库中无块,无需删除")
        return True

    def _parse_file(self, file_path: str):
        """按扩展名选 file_tool 解析函数处理文档,返回 list[Document]|bool。"""
        ext = os.path.splitext(file_path)[1].lstrip(".").lower()
        parser = _PARSER_BY_EXT.get(ext)
        if parser is None:
            logger.error(f"_parse_file: 不支持的文档格式 .{ext}: {file_path}")
            return False
        return parser(file_path)

    def _split_to_chunks(self, docs) -> list | bool:
        """把解析出的 Document 列表分片,返回 chunk(Document)列表。

        返回 chunk 而非纯文本,是为了让 load_document 取到每个 chunk 的
        metadata(如 PDF 的 page/chapter/section)随块落库溯源。
        """
        if not isinstance(docs, list) or not docs:
            logger.warning("_split_to_chunks: 文档解析结果为空,无可分片内容")
            return False
        chunks = self.splitter.split_documents(docs)
        chunks = [c for c in chunks if c.page_content and c.page_content.strip()]
        if not chunks:
            logger.warning("_split_to_chunks: 分片后无有效文本")
            return False
        return chunks

    def load_document(self, file_path: str) -> bool:
        """把单个文档入库:按类型解析 -> 分片 -> embedding 向量化 -> 写入向量库。

        若该文件内容已入库(md5 命中)则跳过,避免重复。
        PDF chunk 的 page/chapter/section 溯源元数据随块落库。
        """
        self._get_client()
        if not os.path.isfile(file_path):
            logger.error(f"load_document: 文件不存在: {file_path}")
            return False

        md5 = self._file_md5_of_file(file_path)
        if not md5:
            logger.error(f"load_document: 计算 {file_path} 的 md5 失败,入库中止")
            return False
        if self._collection_has_md5(md5):
            logger.info(f"load_document: {file_path} 已入库(md5={md5[:8]}...),跳过")
            return True

        docs = self._parse_file(file_path)
        if not isinstance(docs, list):
            logger.error(f"load_document: 解析文档失败: {file_path}")
            return False

        chunks = self._split_to_chunks(docs)
        if not isinstance(chunks, list):
            logger.error(f"load_document: 分片失败: {file_path}")
            return False

        texts = [c.page_content for c in chunks]
        logger.info(f"load_document: {file_path} 解析出 {len(texts)} 个分片,开始向量化")
        try:
            vectors = emb_model.embed_documents(texts)
        except Exception as e:
            logger.error(f"load_document: 向量化失败: {file_path}: {e}")
            return False
        logger.debug(f"load_document: 向量化完成,共 {len(vectors)} 条向量")

        fname = os.path.basename(file_path)
        ext = os.path.splitext(fname)[1].lstrip(".").lower()
        fsize = os.path.getsize(file_path)
        rows = []
        for chunk, vec in zip(chunks, vectors):
            meta = chunk.metadata or {}
            row = {
                _FILE_PATH_FIELD: file_path,
                _FILE_MD5_FIELD: md5,
                _FILE_NAME_FIELD: fname,
                _FILE_TYPE_FIELD: ext or "unknown",
                _FILE_SIZE_FIELD: fsize,
                _TEXT_FIELD: chunk.page_content,
                _VEC_FIELD: vec,
            }
            # 溯源元数据:有值才写入(动态字段),非 PDF 格式无这些字段则不影响
            if meta.get("page") is not None:
                row["page"] = int(meta["page"])
            for key in ("chapter", "section", "subsection", "content_type"):
                if meta.get(key):
                    row[key] = str(meta[key])
            rows.append(row)

        try:
            self._client.insert(_COLLECTION_NAME, rows)
            self._client.flush(_COLLECTION_NAME)
        except Exception as e:
            logger.error(f"load_document: 写入向量库失败: {fname}: {e}")
            return False
        logger.info(f"load_document: {fname} 已入库,{len(rows)} 个分片(md5={md5[:8]}...)")
        return True

    def preview_text(self, file_path: str, max_chars: int = 3000) -> str:
        """解析文档并返回开头文本(供前端预览弹窗)。

        直接复用 file_tool 解析器(与入库同一套);PDF/表格等格式解析后取全文前段。
        文件不存在或解析失败返回空串。
        """
        if not os.path.isfile(file_path):
            logger.warning(f"preview_text: 文件不存在: {file_path}")
            return ""
        docs = self._parse_file(file_path)
        if not isinstance(docs, list) or not docs:
            logger.warning(f"preview_text: 文档解析失败或为空: {file_path}")
            return ""
        parts: list[str] = []
        for d in docs:
            text = (getattr(d, "page_content", "") or "").strip()
            if text:
                parts.append(text)
            if sum(len(p) for p in parts) >= max_chars:
                break
        return "\n".join(parts)[:max_chars]

    def list_documents(self) -> list[dict]:
        """列出向量库中已入库的文档:文件名、格式、原始字节大小、分片数量、存储路径。

        file_size 返回原始字节数(由前端换算 B/KB/MB/GB/TB 展示);
        file_path 供预览接口定位原始文件。
        """
        client = self._get_client()
        rows = client.query(
            collection_name=_COLLECTION_NAME,
            filter="pk >= 0",
            output_fields=[
                _FILE_PATH_FIELD, _FILE_MD5_FIELD, _FILE_NAME_FIELD,
                _FILE_TYPE_FIELD, _FILE_SIZE_FIELD,
            ],
        )
        meta: dict[str, dict] = {}
        for r in rows:
            name = r.get(_FILE_NAME_FIELD) or os.path.basename(r.get(_FILE_PATH_FIELD, ""))
            key = r.get(_FILE_MD5_FIELD) or name
            item = meta.setdefault(key, {
                "file_name": name,
                "file_type": r.get(_FILE_TYPE_FIELD, ""),
                "file_size": int(r.get(_FILE_SIZE_FIELD, 0) or 0),
                "file_path": r.get(_FILE_PATH_FIELD, ""),
                "chunk_count": 0,
            })
            item["chunk_count"] += 1
        logger.info(f"list_documents: 查询完成,共 {len(meta)} 个文档、{len(rows)} 个分片")
        return list(meta.values())

    def list_chunks(self, file_name: str) -> list[dict]:
        """列出某文档在向量库中的全部分片(按 pk 排序),供前端分片预览。"""
        client = self._get_client()
        rows = client.query(
            collection_name=_COLLECTION_NAME,
            filter=f"{_FILE_NAME_FIELD} == {_milvus_str(file_name)}",
            output_fields=["pk", _TEXT_FIELD, "page", "chapter", "section"],
            limit=2000,
        )
        rows.sort(key=lambda r: r.get("pk", 0))
        out = []
        for r in rows:
            item = {"text": r.get(_TEXT_FIELD, "")}
            if r.get("page") is not None:
                item["page"] = r["page"]
            for k in ("chapter", "section"):
                if r.get(k):
                    item[k] = r[k]
            out.append(item)
        logger.info(f"list_chunks: {file_name} 共 {len(out)} 个分片")
        return out

    def del_document(self, file_name: str) -> bool:
        """删除向量库中的文档:入参为库中已存在的文档名(list_documents 的 file_name)。

        按 file_name 查出该文档对应的全部 md5,逐个 del_md5_hex 清除其向量块;
        md5 作为 file_md5 字段存于 Milvus,删除这些块的瞬间其 md5 记录一并清除。
        """
        client = self._get_client()
        rows = client.query(
            collection_name=_COLLECTION_NAME,
            filter=f"{_FILE_NAME_FIELD} == {_milvus_str(file_name)}",
            output_fields=[_FILE_MD5_FIELD],
        )
        md5s = {r[_FILE_MD5_FIELD] for r in rows if r.get(_FILE_MD5_FIELD)}
        if not md5s:
            logger.warning(f"del_document: 向量库中不存在文档「{file_name}」,无需删除")
            return False
        for md5 in md5s:
            self.del_md5_hex(md5)
        logger.info(f"del_document: 已从向量库清除文档「{file_name}」({len(md5s)} 个内容版本)")
        return True

    def search(self, query: str, top_k: int = 20, score_threshold: float = 0.0,
               file_names: list[str] | None = None) -> list[dict]:
        """向量检索:query 向量化 -> Milvus COSINE 相似度召回 top_k 个分片。

        Returns:
            [{pk, text, score, file_name, file_type}], 按 score 降序。
            score 为 COSINE 相似度(0~1)。file_names 非空时限定在这些文档内检索。
        """
        client = self._get_client()
        logger.debug(f"search: query={query[:50]!r} top_k={top_k} file_names={file_names}")
        try:
            qvec = emb_model.embed_query(query)
        except Exception as e:
            logger.error(f"search: 查询向量化失败: {e}")
            return []
        req = dict(
            collection_name=_COLLECTION_NAME,
            data=[qvec],
            anns_field=_VEC_FIELD,
            limit=top_k,
            output_fields=[
                _TEXT_FIELD, _FILE_NAME_FIELD, _FILE_TYPE_FIELD,
                "page", "chapter", "section",
            ],
        )
        # 文档级过滤:Milvus 支持 IN 表达式,实现"只在指定文档内搜"
        if file_names:
            names = ", ".join(_milvus_str(n) for n in file_names)
            req["filter"] = f"{_FILE_NAME_FIELD} in [{names}]"
        try:
            res = client.search(**req)[0]
        except Exception as e:
            logger.error(f"search: Milvus 检索失败: {e}")
            return []
        hits = [
            {
                "pk": hit["id"],
                "text": hit["entity"].get(_TEXT_FIELD, ""),
                "score": hit["distance"],
                "file_name": hit["entity"].get(_FILE_NAME_FIELD, ""),
                "file_type": hit["entity"].get(_FILE_TYPE_FIELD, ""),
                "page": hit["entity"].get("page"),
                "chapter": hit["entity"].get("chapter", ""),
                "section": hit["entity"].get("section", ""),
            }
            for hit in res
            if hit["distance"] >= score_threshold
        ]
        logger.debug(f"search: 命中 {len(hits)} 条(top1 score={hits[0]['score']:.3f})" if hits else "search: 未命中任何分片")
        return hits

    def get_base_retriever(self, top_k: int | None = None):
        """返回纯向量检索器:top_k 默认取 vector_store.yml 配置,调用时只传 query。

        独立成方法便于评估时与 get_rerank_retriever 对照(是否加精排的增益)。
        """
        top_k = top_k or vector_store_conf.get("top_k", 20)
        def retrieve(query: str, file_names: list[str] | None = None) -> list[dict]:
            return self.search(query, top_k=top_k, file_names=file_names)
        return retrieve

    def get_rerank_retriever(self, top_k: int | None = None, rerank_n: int | None = None,
                             on_step=None):
        """返回 向量粗排 + rerank 精排 的检索器:top_k/rerank_n 默认取 vector_store.yml 配置,调用时只传 query。

        on_step: 可选回调 `(label: str, info: dict)` 在检索内部子步骤(向量召回/精排/过滤)发生时调用,
                供上层把工作流细化到每一步(web 工作流面板展示)。
        """
        top_k = top_k or vector_store_conf.get("top_k", 20)
        rerank_n = rerank_n or vector_store_conf.get("rerank_n", 5)
        def retrieve(query: str, file_names: list[str] | None = None) -> list[dict]:
            t0 = time.perf_counter()
            logger.debug(f"get_rerank_retriever: query={query[:50]!r} top_k={top_k} rerank_n={rerank_n}")
            # ① 向量召回
            if on_step:
                on_step("向量召回", {"阶段": f"召回 {top_k} 个候选分片"})
            hits = self.search(query, top_k=top_k, file_names=file_names)
            if not hits:
                logger.warning(f"get_rerank_retriever: 向量检索无命中,query={query[:50]!r}")
                if on_step:
                    on_step("精排", {"阶段": "无候选,跳过 rerank"})
                return []
            # ② rerank 精排
            try:
                if on_step:
                    on_step("精排", {"阶段": f"对 {len(hits)} 个候选做 rerank 精排", "候选数": len(hits)})
                reranked = rerank_model.rerank(query, [h["text"] for h in hits], top_n=min(rerank_n, len(hits)))
            except Exception as e:
                logger.error(f"get_rerank_retriever: 精排失败,退回向量检索结果: {e}")
                return hits[:rerank_n]
            ordered = [hits[i] for i, _ in reranked]
            for h, (_, score) in zip(ordered, reranked):
                h["score"] = score   # 用精排相关度分数替换向量相似度

            logger.debug(f"get_rerank_retriever: 精排完成,返回 {len(ordered)} 条,耗时 {fmt_duration(time.perf_counter()-t0)}")
            return ordered   # 列表顺序即精排名次
        return retrieve


if __name__ == '__main__':
    svc = VectorStoreService()
    # svc.load_document("C:\\Users\\Administrator\\Desktop\\Python-Project\\AI大模型RAG与智能体开发_Agent项目(2刷)\\data\\扫地机器人120问.md")
    # svc.load_document("C:\\Users\\Administrator\\Desktop\\Python-Project\\AI大模型RAG与智能体开发_Agent项目\\data\\故障排除.txt")
    # svc.load_document("C:\\Users\\Administrator\\Desktop\\Python-Project\\AI大模型RAG与智能体开发_Agent项目\\data\\扫地机器人100问.pdf")
    # svc.load_document("C:\\Users\\Administrator\\Desktop\\Python-Project\\AI大模型RAG与智能体开发_Agent项目\\data\\扫拖一体机器人100问.txt")
    # svc.load_document("C:\\Users\\Administrator\\Desktop\\Python-Project\\AI大模型RAG与智能体开发_Agent项目\\data\\维护保养.txt")
    # svc.load_document("C:\\Users\\Administrator\\Desktop\\Python-Project\\AI大模型RAG与智能体开发_Agent项目\\data\\选购指南.txt")
    # svc.load_document("D:\\WechatDocuments\\xwechat_files\\wxid_8pvqntf7ufpt12_6ce1\\msg\\file\\2026-07\\6、python环境安装及使用(1).pdf")
    # svc.load_document("D:\\WechatDocuments\\xwechat_files\\wxid_8pvqntf7ufpt12_6ce1\\msg\\file\\2026-09\\2026年软考工作安排的通知.pdf")

    # rprint(svc.list_documents())

    # svc.del_document("6、python环境安装及使用(1).pdf")   # 参数是向量库中的文档名,不是本地路径
    #
    res = svc.get_rerank_retriever()("不同季节机器人保养方案")
    for r in res:
        print(r)
