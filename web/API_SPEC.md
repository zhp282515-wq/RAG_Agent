# 前端 ↔ 后端 API 契约(API_SPEC)

> 前端 `web/static/` 只按本契约调用接口。后端 `web/server.py` 按本契约实现 FastAPI。
> 端口约定:**8001**;静态资源挂 `/static/`;`GET /` 返回 index.html。

## 基础约定

- 所有 JSON 请求/响应均为 UTF-8。
- 错误响应统一:`{"error": "错误信息"}`;状态码:400 参数错误 / 404 不存在 / 500 内部错误。
- SSE 响应:`Content-Type: text/event-stream; charset=utf-8`;每个事件一行或多行 `data: <JSON>\n\n`。

## 1. 会话(侧边栏)

### GET /api/sessions
会话列表(按 updated_at 倒序)。
```json
{"sessions": [{"session_id": "uuid", "title": "会话标题", "created_at": "2026-09-04 18:00:00", "updated_at": "2026-09-04 18:05:00"}]}
```
后端:`ReActAgentService().get_sessions()`。

### POST /api/sessions
新建会话。无请求体。
```json
{"session": {"session_id": "uuid", "title": "", "created_at": "...", "updated_at": "..."}}
```
后端:`create_or_get_session(session_id=None)`。

### GET /api/sessions/{session_id}
会话详情(含历史消息)。
```json
{"session": {..., "messages": [{"role": "user", "content": "文本(图片消息为[图片]占位)", "created_at": "..."}]}}
```
后端:`get_session(session_id)`;不存在返回 404。

### DELETE /api/sessions/{session_id}
删除会话。
```json
{"ok": true}
```
后端:`delete_session(session_id)`;不存在 404 `{"error": "会话不存在"}`。

## 2. 附件上传(聊天图片 / 文档输入)

### POST /api/upload
multipart/form-data,字段名 `file`。支持图片与文档(pdf/docx/txt/md/csv/xlsx/xls),大小限制建议与 agent.yml 一致。
```json
{"path": "data\\uploads\\xxx.png"}
```
后端:调 `utils/image_input_tool.save_uploaded_image`(或等价保存函数);失败 400。前端据此 path 再传给聊天接口。

## 3. 聊天(SSE 流式)

### POST /api/chat
JSON body:
```json
{"query": "用户问题", "session_id": "uuid", "image_paths": ["data\\uploads\\a.png"], "doc_paths": ["data\\uploads\\b.pdf"]}
```
- `session_id`:必填(前端先 POST /api/sessions 获得;续聊沿用)。
- `image_paths`:可空数组;非空时作为 image_sources 传入(多模态图片)。
- `doc_paths`:可空数组;聊天中用户附带的文档,作为知识来源处理(agent 据此补充上下文)。若后端暂不区分,可将 doc_paths 与 query 一并提交或由后端决定如何利用;契约要求字段存在即可。

**SSE 事件序列**(共六类,均 `data: {json}\n\n`):

| 事件 | 格式 | 说明 |
|---|---|---|
| trace | `{"trace": {"step": "调用工具", "name": "get_rerank_retriever", "duration": 0.52, "tokens": 8074}}` | **工作流可视化**:agent 执行阶段推送;step 取值建议:查询改写 / 调用工具 / 工具完成 / 检索完成 / 模型生成 / 模型完成;duration 为该阶段秒数(可为 null);**tokens 为该步骤 token 消耗**(模型调用取 usage_metadata,检索取 embedding+rerank 用量;无计量时省略该字段) |
| sources | `{"sources": [{"file_name": "x.pdf", "score": 0.93, "label": "高", "page": 3, "chapter": "…", "section": "…", "text": "截断至500字"}]}` | **该轮回答的溯源依据**(每次 get_rerank_retriever 执行后推送;无命中发 `{"sources": []}`);label:≥0.85 高 / ≥0.60 中 |
| ctx | `{"ctx": {"used": 4617, "total": 15805, "reason": 74}}` | **会话上下文占用**(每次模型调用后推送,驱动前端进度条)。used=最后一次模型调用的 input_tokens(真实上下文体积),分母由 `/api/settings` 的 `context_limit` 给出;total=本轮计费总量(含 embedding/rerank/摘要),reason=其中的推理 token。发生上下文压缩时附 `compressed: {before, after}` |
| token | `{"token": "文本片段"}` | 流式回答文本(可能多段) |
| done | `{"done": true}` | 结束 |
| error | `{"error": "..."}` | 出错后结束 |

