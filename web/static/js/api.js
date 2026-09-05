/* ============================================
   api.js — fetch 封装 + SSE 流式读取器
   依赖契约:web/API_SPEC.md
   ============================================ */

/** 线性 SVG 图标 helper:ic("trash", "ic-sm") -> <svg class="ic ic-sm"><use href="#i-trash"/></svg> */
function ic(name, cls) {
  return `<svg class="ic${cls ? " " + cls : ""}"><use href="#i-${name}"/></svg>`;
}

const API = {
  /** GET JSON */
  async get(url) {
    const r = await fetch(url);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `请求失败(${r.status})`);
    return data;
  },

  /** POST JSON */
  async post(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `请求失败(${r.status})`);
    return data;
  },

  /** PUT JSON */
  async put(url, body) {
    const r = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `请求失败(${r.status})`);
    return data;
  },

  /** DELETE */
  async del(url) {
    const r = await fetch(url, { method: "DELETE" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `请求失败(${r.status})`);
    return data;
  },

  /** 上传文件(multipart) */
  async upload(url, file) {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(url, { method: "POST", body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `上传失败(${r.status})`);
    return data;
  },
};

/**
 * SSE 流式读取(POST + fetch ReadableStream)。
 * 解析 `data: {json}\n\n` 事件,通过回调分发五类事件:
 *   onToken(str) / onTrace(obj) / onSources(arr) / onDone() / onError(msg)
 * 返回 Promise<{aborted: boolean}>;调用方持 handle.abort() 可中断。
 */
function sseRequest({ url, body, onToken, onTrace, onSources, onDone, onError }) {
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
      try { const d = await resp.json(); msg = d.error || msg; } catch (_) {}
      onError && onError(msg);
      return { aborted: false };
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        // 按 \n\n 切事件;末尾不完整段留在 buf
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of chunk.split("\n")) {
            if (!line.startsWith("data:")) continue;
            let payload;
            try { payload = JSON.parse(line.slice(5).trim()); }
            catch (_) { continue; }
            dispatch(payload);
          }
        }
      }
      if (buf.trim()) {
        for (const line of buf.split("\n")) {
          if (!line.startsWith("data:")) continue;
          let payload;
          try { payload = JSON.parse(line.slice(5).trim()); }
          catch (_) { continue; }
          dispatch(payload);
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") onError && onError(String(e));
      return { aborted: e.name === "AbortError" };
    }
    return { aborted: false };

    function dispatch(p) {
      if (p.done) onDone && onDone();
      else if (p.error) onError && onError(p.error);
      else if (p.token) onToken && onToken(p.token);
      else if (p.trace) onTrace && onTrace(p.trace);
      else if (p.sources) onSources && onSources(p.sources);
    }
  })();

  return {
    promise: run,
    abort: () => ctrl.abort(),
  };
}
