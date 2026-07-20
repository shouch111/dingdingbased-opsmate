"""
日志配置 -- 集中管理全项目日志。

功能：
1. 统一日志格式（时间 | 级别 | 模块 | req=请求ID | 消息）
2. 双输出（控制台 + 文件轮转）
3. request_id 请求级关联（contextvars，线程池安全）

使用方式：
- 启动时调用 setup_logging() 一次
- 各模块顶部：logger = logging.getLogger(__name__)
- 请求入口：set_request_id(gen_request_id())
- 日志过滤器自动注入 request_id，无需手动传参
"""

import contextvars
import logging
import logging.handlers
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

# -------------------- 环境变量加载 --------------------

DATA_DIR = Path(__file__).parent.parent
_env_paths = [DATA_DIR / ".env", DATA_DIR.parent / ".env"]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        break

# -------------------- 请求级上下文 --------------------

# contextvars 保证线程池并发隔离（run_in_threadpool 自动传播）
_request_id_ctx = contextvars.ContextVar("request_id", default="-")


def set_request_id(request_id: str):
    """设置当前请求的 request_id（在请求入口调用）"""
    _request_id_ctx.set(request_id)


def get_request_id() -> str:
    """读取当前请求的 request_id（供日志过滤器调用）"""
    return _request_id_ctx.get()


def gen_request_id() -> str:
    """生成短请求 ID（8 位十六进制，可读且足够区分）"""
    return uuid.uuid4().hex[:8]


# -------------------- 日志过滤器 --------------------


class RequestIdFilter(logging.Filter):
    """将 contextvars 中的 request_id 注入每条日志记录"""

    def filter(self, record):
        record.request_id = get_request_id()
        return True


# -------------------- 日志格式 --------------------


class OpsAgentFormatter(logging.Formatter):
    """自定义格式：时间 | 级别 | 模块 | req=ID | 消息"""

    def format(self, record):
        # 补全 request_id（filter 可能未触发的情况兜底）
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return super().format(record)


def setup_logging():
    """
    初始化全局日志配置。启动时调用一次。

    - 控制台输出（始终启用）
    - 文件轮转输出（配置 LOG_FILE 时启用）
    - 级别由 LOG_LEVEL 控制（默认 INFO）
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_file = os.getenv("LOG_FILE", "")
    max_size = int(os.getenv("LOG_MAX_SIZE", "10")) * 1024 * 1024  # MB -> bytes
    backup_count = int(os.getenv("LOG_BACKUP_COUNT", "5"))

    # 日志格式
    fmt = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-22s | req=%(request_id)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = OpsAgentFormatter(fmt=fmt, datefmt=datefmt)

    # 根 logger 配置
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 清除已有 handler（避免重复添加，如 uvicorn 已配置）
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(RequestIdFilter())
    root_logger.addHandler(console_handler)

    # 文件 handler（轮转）
    if log_file:
        log_path = Path(log_file)
        # 相对路径相对于项目根目录解析（DATA_DIR 是 data/，父目录是项目根）
        if not log_path.is_absolute():
            log_path = DATA_DIR.parent / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_size,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(RequestIdFilter())
        root_logger.addHandler(file_handler)
        print(f"[log_config] 日志文件：{log_path.resolve()}")

    # 降低第三方库噪声
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger(" DingTalkStreamClient").setLevel(logging.WARNING)

    root_logger.info(
        "日志系统初始化完成 级别=%s 文件=%s",
        level_name,
        log_file or "无(仅控制台)",
    )
