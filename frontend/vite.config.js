import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 构建产物直接落到 FastAPI 的静态目录 web/static/。
// base 只在 build 时需要是 /static/ —— server.py 用 app.mount("/static", StaticFiles(...))
// 挂载,产出的 index.html 里资源引用要带这个前缀才能被后端解析。
// dev 下必须是 /,否则应用被服务在 /static/ 路径下,而预览/浏览器访问的是根路径。
// emptyOutDir: false —— web/static/ 是后端挂载目录,里面可能还有构建之外的内容,
// 不能让 vite 清空它。
export default defineConfig(({ command }) => ({
  plugins: [vue()],
  base: command === "build" ? "/static/" : "/",
  build: {
    outDir: "../web/static",
    emptyOutDir: false,
  },
  server: {
    port: 3000,
    // 开发期只起 vite,把后端接口与上传文件读代理到 FastAPI(127.0.0.1:8001),
    // 这样前端始终同源请求,不用配 CORS、也不用起两套端口。
    proxy: {
      "/api": "http://127.0.0.1:8001",
      "/uploads": "http://127.0.0.1:8001",
    },
  },
  // `vite preview`(serve 构建产物)同样需要代理,否则本地看生产版时接口会 404。
  // 真实部署时前端由 FastAPI 从 /static/ 提供,天然同源,不经过这里。
  preview: {
    port: 3000,
    proxy: {
      "/api": "http://127.0.0.1:8001",
      "/uploads": "http://127.0.0.1:8001",
    },
  },
}));
