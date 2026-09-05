# RAG Agent · 多模态检索增强智能体全栈系统

基于 **LangGraph Agent + 大模型 + Milvus 向量检索**的对话式知识助手。内置扫地机器人产品知识库，可在多轮会话中自动决定检索时机、融合图片 / 文档 / 天气 / 用户使用数据等多源信息作答，并能一键生成周期使用报告。提供完整 Web 前端（会话流式问答、工作流可视化、知识库管理、检索调试、报告中心）。

> 核心亮点：真 Agent 编排（工具自动调用 + 中间件切提示词 + 会话记忆持久化）、向量粗排 + Rerank 精排的混合检索、PDF 布局感知解析 + VLM 图片描述、端到端可运行的全栈 Web。

---

## 一、核心特性

- **ReAct Agent 自动编排**：LangGraph `create_agent` 构建，模型自主决定何时调用检索 / 天气 / 用户定位 / 外部数据等 7 个工具，支持 `stream_mode="messages"` 真流式逐 token 输出。
- **混合检索（粗排 + 精排）**：向量召回 Top-20 → Rerank 精排取 Top-5，相关度按 高(≥0.85) / 中(0.60~0.85) 分级，<0.60 不进上下文。
- **多模态输入**：聊天可带图片（多模态消息直接理解）与附件文档（解析后随消息喂给模型，不入知识库）。
- **会话记忆 MySQL 持久化**：LangGraph 官方 `PyMySQLSaver` 作 checkpointer，多轮记忆按会话窗口存库，**服务重启可续聊**；另有独立的会话 / 消息 / 报告业务表。
- **PDF 深度解析**：布局感知解析（正文 / 表格 / 图片按阅读顺序合流），页内图片由 VLM 生成描述后并入文本，向量命中可溯源到 页码 / 章节 / 小节。
- **文件去重入库**：文档按 MD5 去重，重复上传自动跳过；支持 txt / md / pdf / docx / csv / xlsx。
- **自动化周期报告**：报告场景经中间件自动切换到报告提示词，聚合检索资料 + 用户使用数据（CSV）生成 Markdown 报告并落库、可编辑。
- **全程可视化**：前端实时展示工作流阶段（模型思考 / 工具调用 / 向量召回→精排→过滤 子步骤）与回答溯源来源。
- **可切换模型**：qwen3.8-flash 等 DashScope 模型在 Web 端一键切换（同一模型复用 Agent 实例，避免反复构建）。

## 二、技术栈

| 层 | 选型 |
| --- | --- |
| 智能体框架 | LangChain `create_agent` / LangGraph（含官方中间件扩展 + PyMySQLSaver checkpointer） |
| 大模型 | DashScope：`qwen3.8-flash`(对话) / `qwen3.7-text-embedding-flash`(向量) / `qwen3.7-text-rerank`(精排) / `qwen-vl-plus`(VLM 图片描述) |
| 向量数据库 | Milvus v2.4 + etcd + MinIO（Docker Compose 编排，HNSW + COSINE） |
| 后端 | Python 3.13 / FastAPI + SSE 流式 / PyMySQL / PyMuPDF / python-docx / openpyxl |
| 前端 | 原生 HTML/CSS/JS（四视图单页应用：会话 / 知识库 / 检索调试 / 报告） |
| 环境/工程 | uv / Docker Compose / Rich / 结构化 logging |

## 三、架构总览

```
┌────────────────────────────── Web (FastAPI) ──────────────────────────────┐
│  会话视图    知识库视图    检索调试     报告视图                              │
│   (SSE流式 + 工作流面板 + 溯源 + 图片/附件)                                 │
└───────────────┬────────────────────────────────────────────────────────────┘
                │ /api/chat  /api/documents  /api/search  /api/report /api/sessions
┌───────────────▼────────────────────────────────────────────────────────────┐
│  ReAct Agent 服务层                                                        │
│   中间件链路(监控/日志/动态提示词切换) → 7 个工具 → create_agent 主循环        │
│   检索工具: get_rerank_retriever(粗排→精排→阈值过滤)                        │
│   会话窗口: thread_id ↔ session_id(业务表 + langgraph checkpointer 双层)   │
└───────┬───────────────────────────────┬────────────────────────────────────┘
        │ 向量化/检索                     │ 图片/文档解析
┌───────▼─────────────┐          ┌───────▼───────────────────────────────────┐
│  Milvus document_chunks │       │ 文件解析层                                 │
│  (HNSW 向量 + 标量索引,   │      │  PDF布局感知(含VLM图片描述) / docx / xlsx /… │
│   text/pk/md5/来源溯源)   │      │  Embedding / Rerank / VLM(图片)           │
└──────────┬────────────┘          └──────────────────────────────────────────┘
           │
┌──────────▼─────────────── MySQL(agent_sessions 库) ────────────────────────┐
│  agent_sessions / agent_session_messages / reports(业务)                   │
│  checkpoints 系列 6 张表(langgraph 官方 saver 多轮记忆)                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

（数据流向：上传/入库 → 解析分片 → 向量化写 Milvus；提问 → 工具检索召回 → 精排 → 阈值过滤 → 注入上下文 → 模型生成 → 流式回传并落库会话。）

## 四、快速启动

### 1. 前置

- Python 3.13+、uv；Docker（跑 Milvus）。
- 注册 [DashScope](https://dashscope.console.aliyun.com/) 获取 API Key，并开通 `qwen3.8-flash`、`qwen3.7-text-embedding-flash`、`qwen3.7-text-rerank`、`qwen-vl-plus` 模型。

### 2. 环境变量

复制本地 `.env` 模板并填写（键名参考）：

```
DASHSCOPE_API_KEY=sk-xxx
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
RERANK_BASE_URL=https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=xxx
MYSQL_DATABASE=agent_sessions
```

> `.env` 不入库不提交，请自行维护；`TAVILY / QWEATHER / LANGSMITH` 等为可选预留。

### 3. 启动基础服务（Milvus + MySQL）

```bash
# Milvus: etcd + MinIO + Milvus standalone
docker compose up -d

