"""
日志工具模块：统一日志接口，实现「控制台 + 文件」双输出。

其他文件用法：
    from utils.logger_tool import logger
    logger.info("...")
    logger.error("...")

要求说明：
1. logger.info / logger.error 同时输出到控制台与日志文件；
2. 日志文件按天切分，每天一份，文件名形如 agent_2026-09-02.log；
3. 单条日志格式：
   日期时间 - logger名称(默认agent) - 日志级别 - 来源文件:行号 - 日志信息。
"""

import logging
import os
from datetime import datetime

# 日志目录：项目根目录 /logs
LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)

# 单条日志格式（%(filename)s:%(lineno)d 自动取自 logger 实际调用处）
LOG_FORMAT = (
    "%(asctime)s - %(name)s - %(levelname)s - "
    "%(filename)s:%(lineno)d - %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class DailyFileHandler(logging.Handler):
    """按天切分日志文件的处理器：每天一份，文件名形如 agent_2026-09-02.log。"""

    def __init__(self, log_dir: str = LOG_DIR, name: str = "agent", encoding: str = "utf-8"):
        super().__init__()
        self.log_dir = log_dir
        self.name = name
        self.encoding = encoding
        self._current_date: tuple[int, int, int] | None = None
        self._stream = None

    def _today_path(self, now: datetime) -> str:
        # 手动拼接避免 strftime 补零，得到 agent_2026-09-02.log 这类文件名
        return os.path.join(
            self.log_dir, f"{self.name}_{now.year:04d}-{now.month:02d}-{now.day:02d}.log"
        )

    def _open(self, now: datetime) -> None:
        if self._stream is not None:
            self._stream.close()
        os.makedirs(self.log_dir, exist_ok=True)
        self._stream = open(self._today_path(now), "a", encoding=self.encoding)
        self._current_date = (now.year, now.month, now.day)

    def emit(self, record: logging.LogRecord) -> None:
        # 跨天时切换到新日期文件
        now = datetime.now()
        if (now.year, now.month, now.day) != self._current_date:
            self._open(now)
        try:
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        super().close()


def get_logger(
    name: str = "agent",
    *,
    console: bool = True,
    level: int = logging.DEBUG,
) -> logging.Logger:
    """获取配置好的 logger（控制台 + 按天文件双输出）。

    Args:
        name: logger 名称，默认 agent
        console: 是否输出到控制台
        level: 日志级别

    Returns:
        logging.Logger 对象
    """
    log = logging.getLogger(name)
    if log.handlers:  # 已初始化过则直接复用，避免重复添加 handler
        return log

    log.setLevel(level)
    log.propagate = False  # 避免向上冒泡导致重复输出

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    file_handler = DailyFileHandler(LOG_DIR, name=name)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    log.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        log.addHandler(console_handler)

    return log


# 模块默认 logger（名称：agent）
logger = get_logger()


def fmt_duration(seconds: float) -> str:
    """把秒数格式化为人类可读时长:60s 内按秒,超则进位到分/时/天。"""
    if seconds < 60:
        return f"{seconds:.2f}秒"
    if seconds < 3600:
        return f"{seconds / 60:.2f}分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.2f}小时"
    return f"{seconds / 86400:.2f}天"


if __name__ == "__main__":

    logger.info("logger 初始化成功，这是一条 INFO 日志")
    logger.warning("这是一条 WARNING 日志")
    logger.error("这是一条 ERROR 日志")
    print(fmt_duration(37.1))
    print(fmt_duration(90))
    print(fmt_duration(7200))
    print(fmt_duration(200000))
