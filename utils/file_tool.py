"""文件加载工具：将不同格式文件解析为 LangChain Document 列表。

支持 txt、markdown、pdf、docx、csv、xlsx/xls。

csv/xlsx/xls 以管道分隔的表格式文本输出（保留表头与行对应）。
xlsx 含合并单元格的 sheet 按左上角值展开；顶部被独占或仅单列的
标题/汇总行会识别为对下表信息的说明而保留在表头行之前（不误作表头）。
markdown 清洗规则：
- 只保留纯文本：去掉 #、*、[链接]()、`、<标签>、表格分隔线等标记字符；
- 在此基础上，以纯文本的序号形式还原 md 排版：
  - 有序列表条目重排为连续序号（1. 2. 3.…）；
  - 紧跟在某条有序条目正下方的无序 “-” 条目，视为该条目的子内容
    （例如“问题 + 下方答案”的问答结构），以缩进行归属到其编号条目，
    不额外占用新序号；
  - 不与编号条目相邻的独立无序列表，保留 “-” 标记与层级缩进。
"""

import csv
import math
import os
import re
import time

import pymupdf  # PyMuPDF 1.28+ 官方推荐导入名
import docx
import openpyxl
import pandas as pd
from langchain_core.documents import Document
from langchain_community.document_loaders import TextLoader

from utils.logger_tool import logger, fmt_duration
from utils.vlm_tool import describe_image

from datetime import datetime as dt

# 图片描述段落分隔符：便于文本检索命中与溯源
IMG_SEP = "\n\n===[本页图片 {idx}]===\n{desc}\n====================\n"


def _inline_clean(s: str) -> str:
    """去掉行内的 markdown 标记字符，只保留文字内容。"""
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)  # 图片保留 alt 文字
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)    # 链接保留文字
    s = re.sub(r"<[^>]+>", "", s)                      # HTML 标签
    s = re.sub(r"`([^`]*)`", r"\1", s)                 # 行内代码保留内容
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", s)         # 加粗
    s = re.sub(r"__([^_\n]+)__", r"\1", s)             # 加粗
    s = re.sub(r"~~([^~\n]+)~~", r"\1", s)             # 删除线
    s = re.sub(r"\*([^*\n]+)\*", r"\1", s)             # 斜体
    s = re.sub(r"_([^_\n]+)_", r"\1", s)               # 斜体
    return s.strip()


def _dedent_code(code: str) -> str:
    """去掉代码块的公共缩进。"""
    lines = code.split("\n")
    indents = [
        len(ln) - len(ln.lstrip())
        for ln in lines
        if ln.strip()
    ]
    cut = min(indents) if indents else 0
    return "\n".join(ln[cut:] if ln.strip() else "" for ln in lines).strip()