# MySQL(任选其一)
# 本机已装:直接建库即可(首次启动 Web 服务也会自动建库建表,幂等)
mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS agent_sessions DEFAULT CHARACTER SET utf8mb4;"
# 或用 Docker 起一个
docker run -d --name rag-mysql -e MYSQL_ROOT_PASSWORD=xxx -p 3306:3306 mysql:8
```

### 4. 安装依赖并启动 Web

```bash
uv sync                          # 按 pyproject.toml / uv.lock 安装
uv run python web/server.py      # 启动 FastAPI(默认 127.0.0.1:8001)
```

浏览器打开 `http://127.0.0.1:8001` 即可进入「会话」页提问，首次提问会自动初始化数据库。

### 5.（可选）导入知识库文档

在「知识库」页上传 txt / md / pdf / docx / csv / xlsx，自动 解析→分片→向量化→入库（MD5 去重）；也可脚本导入：

```python
from vector_store.vector_store import VectorStoreService
VectorStoreService().load_document("D:/docs/扫地机器人100问.pdf")
```

## 五、界面视图

- **会话**：多轮流式问答；可上传图片 / 附件文档；右侧实时展示「工作流」阶段与回答「溯源来源」。
- **知识库**：已入库文档列表（分片数 / 大小 / 格式），文档预览与分片预览，删除。
- **检索调试**：输入检索词，直接对比向量检索与 Rerank 精排后的 Top-N 命中及相关度。
- **报告**：输入用户 ID 与月份生成使用报告（流式），报告自动保存、可二次编辑。

## 六、目录结构

```
RAG_Agent/
├── ReAct/
│   ├── ReAct_Agent/agent.py        # ReActAgentService: 会话管理 + 流式/图文问答
│   ├── middleware/agent_middleware.py # 工具监控、阶段化 trace、报告提示词动态切换
│   └── tools/agent_tools.py        # 检索/天气/定位/外部数据 等 7 个工具
├── vector_store/vector_store.py    # Milvus 连接/建库、解析→分片→入库、粗排+精排检索
├── model/modelfactory.py           # 对话/向量/Rerank/VLM 模型工厂
├── utils/                          # MySQL、会话存储、文件解析、图片、VLM、日志、配置
│   └── eval_rerank.py              # ±Rerank 检索对照评测(115 问集,可复跑)
├── config/                         # agent / model / rag / vector_store 的 YAML 配置
├── prompts/                        # 系统提示词 & 报告提示词
├── web/
│   ├── server.py                   # FastAPI: 会话/上传/聊天SSE/报告/检索/文档/报告记录
│   ├── API_SPEC.md                 # 前后端接口契约
│   └── static/                     # 单页前端(css/js)
├── docker-compose.yml              # Milvus + etcd + MinIO
├── pyproject.toml / uv.lock        # 依赖
└── data/                           # uploads(上传) / external(外部用户数据 CSV) 等
```

## 七、CLI / 脚本演示

- `ReAct/ReAct_Agent/agent.py`：纯文本流式问答示例（复用会话窗口续聊）。
- `utils/session_store.py`：会话窗口 CRUD 自测。
- `utils/mysql_tool.py`：数据库初始化自测。

## 八、说明

- 敏感配置仅存在于本地 `.env`，未纳入版本管理；API Key 请勿外传。
- 数据库 / 向量库随首次启动自动初始化（幂等建库建表），无需手工迁移。
- 本项目为学习与技术验证目的，知识库语料来自公开演示数据。
