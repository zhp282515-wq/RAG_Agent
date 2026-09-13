#!/usr/bin/env python
"""一键启动:构建前端 → 启动后端。

前端源码在 frontend/,构建产物落在 web/static/(由 FastAPI 当静态文件托管)。
所以改了 frontend/src/ 必须先构建,否则 8001 发出的还是旧页面。

用法:
    python start.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(ROOT, "frontend")
IS_WIN = os.name == "nt"

# 输出重定向到文件时 Python 默认块缓冲,提示语会被压到最后才出现;
# 而子进程(npm/uvicorn)直接写 fd,于是日志看起来像"少了开头"。改行缓冲。
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass


def npm_cmd():
    """Windows 上 npm 是 npm.cmd,直接调 `npm` 会找不到。"""
    return "npm.cmd" if IS_WIN else "npm"


def run(cmd, cwd):
    """执行命令;失败抛 CalledProcessError,由调用方给出中文提示。"""
    return subprocess.run(cmd, cwd=cwd, shell=False, check=True)


def main():
    print("=" * 44)
    print(" RAG Agent 启动")
    print("=" * 44)

    if not shutil.which(npm_cmd()):
        print("[错误] 未找到 npm,请先安装 Node.js (https://nodejs.org)")
        return 1

    print("\n[1/2] 构建前端...")
    if not os.path.isdir(os.path.join(FRONTEND, "node_modules")):
        print("      首次运行,安装前端依赖(可能要几分钟)...")
        try:
            run([npm_cmd(), "install", "--no-audit", "--no-fund"], FRONTEND)
        except subprocess.CalledProcessError:
            print("[错误] 前端依赖安装失败")
            return 1
    try:
        run([npm_cmd(), "run", "build"], FRONTEND)
    except subprocess.CalledProcessError:
        # 构建失败就别起后端:否则用户会看到一个旧页面,还不知道为什么
        print("[错误] 前端构建失败,已中止(不启动后端,避免发出旧页面)")
        return 1
    print("      完成")

    print("\n[2/2] 启动后端 http://127.0.0.1:8001")
    print("      按 Ctrl+C 停止\n")
    # 用同一个解释器起后端,保证和当前环境一致(venv / uv run 都适用)
    try:
        return subprocess.call([sys.executable, os.path.join("web", "server.py")], cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
