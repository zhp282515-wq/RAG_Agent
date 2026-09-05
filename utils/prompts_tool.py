
from utils.logger_tool import logger
import re

def _clean_light_markdown(text: str) -> str:
    """轻量剥离 md 行内标记,保留标题层级与文字(供读 prompt 用)。"""
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)   # 图片保留alt
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)     # 链接保留文字
    text = re.sub(r"<[^>]+>", "", text)                       # HTML标签
    text = re.sub(r"`([^`]*)`", r"\1", text)                  # 行内代码保留内容
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)          # 加粗
    text = re.sub(r"__([^_\n]+)__", r"\1", text)
    text = re.sub(r"~~([^~\n]+)~~", r"\1", text)
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    text = re.sub(r"_([^_\n]+)_", r"\1", text)
    return text.strip()

def get_txt_prompt(prompt_path: str | None) -> str | bool:

    try:
        with open(prompt_path, "r", encoding="utf-8") as f:

            prompt = f.read()

            if not prompt:
                logger.warning("txt提示词文件为空")
                return prompt

            return prompt
    except Exception as e:
        logger.error(f"get_txt_prompt: 读取 txt 提示词文件失败：{e}")
        return False


def get_markdown_prompt(prompt_path: str) -> str | bool:
    """读取 md 提示词文件,返回整篇纯文本(保持原样整篇,不按标题切)。"""
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            text = f.read()
        if not text or not text.strip():
            logger.warning("get_markdown_prompt: md 提示词文件为空")
            return text.strip()
        return _clean_light_markdown(text)
    except Exception as e:
        logger.error(f"get_markdown_prompt: 读取 md 提示词文件失败: {e}")
        return False

def get_prompt(prompt_path: str | None) -> str | bool:
    if prompt_path.endswith(".md"):
        return get_markdown_prompt(prompt_path)
    elif prompt_path.endswith(".txt"):
        return get_txt_prompt(prompt_path)
    else:
        logger.error("get_prompt: 提示词文件格式错误")
        return False

if __name__ == '__main__':
    # md = get_txt_prompt("C:\\Users\\Administrator\\Desktop\\Python-Project\\RAG_Agent\\prompts\\rag_prompt.txt")
    md = get_markdown_prompt("C:\\Users\\Administrator\\Desktop\\Python-Project\\RAG_Agent\\prompts\\system_prompt.md")
    # md = get_markdown_prompt("C:\\Users\\Administrator\\Desktop\\Python-Project\\AI大模型RAG与智能体开发_Agent项目(2刷)\\data\\扫地机器人100问.pdf")
    print(md)