def _clean_markdown(text: str) -> str:
    """去掉 markdown 标记，按文本序号还原排版，返回纯文本。"""
    # 1) 先把代码块抽离成占位（删围栏，代码内容后置还原）
    code_blocks: list[str] = []

    def _extract_code(m: re.Match) -> str:
        code_blocks.append(_dedent_code(m.group(1)))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[^\n]*\n([\s\S]*?)```", _extract_code, text)

    out: list[str] = []
    para: list[str] = []
    frames: list[dict] = []   # 有序列表计数帧：{depth, count}
    open_question = False     # 上一条内容是有序条目，其正下方的 "-" 归属为子内容
    gap = False               # 与上一内容行之间是否隔了空行

    def close_para():
        if para:
            out.append(" ".join(p for p in para if p))
            para.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        indent = len(line) - len(line.lstrip())

        if not s:
            # 空行：结束段落；打断 "-" 与上一条有序条目的“直接相邻”关系
            close_para()
            gap = True
            continue

        # 代码块占位还原
        cm = re.fullmatch(r"\x00CODEBLOCK(\d+)\x00", s)
        if cm:
            close_para()
            frames.clear()
            open_question = False
            if out and out[-1] != "":
                out.append("")
            out.append(code_blocks[int(cm.group(1))])
            out.append("")
            gap = False
            continue

        # 表格行（以 | 开头或含多个 |）
        if s.startswith("|") or s.count("|") >= 2:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue  # 表头分隔行整行删除
            close_para()
            frames.clear()
            open_question = False
            if out and out[-1] != "":
                out.append("")
            out.append(" | ".join(_inline_clean(c) for c in cells))
            out.append("")
            gap = False
            continue

        # 标题：# 文字（去掉井号，保留标题文字，不打断序号连续）
        hm = re.match(r"^(#{1,6})\s+(.*)$", s)
        if hm:
            close_para()
            open_question = False
            if out and out[-1] != "":
                out.append("")
            out.append(_inline_clean(hm.group(2)))
            out.append("")
            gap = False
            continue

        # 分隔线
        if re.fullmatch(r"(?:[-*_]\s*){3,}", s):
            close_para()
            frames.clear()
            open_question = False
            out.append("")
            gap = False
            continue

        # 有序列表条目：1. / 1)（按出现顺序重排为连续序号）
        om = re.match(r"^(\d+)[.)]\s+(.*)$", s)
        if om:
            close_para()
            depth = indent // 2
            while frames and frames[-1]["depth"] > depth:
                frames.pop()
            if frames and frames[-1]["depth"] == depth:
                frames[-1]["count"] += 1
                idx = frames[-1]["count"]
            else:
                frames.append({"depth": depth, "count": 1})
                idx = 1
            prefix = "  " * (len(frames) - 1)
            out.append(f"{prefix}{idx}. {_inline_clean(om.group(2))}")
            open_question = True
            gap = False
            continue

        # 无序列表条目：- / * / +
        bm = re.match(r"^[-*+]\s+(.*)$", s)
        if bm:
            content = _inline_clean(bm.group(1))
            if not content:
                gap = False
                continue
            if open_question and not gap and not para:
                # 直接紧跟在编号条目正下方：作为其子内容归属（如问答的答案）
                lvl = max(len(frames), 1)
                out.append("  " * lvl + content)
            else:
                # 独立无序列表：保留 “-” 标记与层级缩进
                close_para()
                prefix = "  " * (indent // 2)
                out.append(f"{prefix}- {content}")
                open_question = False
            gap = False
            continue

        # 引用：> 文字
        qm = re.match(r"^>\s?(.*)$", s)
        if qm:
            close_para()
            frames.clear()
            open_question = False
            out.append(_inline_clean(qm.group(1)))
            out.append("")
            gap = False
            continue

        # 普通段落文字
        cleaned = _inline_clean(s)
        if cleaned:
            if not para and open_question:
                # 编号列表之后直接接普通段落：列表到此结束
                frames.clear()
                open_question = False
            para.append(cleaned)
            gap = False

    close_para()

    # 合并多余空行、去掉首尾空白
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return result.strip()


def get_txt_document(file_path: str) -> list[Document] | bool:
    """读取 txt 文件，返回 Document 列表。"""
    t0 = time.perf_counter()
    try:
        loader = TextLoader(file_path, encoding="utf-8")
        docs = loader.load()
        logger.info(f"get_txt_document: 解析完成 {os.path.basename(file_path)},耗时 {fmt_duration(time.perf_counter()-t0)},{len(docs)} 篇 Document")
        return docs
    except Exception as e:
        logger.error(f"get_txt_document: 读取文件 {file_path} 时出错：{e}")
        return False


def get_markdown_document(file_path: str) -> list[Document] | bool:
    """读取 markdown 文件，去掉标记只保留文本，返回 Document 列表。"""
    t0 = time.perf_counter()
    try:
        loader = TextLoader(file_path, encoding="utf-8")
        docs = loader.load()
        result = [Document(page_content=_clean_markdown(doc.page_content)) for doc in docs]
        logger.info(f"get_markdown_document: 解析完成 {os.path.basename(file_path)},耗时 {fmt_duration(time.perf_counter()-t0)},{len(result)} 篇 Document")
        return result
    except Exception as e:
        logger.error(f"get_markdown_document: 读取文件 {file_path} 时出错：{e}")
        return False

def get_pdf_document(file_path: str) -> list[Document] | bool:
    """读取 PDF,返回 Document 列表。

    pdf_layout=true(默认)走布局感知解析:文本行/表格/图片按阅读顺序合流、
    过滤页眉页脚与页码、识别标题结构并跨页维护章节链、metadata 带页码与章节;
    解析异常自动降级简单提取。pdf_layout=false 走旧版每页一条的简单提取。
    """
    t0 = time.perf_counter()
    try:
        with pymupdf.open(file_path) as pdf:
            if _pdf_layout_enabled():
                try:
                    docs = _get_pdf_document_layout(pdf)
                    logger.info(f"get_pdf_document: 解析完成 {os.path.basename(file_path)},耗时 {fmt_duration(time.perf_counter()-t0)},{len(docs)} 篇 Document(布局解析)")
                    return docs
                except Exception as e:
                    logger.warning(f"get_pdf_document: 布局解析失败,降级简单提取: {e}")
            docs = _get_pdf_document_legacy(pdf)
            logger.info(f"get_pdf_document: 解析完成 {os.path.basename(file_path)},耗时 {fmt_duration(time.perf_counter()-t0)},{len(docs)} 篇 Document(简单提取)")
            return docs
    except Exception as e:
        logger.error(f"get_pdf_document: 读取文件 {file_path} 时出错：{e}")
        return False


# ---------- PDF 布局感知解析(内部 helper) ----------

_HEADING_HASH_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HEADING_CN_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百0-9]+[章节部分条]")
_HEADING_CN_ENUM_RE = re.compile(r"^[一二三四五六七八九十]+、")
_HEADING_CN_PAREN_RE = re.compile(r"^（[一二三四五六七八九十]+）")
_HEADING_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.+)$")
_PAGE_NUM_RE = re.compile(r"^(\d{1,4}|第?\s*\d+\s*页|-\s*\d+\s*-|\d+\s*/\s*\d+)$")


def _pdf_layout_enabled() -> bool:
    """读取 vector_store.yml 的 pdf_layout 开关;读取失败默认启用新路径。"""
    try:
        from utils.config_tool import vector_store_conf
        return bool(vector_store_conf.get("pdf_layout", True))
    except Exception:
        return True


def _norm_line(line: str) -> str:
    """折叠全部空白,用于跨页重复行(页眉页脚)比较。"""
    return re.sub(r"\s+", "", line)


def _is_page_number(line: str) -> bool:
    """判断是否为孤立页码行(纯数字/第N页/-N-/N/M)。"""
    return bool(_PAGE_NUM_RE.match(line))


def _heading_level(text: str, max_size: float, bold: bool, size_mode: float | None) -> int | None:
    """识别标题并返回层级 1~6,非标题返回 None。

    优先级:字面 # 标记 > 中文"第X章/节/部/条" > 中文序号(一、/（一）)
    > 数字编号(1. / 1.2,长度<40 且不含 **,排除"12. **问答标题**"这类正文)
    > 字号兜底(>页内众数+0.5pt)或粗体兜底 → 3 级。
    """
    m = _HEADING_HASH_RE.match(text)
    if m:
        return len(m.group(1))
    if _HEADING_CN_CHAPTER_RE.match(text):
        return 2
    if _HEADING_CN_ENUM_RE.match(text):
        return 2
    if _HEADING_CN_PAREN_RE.match(text):
        return 3
    m = _HEADING_NUM_RE.match(text)
    if m and len(text) < 40 and "**" not in text:
        return min(m.group(1).count(".") + 1, 6)
    if size_mode is not None and max_size > size_mode + 0.5:
        return 3
    if bold:
        return 3
    return None


def _page_plain_text(page) -> str:
    """降级路径:整页简单文本提取。"""
    return page.get_text("text").strip()


def _page_items(page) -> list:
    """收集一页的文本行/表格/图片,按 (y0, x0) 排序。

    返回 [(bbox, kind, payload)]:
      kind="line"  payload=(text, max_size, bold)
      kind="table" payload=Table 对象
      kind="img"   payload=xref
    表格 bbox 内的文本行不重复收录(表格文字由表格项自己贡献)。
    """
    items: list = []
    tables = page.find_tables().tables

    def _in_table(bbox) -> bool:
        x0, y0, x1, y1 = bbox
        for t in tables:
            tx0, ty0, tx1, ty1 = t.bbox
            if tx0 - 3 <= x0 and ty0 - 3 <= y0 and x1 <= tx1 + 3 and y1 <= ty1 + 3:
                return True
        return False

    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = ln["spans"]
            text = "".join(s["text"] for s in spans).strip()
            if not text:
                continue
            if _in_table(ln["bbox"]):
                continue
            max_size = max(s["size"] for s in spans)
            bold = any(s["flags"] & 16 for s in spans)
            items.append((ln["bbox"], "line", (text, max_size, bold)))

    for t in tables:
        items.append((t.bbox, "table", t))

    for info in sorted(page.get_image_info(xrefs=True),
                       key=lambda r: (r["bbox"][1], r["bbox"][0])):
        xref = info.get("xref")
        if not xref:
            continue
        items.append((info["bbox"], "img", xref))

    items.sort(key=lambda r: (r[0][1], r[0][0]))
    return items


def _clean_table_md(md: str) -> str:
    """表格 Markdown 清洗:br 换行转空格(含 HTML 转义);删除含 Col数字 幻影单元格的行。"""
    out: list[str] = []
    for line in md.splitlines():
        line = (line.replace("&lt;br&gt;", " ").replace("&lt;br/&gt;", " ")
                .replace("<br>", " ").replace("<br/>", " ").strip())
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and any(re.fullmatch(r"Col\d+", c) for c in cells):
            continue  # 幻影表头/占位行(表格检测器的 Col 列占位)
        out.append(line)
    return "\n".join(out).strip()


def _table_to_markdown(table) -> str:
    """表格转 Markdown 文本;失败降级为管道分隔行(与 csv/xlsx 入库约定一致)。"""
    try:
        return _clean_table_md(table.to_markdown(clean=True))
    except Exception as e:
        logger.warning(f"_table_to_markdown: Markdown 转换失败,降级管道分隔: {e}")
        try:
            cells = table.extract()
            return "\n".join(
                " | ".join("" if c is None else str(c).strip() for c in row)
                for row in cells
            )
        except Exception as e2:
            logger.warning(f"_table_to_markdown: 表格提取完全失败: {e2}")
            return ""


def _page_size_mode(lines: list) -> float | None:
    """页内文本行字号众数,用于标题字号兜底;无行返回 None。"""
    sizes = [s for _t, s, _b in lines]
    if not sizes:
        return None
    return max(set(sizes), key=sizes.count)


def _find_repeat_lines(candidates_per_page: list[set[str]], page_count: int) -> set[str]:
    """跨页重复行(页眉页脚)检测:候选行在 ≥max(3, 70%页数) 页出现则判为重复。"""
    from collections import Counter
    counter: Counter = Counter()
    for cands in candidates_per_page:
        counter.update(cands)
    threshold = max(3, math.ceil(page_count * 0.7))
    return {line for line, count in counter.items() if count >= threshold}


def _get_pdf_document_layout(pdf) -> list[Document]:
    """布局感知解析:合流排序 → 过滤 → 标题栈 → 逐页组装 Document。"""
    t_pass1 = time.perf_counter()
    # 第一遍:收集每页 items 与页眉页脚候选行
    pages_items: list[list] = []
    candidates_per_page: list[set[str]] = []
    for page in pdf:
        items = _page_items(page)
        pages_items.append(items)
        cands: set[str] = set()
        lines = [p for _b, k, p in items if k == "line"]
        for text, max_size, bold in (lines[:2] + lines[-2:]):
            norm = _norm_line(text)
            if not norm or _is_page_number(text):
                continue
            if _heading_level(text, max_size, bold, None) is not None:
                continue  # 标题样行不参与重复统计
            cands.add(norm)
        candidates_per_page.append(cands)
    repeat_set = _find_repeat_lines(candidates_per_page, len(pages_items))
    logger.debug(f"get_pdf_document: 第一遍收集 {len(pages_items)} 页 items,耗时 {fmt_duration(time.perf_counter()-t_pass1)}")

    # 第二遍:逐页渲染,标题栈跨页维护章节链
    docs: list[Document] = []
    chapter = section = subsection = None
    t_render = time.perf_counter()
    for pno, items in enumerate(pages_items, 1):
        t_page = time.perf_counter()
        line_items = [p for _b, k, p in items if k == "line"]
        size_mode = _page_size_mode(line_items)

        parts: list[str] = []
        has_text = has_table = False
        img_idx = 0

        for _bbox, kind, payload in items:
            if kind == "line":
                text, max_size, bold = payload
                level = _heading_level(text, max_size, bold, size_mode)
                if level is not None:
                    if level == 1:
                        chapter = text
                    elif level == 2:
                        section = text
                    elif level == 3:
                        subsection = text
                    parts.append(text)
                    has_text = True
                    continue
                if _is_page_number(text):
                    continue
                if _norm_line(text) in repeat_set:
                    continue
                parts.append(text)
                has_text = True
            elif kind == "table":
                md = _table_to_markdown(payload)
                if md:
                    parts.append(md)
                    has_table = True
            elif kind == "img":
                img_idx += 1
                try:
                    img = pdf.extract_image(payload)
                    # 检查真实像素尺寸:装饰性极小图(如2x2) VLM 不受理,跳过描述
                    img_w = img.get("width", 0)
                    img_h = img.get("height", 0)
                    if img_w and img_h and (img_w < 10 or img_h < 10):
                        logger.debug(f"get_pdf_document: 第{pno}页图片 {img_idx} 像素 {img_w}x{img_h} 过小,跳过描述")
                        continue
                    t_desc = time.perf_counter()
                    desc = describe_image(img["image"], mime=img.get("ext", "png"))
                    logger.debug(f"get_pdf_document: 第{pno}页图片 {img_idx} 描述生成完成,耗时 {fmt_duration(time.perf_counter()-t_desc)}")
                except Exception as e:
                    logger.warning(f"get_pdf_document: 提取/描述第{pno}页图片失败: {e}")
                    continue
                parts.append(IMG_SEP.format(idx=img_idx, desc=desc))

        if not parts:
            logger.warning(f"get_pdf_document: 第{pno}页无有效内容,已跳过")
            continue

        logger.debug(f"get_pdf_document: 第{pno}页组装完成,耗时 {fmt_duration(time.perf_counter()-t_page)}")

        if has_table and has_text:
            content_type = "mixed"
        elif has_table:
            content_type = "table"
        else:
            content_type = "text"

        docs.append(Document(
            page_content="\n".join(parts),
            metadata={
                "source": os.path.basename(pdf.name),
                "file_type": "pdf",
                "page": pno,
                "is_image": img_idx > 0,
                "image_count": img_idx,
                "chapter": chapter,
                "section": section,
                "subsection": subsection,
                "content_type": content_type,
            },
        ))
    logger.debug(f"get_pdf_document: 第二遍逐页渲染完成,耗时 {fmt_duration(time.perf_counter()-t_render)}")
    return docs


def _get_pdf_document_legacy(pdf) -> list[Document]:
    """旧版简单提取路径(pdf_layout=false 或布局解析失败时使用):每页一条 Document。"""
    docs: list[Document] = []
    for pno, page in enumerate(pdf, 1):
        text = page.get_text("text").strip()
        pics: list[str] = []

        img_infos = sorted(
            page.get_image_info(xrefs=True),
            key=lambda r: (r["bbox"][1], r["bbox"][0]),
        )
        for idx, info in enumerate(img_infos, 1):
            xref = info.get("xref")
            if not xref:
                continue
            try:
                img = pdf.extract_image(xref)
            except Exception as e:
                logger.warning(f"get_pdf_document: 提取第{pno}页图片失败: {e}")
                continue
            desc = describe_image(img["image"], mime=img.get("ext", "png"))
            pics.append(IMG_SEP.format(idx=idx, desc=desc))

        if not text and not pics:
            logger.warning(f"get_pdf_document: 第{pno}页无文本,疑似整页扫描图,已跳过")
            continue

        docs.append(Document(
            page_content=text + "".join(pics),
            metadata={
                "source": os.path.basename(pdf.name),
                "file_type": "pdf",
                "page": pno,
                "is_image": bool(pics),
                "image_count": len(pics),
            },
        ))
    return docs


def _docx_paragraph_images(para, part) -> list[tuple[bytes, str]]:
    """返回段落里内嵌图片的 [(bytes, ext)]，ext 为 png/jpeg 等。"""
    images: list[tuple[bytes, str]] = []
    # python-docx 的 lxml 元素已注册 a 前缀命名空间，直接 XPath 取所有 a:blip 的 rId
    for embed_id in para._p.xpath(".//a:blip/@r:embed"):
        try:
            image_part = part.related_parts[embed_id]
            blob = image_part.blob
            if not blob:
                continue
            ctype = getattr(image_part, "content_type", "")  # image/png, image/jpeg ...
            ext = ctype.split("/")[-1] if "/" in ctype else "png"
            if ext not in ("png", "jpeg", "jpg", "gif", "bmp"):
                ext = "png"
            images.append((blob, ext))
        except Exception as e:
            logger.warning(f"get_docs_document: 提取段落图片失败: {e}")
    return images


def get_docs_document(file_path: str) -> list[Document] | bool:
    """读取 Word(.docx)，返回 Document 列表。

    按正文段落顺序，把每段文字与其内嵌图片的多模态描述合并，
    整篇作为一条 Document（外层再由 splitter 切块）。
    """
    t0 = time.perf_counter()
    try:
        d = docx.Document(file_path)
        part = d.part
        out_parts: list[str] = []
        img_count = 0

        for para in d.paragraphs:
            text = para.text.strip()
            for blob, ext in _docx_paragraph_images(para, part):
                img_count += 1
                logger.debug(f"get_docs_document: 插入图片 {img_count}，描述生成中...")
                t_desc = time.perf_counter()
                desc = describe_image(blob, mime=ext)
                logger.debug(f"get_docs_document: 插入图片 {img_count}，描述生成完成：{desc[:20]}..., 耗时 {fmt_duration(time.perf_counter()-t_desc)}")
                text += IMG_SEP.format(idx=img_count, desc=desc)
            if text:
                out_parts.append(text)

        if not out_parts:
            logger.info(f"get_docs_document: 解析完成 {os.path.basename(file_path)},耗时 {fmt_duration(time.perf_counter()-t0)},0 篇 Document(无内容)")
            return []

        result = [Document(
            page_content="\n".join(out_parts),
            metadata={
                "source": os.path.basename(file_path),
                "file_type": "docx",
                "is_image": img_count > 0,
                "image_count": img_count,
            },
        )]
        logger.info(f"get_docs_document: 解析完成 {os.path.basename(file_path)},耗时 {fmt_duration(time.perf_counter()-t0)},{len(result)} 篇 Document")
        return result
    except Exception as e:
        logger.error(f"get_docs_document: 读取文件 {file_path} 时出错：{e}")
        return False


# ---------- Excel / CSV 加载 ----------

def _norm_cell(v) -> str:
    """把单元格值规范成用于检索的文本：None/NaN 置空，整数值去掉 .0。"""
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return ""
        if v.is_integer():
            return str(int(v))
        return str(v)
    if isinstance(v, str):
        return v.strip()
    return str(v)


def _norm_row(cells) -> list[str]:
    """规范化一行：逐格清洗并保留空占位，保证列对齐。"""
    return [_norm_cell(c) for c in cells]


def _rows_to_text(sheet_name: str, header, rows, preamble: list[str] | None = None) -> str:
    """把标题/汇总说明（可选）+ 表头 + 数据行渲染为可按行检索的纯文本。"""
    lines = [f"工作表：{sheet_name}"]
    if preamble:
        lines.append("说明：")
        lines += [l for l in preamble if l]
    if header:
        lines.append(" | ".join(_norm_row(header)))
    for row in rows:
        lines.append(" | ".join(_norm_row(row)))
    return "\n".join(lines).strip()


def _preamble_lines(preamble_rows: list[list[str]]) -> list[str]:
    """把顶部标题/汇总行压成说明文本：每行取不同值合并为一条。

    合并独占行展开后全是同一值，去重后只留一条；避免输出 N 份相同值。
    """
    lines = []
    for row in preamble_rows:
        uniq = list(dict.fromkeys(c for c in row if c))
        if uniq:
            lines.append(" | ".join(uniq))
    return lines


def _find_header_row(rows: list[list[str]]) -> tuple[int, int]:
    """定位真正表头行，返回 (表头行号, 前导说明行数)。

    顶部那些“不同非空值种类 < 2”的整行（被合并单元格整体独占、或只有
    单个标题单元格）是对下方表格的说明，跳过；首个不同非空值种类 >= 2
    的行视为表头。找不到则回退第 0 行当表头。
    """
    for i, row in enumerate(rows):
        if len(dict.fromkeys(c for c in row if c)) >= 2:
            return i, i
    return 0, 0


def _table_to_text(sheet_name: str, rows: list[list[str]]) -> str:
    """统一渲染：自动定位表头行，表头之上的标题/汇总行作前导说明保留。"""
    if not rows:
        return _rows_to_text(sheet_name, [], [], None)
    hi, skip = _find_header_row(rows)
    preamble = _preamble_lines(rows[:skip])
    return _rows_to_text(sheet_name, rows[hi], rows[hi + 1:], preamble)


def _frame_to_text(sheet_name: str, df: pd.DataFrame) -> str:
    """DataFrame -> 纯文本：以 header=None 保留全部行，交由 _table_to_text 定位表头。"""
    rows = [[_norm_cell(v) for v in r] for r in df.itertuples(index=False, name=None)]
    return _table_to_text(sheet_name, rows)


def _sheet_has_text(rows) -> bool:
    """判断二维列表是否有任何非空单元格。"""
    for row in rows:
        if any(_norm_cell(c) for c in row):
            return True
    return False


def _merged_sheet_text(ws) -> list[str]:
    """含合并单元格的 sheet：按左上角值展开合并区域，返回数据行列表。

    遍历每一数据行：若某单元格落入合并区域且非左上角，则取左上角的值。
    """
    if not ws.max_row or not ws.max_column:
        return []

    # (合并区域, 左上角值)；B1:B10 这类垂直合并其左上角在 B1
    merged = [(rng, ws.cell(rng.min_row, rng.min_col).value) for rng in ws.merged_cells.ranges]

    def cell_value(row, col) -> str:
        for rng, top_left in merged:
            if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
                return _norm_cell(top_left)
        return _norm_cell(ws.cell(row, col).value)

    rows = []
    for row in range(1, ws.max_row + 1):
        values = [cell_value(row, col) for col in range(1, ws.max_column + 1)]
        # 跳过整行全空
        if _sheet_has_text([values]):
            rows.append(values)
    return rows


def get_csv_document(file_path: str) -> list[Document] | bool:
    """读取 CSV，返回 Document 列表。

    用 utf-8-sig 打开以兼容 Excel 导出的带 BOM 文件；首行为表头，
    整份文件作为一条 Document（后续由 splitter 切块）。
    """
    t0 = time.perf_counter()
    try:
        with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))

        header = [c.strip() for c in rows[0]] if rows else []
        data_rows = []
        for row in rows[1:]:
            cells = [c.strip() for c in row]
            if any(cells):
                data_rows.append(cells)

        result = [Document(
            page_content=_rows_to_text("", header, data_rows),
            metadata={
                "source": os.path.basename(file_path),
                "file_type": "csv",
            },
        )]
        logger.info(f"get_csv_document: 解析完成 {os.path.basename(file_path)},耗时 {fmt_duration(time.perf_counter()-t0)},{len(result)} 篇 Document")
        return result
    except Exception as e:
        logger.error(f"get_csv_document: 读取文件 {file_path} 时出错：{e}")
        return False


def get_excel_document(file_path: str) -> list[Document] | bool:
    """读取 Excel(.xlsx/.xls)，按 sheet 逐张处理，返回 Document 列表。

    常规（无合并单元格）sheet 用 pandas 读；含合并/复杂格式的 sheet 用
    openpyxl 直接遍历单元格（data_only 取计算值、丢弃全部样式），合并区域
    按左上角值展开。顶部被独占/单列的标题行不误作表头，作为说明保留在
    表头之前。空 sheet 跳过并打 warning。
    """
    t0 = time.perf_counter()
    try:
        ext = file_path.lower().rsplit(".", 1)[-1]
        file_type = ext if ext in ("xls", "xlsx", "xlsm") else "excel"
        docs: list[Document] = []

        if file_type == "xls":
            # openpyxl 读不了旧版 .xls，整本交给 pandas/xlrd；header=None 让标题行也保留
            sheets = pd.read_excel(file_path, sheet_name=None, engine="xlrd", header=None)
            for sheet_name, df in sheets.items():
                if df.empty:
                    logger.warning(f"get_excel_document: 工作表 {sheet_name} 为空，已跳过")
                    continue
                docs.append(Document(
                    page_content=_frame_to_text(sheet_name, df),
                    metadata={"source": os.path.basename(file_path), "file_type": file_type,
                              "sheet_name": sheet_name},
                ))
            logger.info(f"get_excel_document: 解析完成 {os.path.basename(file_path)},耗时 {fmt_duration(time.perf_counter()-t0)},{len(docs)} 篇 Document")
            return docs

        wb = openpyxl.load_workbook(file_path, data_only=True)
        merged_sheets = {ws.title for ws in wb.worksheets if ws.merged_cells.ranges}

        for ws in wb.worksheets:
            sheet_name = ws.title
            t_sheet = time.perf_counter()

            if sheet_name in merged_sheets:
                rows = _merged_sheet_text(ws)
                if not _sheet_has_text(rows):
                    logger.warning(f"get_excel_document: 工作表 {sheet_name} 为空，已跳过")
                    continue
                text = _table_to_text(sheet_name, rows)
            else:
                df = pd.read_excel(file_path, sheet_name=sheet_name, engine="openpyxl", header=None)
                if df.empty:
                    logger.warning(f"get_excel_document: 工作表 {sheet_name} 为空，已跳过")
                    continue
                text = _frame_to_text(sheet_name, df)

            logger.debug(f"get_excel_document: 工作表 {sheet_name} 解析完成,耗时 {fmt_duration(time.perf_counter()-t_sheet)}")
            docs.append(Document(
                page_content=text,
                metadata={
                    "source": os.path.basename(file_path),
                    "file_type": file_type,
                    "sheet_name": sheet_name,
                },
            ))

        wb.close()
        logger.info(f"get_excel_document: 解析完成 {os.path.basename(file_path)},耗时 {fmt_duration(time.perf_counter()-t0)},{len(docs)} 篇 Document")
        return docs
    except Exception as e:
        logger.error(f"get_excel_document: 读取文件 {file_path} 时出错：{e}")
        return False


if __name__ == '__main__':
    doc = get_markdown_document("C:\\Users\\Administrator\\Desktop\\Python-Project\\AI大模型RAG与智能体开发_Agent项目(2刷)\\data\\扫地机器人120问.md")
    # doc = get_markdown_document("C:\\Users\\Administrator\\Desktop\\Python-Project\\AI大模型RAG与智能体开发_Agent项目(2刷)\\data")
    # doc = get_pdf_document("D:\\1oveb\\CET4_202406_360021241119305_1.pdf")
    # doc = get_pdf_document("D:\\1oveb")
    # doc = get_docs_document("D:\\1oveb\\计科二班202326202064张海鹏.docx")
    # doc = get_docs_document("D:\\1oveb\\计科二班202326202064张海鹏")
    # doc = get_excel_document("D:\\WechatDocuments\\xwechat_files\\wxid_8pvqntf7ufpt12_6ce1\\msg\\file\\2026-06\\毛概实践活动分组与选题.xlsx")
    # doc = get_excel_document("D:\\WechatDocuments\\xwechat_files\\wxid_8pvqntf7ufpt12_6ce1\\msg\\file\\2026-06")
    # print(doc)

    for page in doc:
        print(page.page_content)