**上下文压缩(会话摘要)**:历史超过 `config/agent.yml` 的 `session.trigger_tokens` 时,
较早的消息被摘要模型压成一条摘要,仅改变**模型工作记忆**(langgraph checkpointer);
`agent_session_messages` 业务表始终保存完整的用户可见历史,前端回显不受影响。
压缩发生时工作流会出现 `step="上下文压缩"` 的阶段事件(参数含 压缩前/压缩后/省下)。

**后端实现提示**:
- `stream_output` 是**同步生成器**(yield str),用 `StreamingResponse(gen)` 即可(FastAPI 线程池执行同步迭代器),每个 yield 的 str 包成 `data: {"token": ...}\n\n`。
- trace/sources 由**中间件回调**收集:在 `agent_middleware.monitor_tool`(已记录工具名与参数)与 `log_befor_model/after_model` 挂收集回调(线程安全队列),外层包装生成器边迭代 token 边把队列内容作为 trace/sources 事件发出。工具结果文本(ToolMessage)可解析出 sources(格式见 agent_tools.get_rerank_retriever 返回)。
- 会话消息落库由 stream_output 内部完成(append_message),后端无需重复处理。

## 4. 报告(SSE 流式)

### POST /api/report
JSON body:
```json
{"user_id": "1001", "month": "2026-08"}
```
SSE 事件与聊天相同(可含 trace/sources/token/done/error)。
后端:拼 query(如"用户ID是1001,帮我生成2026年8月的使用报告")后走 `stream_output`;token 为 Markdown 文本(前端 marked 渲染)。

## 5. 检索调试(同步 JSON)

### POST /api/search
```json
{"query": "耗材更换", "top_k": 20}
```
响应:
```json
{"hits": [{"score": 0.93, "label": "高", "file_name": "x.pdf", "page": 3, "chapter": "…", "section": "…", "text": "正文(截断至500字)"}]}
```
- label:≥高相关线(默认 0.85)高 / ≥达标线(默认 0.60)中;<达标线不返回。两阈值可经「系统配置」全局调整,检索调试结果跟随全局。
- 无命中:`{"hits": []}`。
- top_k:请求体提供为**临时覆盖**(不写库、仅本次);缺省回退全局默认(系统配置,原 20)。
- 后端:`VectorStoreService().get_rerank_retriever(top_k, rerank_n)` 后按全局达标线过滤并标注等级。

## 6. 向量库管理 + 文档管理(知识库页)

支持多向量库(Milvus collection)。agent 检索查询「当前向量库」,在系统配置里切换;
知识库页左栏可新建/切换/删除。

### GET /api/stores
```json
{"stores": [{"name": "document_chunks", "chunks": 141, "is_default": true}], "current": "document_chunks"}
```
`chunks` 为分片数(统计失败时为 -1)。后端:`VectorStoreService.list_stores()`。

### POST /api/stores
body `{"name": "my_store"}`。名称仅允许字母/数字/下划线且不以数字开头。
```json
{"ok": true, "name": "my_store", "created": true}
```
已存在时 `created` 为 false(幂等);名称非法 400 `{"error": "..."}`。

### DELETE /api/stores/{name}
**连同库内全部分片一并删除,不可恢复。** 若删除的正是当前库,`rag.current_store` 自动回退默认库。
```json
{"ok": true, "name": "my_store"}
```
不存在:404。

