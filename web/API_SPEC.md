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

**SSE 事件序列**(共五类,均 `data: {json}\n\n`):

| 事件 | 格式 | 说明 |
|---|---|---|
| trace | `{"trace": {"step": "调用工具", "name": "get_rerank_retriever", "duration": 0.52}}` | **工作流可视化**:agent 执行阶段推送;step 取值建议:查询改写 / 调用工具 / 工具完成 / 检索完成 / 模型生成 / 模型完成;duration 为该阶段秒数(可为 null) |
| sources | `{"sources": [{"file_name": "x.pdf", "score": 0.93, "label": "高", "page": 3, "chapter": "…", "section": "…", "text": "截断至500字"}]}` | **该轮回答的溯源依据**(每次 get_rerank_retriever 执行后推送;无命中发 `{"sources": []}`);label:≥0.85 高 / ≥0.60 中 |
| token | `{"token": "文本片段"}` | 流式回答文本(可能多段) |
| done | `{"done": true}` | 结束 |
| error | `{"error": "..."}` | 出错后结束 |

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
- label:score≥0.85 高 / ≥0.60 中;<0.60 不返回。
- 无命中:`{"hits": []}`。
- 后端:`VectorStoreService().search(query, top_k)` 后按阈值过滤并标注等级。

## 6. 文档管理(知识库页)

### POST /api/documents
multipart/form-data,字段名 `file`。
- 入库成功(200):`{"ok": true, "file_name": "x.pdf", "chunk_count": 16, "message": "已入库"}`
- md5 重复(200):`{"ok": false, "message": "该文档已存在(md5 去重)"}`
- 失败(400):`{"error": "..."}`
后端:文件存 `data/uploads/` 后调 `VectorStoreService().load_document(path)`;入库为耗时操作(embedding),前端会显示 loading。

### GET /api/documents
```json
{"documents": [{"file_name": "x.pdf", "file_type": "pdf", "file_size": "22.87 KB", "chunk_count": 16}]}
```
后端:`list_documents()`。

### DELETE /api/documents/{file_name}
file_name 需 URL 编码。
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

## 附:前端行为约定(后端需知晓)

1. 聊天前前端会先确保有 session_id(无则 POST /api/sessions)。
2. 前端"空会话去重":已有空会话时不再新建(纯前端逻辑,后端无需处理)。
3. 图片流程:先 POST /api/upload 拿 path,再随 POST /api/chat 的 image_paths 传入。
