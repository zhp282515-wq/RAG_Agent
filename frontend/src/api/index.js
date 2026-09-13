/* ============================================
   api.js — fetch 封装 + SSE 流式读取器
   依赖契约:web/API_SPEC.md
   ============================================ */

/**
 * GET JSON。失败时抛出 Error(data.error || "请求失败(status)")。
 * 与原生版 API.get 行为一致。
 */
async function get(url) {
  const r = await fetch(url);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `请求失败(${r.status})`);
  return data;
}

/** POST JSON */
async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `请求失败(${r.status})`);
  return data;
}

/** PUT JSON */
async function put(url, body) {
  const r = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `请求失败(${r.status})`);
  return data;
}

/** DELETE */
async function del(url) {
  const r = await fetch(url, { method: "DELETE" });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `请求失败(${r.status})`);
  return data;
}

/** 上传文件(multipart) */
async function upload(url, file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(url, { method: "POST", body: fd });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `上传失败(${r.status})`);
  return data;
}

export const API = { get, post, put, del, upload };

/**
 * SSE 流式读取(POST + fetch ReadableStream)。
 *
 * 解析 `data: {json}\n\n` 事件,把 payload 分发给对应回调。
 * 派发是**单条 if/else 链**(优先级 done > error > token > trace > sources > ctx):
 * 一个 payload 带多个键时只触发优先级最高那个 —— 与原生版语义保持一致。
 *
 * 返回 { promise: Promise<{aborted}>, abort }。调用方持 abort() 可中断;
 * **中断不触发 onError**,靠 promise 解析出的 aborted 区分。
 */
export function sseRequest({ url, body, onToken, onTrace, onSources, onCtx, onDone, onError }) {
  const ctrl = new AbortController();

  const run = (async () => {
    let resp;
    try {
      resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
    } catch (e) {
      if (e.name !== "AbortError") onError && onError(String(e));
      return { aborted: e.name === "AbortError" };
    }

    if (!resp.ok || !resp.body) {
      let msg = `请求失败(${resp.status})`;
      try {
        const d = await resp.json();
        msg = d.error || msg;
      } catch (_) {}
      onError && onError(msg);
      return { aborted: false };
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    function dispatch(p) {
      if (p.done) onDone && onDone();
      else if (p.error) onError && onError(p.error);
      else if (p.token) onToken && onToken(p.token);
      else if (p.trace) onTrace && onTrace(p.trace);
      else if (p.sources) onSources && onSources(p.sources);
      else if (p.ctx) onCtx && onCtx(p.ctx);
    }

    // 把 buffer 里所有完整事件(以 \n\n 分隔)解析并派发;末尾不完整段留在 buf
    function drain(buffer) {
      let rest = buffer;
      let idx;
      while ((idx = rest.indexOf("\n\n")) !== -1) {
        const chunk = rest.slice(0, idx);
        rest = rest.slice(idx + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data:")) continue;
          let payload;
          try {
            payload = JSON.parse(line.slice(5).trim());
          } catch (_) {
            continue;
          }
          dispatch(payload);
        }
      }
      return rest;
    }

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        buf = drain(buf);
      }
      // 流结束:flush 掉末尾残留的完整事件(可能没有结尾的 \n\n)
      if (buf.trim()) drain(buf + "\n\n");
    } catch (e) {
      if (e.name !== "AbortError") onError && onError(String(e));
      return { aborted: e.name === "AbortError" };
    }
    return { aborted: false };
  })();

  return {
    promise: run,
    abort: () => ctrl.abort(),
  };
}
