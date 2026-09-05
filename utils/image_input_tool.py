"""图片本地上传/引用工具：解析本地图片路径、校验、取字节与 MIME。

"上传"在本项目当前(库/CLI 层)即指图片已在本机磁盘,由调用方把本地路径交给本模块
解析;后续 Web 上传也统一复用 save_uploaded_image 落盘到 data/uploads 后再解析。

对外主要接口:
    resolve_image_paths(source)   -> 规范化后的本地图片绝对路径列表
    parse_local_image(path)       -> 图片原始字节(或 None)
    path_to_mime(path)            -> image/png|jpeg|gif|webp|bmp(不支持返回 None)
    save_uploaded_image(src, dir) -> 把图片复制到目标目录,返回新路径
"""

import os
import shutil

from utils.logger_tool import logger
from utils.path_tool import get_abs_path

# 支持的图片扩展名 -> MIME
_IMG_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
# 单张图片大小上限(10MB),超出拒绝并提示
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# demo 默认上传目录
UPLOAD_DIR = get_abs_path("data\\uploads")


def _is_image_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _IMG_MIME


def path_to_mime(path: str) -> str | None:
    """按扩展名返回 MIME;非支持图片格式返回 None。"""
    return _IMG_MIME.get(os.path.splitext(path)[1].lower())


def resolve_image_paths(source: str) -> list[str]:
    """把用户给的图片引用解析为本地绝对路径列表。

    Args:
        source: ①图片本地绝对/相对路径 ②目录(取其中所有图片) ③data/uploads 下的文件名。

    Returns:
        规范化(绝对)路径列表。非法/不存在/超限项跳过并打 warning;全空返回 []。
    """
    if not source or not str(source).strip():
        return []

    p = str(source).strip()
    abs_p = os.path.abspath(p) if os.path.isabs(p) else os.path.abspath(os.path.join(UPLOAD_DIR, p))
    if not os.path.exists(abs_p):
        # 相对项目根目录再试一次
        alt = os.path.abspath(os.path.join(get_abs_path("."), p))
        abs_p = alt if os.path.exists(alt) else abs_p
    if not os.path.exists(abs_p):
        logger.warning(f"resolve_image_paths: 路径不存在:{p}")
        return []

    candidates: list[str] = []
    if os.path.isdir(abs_p):
        candidates = [
            os.path.join(abs_p, f)
            for f in sorted(os.listdir(abs_p))
            if _is_image_file(f)
        ]
    else:
        candidates = [abs_p]

    out: list[str] = []
    for path in candidates:
        if not _is_image_file(path):
            logger.warning(f"resolve_image_paths: 不支持的图片格式:{path}")
            continue
        size = os.path.getsize(path)
        if size > MAX_IMAGE_BYTES:
            logger.warning(f"resolve_image_paths: 图片超限({size}字节>{MAX_IMAGE_BYTES}):{path}")
            continue
        out.append(path)

    if not out:
        logger.warning(f"resolve_image_paths: 未解析到可用图片:{source}")
    else:
        logger.info(f"resolve_image_paths: 解析到 {len(out)} 张图片")
    return out


def parse_local_image(path: str) -> bytes | None:
    """读取本地图片为原始字节;不存在/读取失败返回 None。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        logger.error(f"parse_local_image: 读取图片失败:{path}:{e}")
        return None


def save_uploaded_image(src: str, dest_dir: str | None = None) -> str | None:
    """把本地图片复制到目标目录(默认 data/uploads),避免覆盖同名,返回新路径。"""
    if not path_to_mime(src):
        logger.error(f"save_uploaded_image: 非支持图片:{src}")
        return None
    dest_dir = dest_dir or UPLOAD_DIR
    os.makedirs(dest_dir, exist_ok=True)

    base = os.path.basename(src)
    name, ext = os.path.splitext(base)
    dest = os.path.join(dest_dir, base)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{name}_{n}{ext}")
        n += 1
    try:
        shutil.copyfile(src, dest)
    except Exception as e:
        logger.error(f"save_uploaded_image: 复制失败:{e}")
        return None
    logger.info(f"save_uploaded_image: 已保存到 {dest}")
    return dest


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    for p in resolve_image_paths(target):
        print(p, "->", path_to_mime(p), f"({len(parse_local_image(p) or b'')} 字节)")