### PUT /api/stores/current
body `{"name": "my_store"}`,切换 agent 检索用的当前库。
```json
{"ok": true, "current": "my_store"}
```
库不存在:404。

### PUT /api/stores/{name}
重命名向量库(纯元数据改名,**分片数据不动**)。body `{"name": "新名"}`。
```json
{"ok": true, "name": "新名", "renamed": true}
```
新旧同名时 `renamed` 为 false(空操作)。名称非法 / 源库不存在 / 新名被占用:400 `{"error": "..."}`。
若改的正是当前检索库,`rag.current_store` 会同步更新为 `新名`。

> 注意:`/api/stores/current` 必须声明在 `/api/stores/{name}` 之前 —— FastAPI 按注册顺序匹配,
> 否则 `current` 会被当成库名路由到重命名处理器。

### POST /api/documents
multipart/form-data,字段名 `file`;可选 query 参数 `store`(缺省=当前库)。
- 入库成功(200):`{"ok": true, "file_name": "x.pdf", "chunk_count": 16, "message": "已入库", "store": "…"}`
- md5 重复(200):`{"ok": false, "message": "该文档已存在(md5 去重)"}`
- 失败(400):`{"error": "..."}`
后端:文件存 `data/uploads/` 后调 `VectorStoreService(collection_name=store).load_document(path)`;入库为耗时操作(embedding),前端会显示 loading。

### GET /api/documents
可选 query 参数 `store`(缺省=当前库)。
```json
{"documents": [{"file_name": "x.pdf", "file_type": "pdf", "file_size": 22870, "chunk_count": 16}], "store": "document_chunks"}
```
后端:`list_documents()`。

### GET /api/documents/preview/{file_name} · GET /api/documents/chunks/{file_name}
均支持可选 query 参数 `store`。

### DELETE /api/documents/{file_name}
file_name 需 URL 编码;支持可选 query 参数 `store`。
```json
{"ok": true}
```
不存在:404 `{"error": "文档不存在"}`。后端:`del_document(file_name)`。

## 7. 报告记录(报告页,MySQL 持久化)

> 建议建表 `reports`(report_id 自增主键 / user_id / month / title / content LONGTEXT / created_at / updated_at),可与 session_store 同库。
> **保存时机**:报告由聊天链路生成(agent 识别报告意图→fill_context_for_report→生成 Markdown);后端在报告生成完成后**自动保存**到 reports 表(从流式全文或最终消息中提取),前端报告页不做保存操作,只查看/编辑/下载。

### GET /api/reports(列表,按 created_at 倒序)
```json
{"reports": [{"report_id": 1, "title": "...", "user_id": "1001", "month": "2026-08", "created_at": "...", "updated_at": "..."}]}
```

### GET /api/reports/{report_id}(详情,含 content)
```json
{"report": {"report_id": 1, "...": "...", "content": "markdown 全文"}}
```
不存在:404。

### PUT /api/reports/{report_id}(编辑保存)
```json
{"title": "...", "content": "..."}
```
响应:`{"ok": true}`;不存在 404。

### DELETE /api/reports/{report_id}
```json
{"ok": true}
```
不存在 404。

> 下载:前端用 Blob 在浏览器本地导出 .md(后端无需下载接口)。

## 8. 系统配置(系统配置页,MySQL app_settings)
设置存 `app_settings` 表(skey / value_json)。服务端在**每次检索 / 构建 agent 前现读**,
改完「下次提问即生效」,无需重启。

### GET /api/settings
读全部可配置项 + 可选模型清单 + API Key 是否已配置(不返回明文/密文)。
```json
{
  "settings": {"retrieval.top_k":20,"retrieval.rerank_n":5,"retrieval.score_high":0.85,
               "retrieval.score_min":0.6,"model.default":"qwen3.8-flash","model.temperature":0.7},
  "model_default": "qwen3.8-flash",
  "temperature": 0.7,
  "api_key_configured": true,
  "retrieval": {"top_k":20,"rerank_n":5,"score_high":0.85,"score_min":0.6},
  "models": ["qwen3.8-flash","qwen3.8-max","qwen3-vl-flash","qwen3-vl-plus"]
}
```

