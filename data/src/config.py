"""
模型路由配置 -- 按复杂度选择不同 AI 模型。

新架构核心：预处理层检测复杂度 -> config 路由模型 -> AI 处理层调用。
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# -------------------- 环境变量加载 --------------------

DATA_DIR = Path(__file__).parent.parent
_env_paths = [
    DATA_DIR / ".env",
    DATA_DIR.parent / ".env",
]
for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        logger.info("已加载环境变量：%s", _p)
        break
else:
    load_dotenv()

# -------------------- LLM 基础配置 --------------------

LLM_API_KEY = os.getenv("open_code_go_api", "")
LLM_BASE_URL = os.getenv("base_url", "https://api.deepseek.com")

if not LLM_API_KEY:
    logger.warning("LLM_API_KEY 未配置（open_code_go_api），LLM 相关功能将不可用")

# -------------------- 模型路由 --------------------
# 按复杂度选择模型：simple/medium/hard -> 不同模型

MODEL_SIMPLE = os.getenv("MODEL_SIMPLE", "deepseek-chat")
MODEL_MEDIUM = os.getenv("MODEL_MEDIUM", "deepseek-chat")
MODEL_HARD = os.getenv("MODEL_HARD", "deepseek-reasoner")

MODEL_ROUTING = {
    "simple": {
        "model": MODEL_SIMPLE,
        "temperature": 0,
        "max_tokens": 1000,
        "tools_enabled": False,  # 简单问题不给工具，直接回答
        "description": "标准桌面问题，快速回答",
    },
    "medium": {
        "model": MODEL_MEDIUM,
        "temperature": 0,
        "max_tokens": 2000,
        "tools_enabled": True,
        "description": "需要工具辅助排查",
    },
    "hard": {
        "model": MODEL_HARD,
        "temperature": 0,
        "max_tokens": 4000,
        "tools_enabled": True,
        "description": "复杂问题，需要深度推理",
    },
}

# -------------------- 预处理配置 --------------------

# 意图检测：规则未命中时是否走 LLM 兜底
INTENT_LLM_FALLBACK = os.getenv("INTENT_LLM_FALLBACK", "true").lower() == "true"

# -------------------- 记忆配置 --------------------

MEMORY_ENABLED = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
MEMORY_SEARCH_TOP_K = int(os.getenv("MEMORY_SEARCH_TOP_K", "3"))

# -------------------- 工具调用限制 --------------------

# AI 工具调用最大轮次（防止死循环）
MAX_TOOL_ROUNDS = 3

# -------------------- LLM 超时配置 --------------------
# 单次 LLM 调用超时（秒），超时抛 Timeout 异常由调用方捕获
# 预处理/摘要等轻量调用默认较短，主流程可适当延长
LLM_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))
# hard 问题（deepseek-reasoner）推理慢，单独放宽
LLM_REQUEST_TIMEOUT_HARD = float(os.getenv("LLM_REQUEST_TIMEOUT_HARD", "120"))

# -------------------- LLM 重试/熔断配置 --------------------
LLM_RETRY_MAX_ATTEMPTS = int(os.getenv("LLM_RETRY_MAX_ATTEMPTS", "3"))
LLM_RETRY_MIN_WAIT = float(os.getenv("LLM_RETRY_MIN_WAIT", "1"))
LLM_RETRY_MAX_WAIT = float(os.getenv("LLM_RETRY_MAX_WAIT", "8"))
LLM_CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("LLM_CIRCUIT_FAILURE_THRESHOLD", "5"))
LLM_CIRCUIT_RECOVERY_SECONDS = int(os.getenv("LLM_CIRCUIT_RECOVERY_SECONDS", "60"))

# -------------------- API 鉴权配置 --------------------
# 角色层级（集合包含，admin 拥有 service 全部权限；readonly 独立只读）
# service : 仅可调用 POST /api/v1/message（内部服务/机器人）
# readonly: 仅可调用 GET 管理接口（查询工单/工程师/记忆）
# admin    : 全部接口
API_ROLES = {
    "service": {"/api/v1/message", "/task"},
    "readonly": {"/tasks", "/engineers", "/memories"},
    "admin": {"*"},  # 通配，全部放行
}

# 各角色对应的 API Key（存 .env，留空则该角色禁用）
API_KEY_SERVICE = os.getenv("API_KEY_SERVICE", "")
API_KEY_READONLY = os.getenv("API_KEY_READONLY", "")
API_KEY_ADMIN = os.getenv("API_KEY_ADMIN", "")

# 角色与 Key 的映射（启动时构建，Key 为空的角色不启用）
ROLE_KEYS = {}
if API_KEY_ADMIN:
    ROLE_KEYS["admin"] = API_KEY_ADMIN
if API_KEY_SERVICE:
    ROLE_KEYS["service"] = API_KEY_SERVICE
if API_KEY_READONLY:
    ROLE_KEYS["readonly"] = API_KEY_READONLY

# 是否打印鉴权配置摘要（启动时）
_PRINT_AUTH_SUMMARY = os.getenv("AUTH_PRINT_SUMMARY", "true").lower() == "true"
if _PRINT_AUTH_SUMMARY:
    _enabled = ", ".join(ROLE_KEYS.keys()) if ROLE_KEYS else "(无，鉴权未生效)"
    logger.info("API 鉴权已启用角色：%s", _enabled)