### PUT /api/settings
写检索三件套 / 默认模型 / 温度 / API Key。支持整段或分项:
```json
{"retrieval":{"top_k":25,"rerank_n":6,"score_high":0.9,"score_min":0.65}}
{"model":{"default":"qwen3.8-max","temperature":0.5}}
{"model":{"api_key":"sk-…"}}    // 写 DashScope API Key(加密落库,供 聊天/向量/精排/图片 全部服务)
{"model":{"api_key":""}}        // 清除 DB key,回退 .env 的 DASHSCOPE_API_KEY
```
- 校验:top_k/rerank_n 1~100;score_high>0;score_min∈[0,1);temperature∈[0,2]。非法返回 400。
- `api_key` 不回读明文;GET 只返回 `api_key_configured` 布尔。
- 响应 `{"ok": true}`。

### API Key 门槛
`POST /api/chat`、`POST /api/report`、`POST /api/documents` 在**未配置任何可用 API Key**
(DB key 与 .env 均无)时返回 `403`:
`未配置模型服务 API Key,请先在「系统配置 → 对话模型」填入 API Key 后再使用`。

## 9. 工具(系统配置 → 工具,MySQL tool_registry)
可插拔工具注册表:8 个内置工具默认全开、可独立启停;外部 MCP 工具(stdio/http)注册挂载。
检索类内置工具两个:`search_knowledge_base`(推荐入口,内部含「问题改写 → 一致性自检 →
分级路由」闭环,入参为**用户原始问题**)与 `get_rerank_retriever`(直通检索,入参为检索词)。
工具启停/注册变化会 bump 服务端 agent 缓存版本 → **下次提问自动重建 agent**(新工具集生效)。

### GET /api/tools
```json
{"tools":[
  {"key":"get_rerank_retriever","kind":"builtin","label":"知识库检索","group":"检索","enabled":true,
   "default_enabled":true,"description":"…","note":"关闭将禁用知识库检索(无 RAG 问答)","config":{},"transport":""},
  {"key":"math_tools","kind":"external","label":"数学计算工具","enabled":true,"transport":"stdio",
   "config":{"command":"…","args":["…"]}}
],"rev":4}
```

### PUT /api/tools/{key}
开/关任意工具(内置或外部):body `{"enabled":true|false}`。未知 404;成功 `{"ok":true,"rev":N}`。

### POST /api/tools(注册外部 MCP 工具)
```json
{"key":"math_tools","label":"数学计算工具","transport":"stdio",
 "config":{"command":"C:/…/python.exe","args":["D:/server.py"]}}
// http:
{"key":"srv","label":"…","transport":"http","config":{"url":"https://…"}}
```
- key 限 `[A-Za-z0-9_]{1,64}`,不能是内置工具名 / 重复 → 400。
- 注册前做**短超时连通探测**(起 MCP 会话列工具),失败 400 不落库。
- 成功:落库(enabled=1)+ bump rev,返回 `{"ok":true,"rev":N,"mcp_tools":[{name,description,args}]}`。

### DELETE /api/tools/{key}
仅外部工具可删;内置 → 400;不存在 → 404。成功 `{"ok":true,"rev":N}`。

> 外部工具错误处理:挂载的外部 MCP 工具会被统一包装,异常/重试后返回 `@@TOOL_ERROR@@`
> 结构化文本(与内置工具一致),让模型按「建议」修正/如实告知,不裸奔 langchain 错误。

## 附:前端行为约定(后端需知晓)

1. 聊天前前端会先确保有 session_id(无则 POST /api/sessions)。
2. 前端"空会话去重":已有空会话时不再新建(纯前端逻辑,后端无需处理)。
3. 图片流程:先 POST /api/upload 拿 path,再随 POST /api/chat 的 image_paths 传入。
