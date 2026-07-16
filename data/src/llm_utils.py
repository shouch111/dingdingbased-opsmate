"""
LLM 调用工具 -- 统一重试 + 熔断保护。

所有 LLM 调用通过 safe_llm_invoke() 入口，获得：
1. 指数退避重试（仅对瞬时故障：超时/429/5xx/连接错误）
2. 熔断保护（连续失败超阈值时短路，避免对已宕机服务持续重试）
3. 结构化日志（每次重试/熔断均记录）

降级逻辑由各调用方的 except 块处理（已有），本模块只负责"尽力调用"。
"""

import logging
import time
from typing import Any

from .config import (
    LLM_CIRCUIT_FAILURE_THRESHOLD,
    LLM_CIRCUIT_RECOVERY_SECONDS,
    LLM_RETRY_MAX_ATTEMPTS,
    LLM_RETRY_MAX_WAIT,
    LLM_RETRY_MIN_WAIT,
)

logger = logging.getLogger(__name__)


# ==================== 可重试异常判断 ====================


def _should_retry(exc: BaseException) -> bool:
    """
    判断异常是否值得重试（仅瞬时故障）。

    可重试：超时、连接错误、429 限流、5xx 服务端错误
    不重试：400/401/403（请求本身有问题）、ValueError（参数错误）
    """
    # httpx/openai 的超时异常
    exc_name = type(exc).__name__
    if "Timeout" in exc_name or "ConnectError" in exc_name:
        return True
    if "ConnectionError" in exc_name:
        return True

    # openai APIStatusError：检查状态码
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        if status_code == 429:
            return True
        if 500 <= status_code < 600:
            return True
        return False

    # httpx.HTTPStatusError：检查 response.status_code
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if code is not None:
            if code == 429:
                return True
            if 500 <= code < 600:
                return True
            return False

    # 通用：APIConnectionError / APITimeoutError（openai 库）
    if "APIConnection" in exc_name or "APITimeout" in exc_name:
        return True

    return False


# ==================== 熔断器 ====================


class CircuitBreakerOpenError(Exception):
    """熔断器开启时抛出，调用方应走降级逻辑"""

    pass


class CircuitBreaker:
    """
    简单熔断器（closed -> open -> half_open -> closed）。

    - closed: 正常调用，记录失败计数
    - open: 连续失败达阈值，直接拒绝调用（抛 CircuitBreakerOpenError）
    - half_open: 熔断恢复后放行 1 次探测，成功则恢复，失败则重新熔断
    """

    def __init__(self, failure_threshold: int, recovery_seconds: int):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._failure_count = 0
        self._state = "closed"  # closed / open / half_open
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        return self._state

    def _update_state(self):
        """检查是否应从 open 转为 half_open"""
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.recovery_seconds:
                self._state = "half_open"
                logger.info("熔断器进入半开状态，放行探测请求")

    def allow(self) -> bool:
        """是否允许调用"""
        self._update_state()
        return self._state != "open"

    def record_success(self):
        """记录成功：重置计数，恢复 closed"""
        if self._state in ("open", "half_open"):
            logger.info("熔断器恢复（closed）")
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self):
        """记录失败：累加计数，可能触发熔断"""
        self._failure_count += 1
        if self._state == "half_open":
            # 半开探测失败 -> 重新熔断
            self._trip()
            return
        if self._failure_count >= self.failure_threshold:
            self._trip()

    def _trip(self):
        """触发熔断"""
        self._state = "open"
        self._opened_at = time.monotonic()
        logger.warning(
            "熔断器触发（open），连续失败 %d 次，%ds 后尝试恢复",
            self._failure_count,
            self.recovery_seconds,
        )


# 全局熔断器实例（全项目共用一个 LLM 服务）
_circuit_breaker = CircuitBreaker(
    failure_threshold=LLM_CIRCUIT_FAILURE_THRESHOLD,
    recovery_seconds=LLM_CIRCUIT_RECOVERY_SECONDS,
)


# ==================== 统一 LLM 调用入口 ====================


def safe_llm_invoke(llm: Any, messages: list) -> Any:
    """
    统一 LLM 调用入口：熔断检查 -> 指数退避重试 -> 返回响应。

    参数:
        llm: ChatOpenAI 实例（或绑定了工具的 Runnable）
        messages: 消息列表
    返回:
        LLM 响应对象
    异常:
        CircuitBreakerOpenError: 熔断器开启时
        原始异常: 重试耗尽后抛出最后一个异常
    """
    # ① 熔断检查
    if not _circuit_breaker.allow():
        logger.warning("熔断器开启，跳过 LLM 调用")
        raise CircuitBreakerOpenError("熔断器开启，LLM 服务暂不可用")

    # ② 重试调用
    last_exc = None
    for attempt in range(1, LLM_RETRY_MAX_ATTEMPTS + 1):
        try:
            response = llm.invoke(messages)
            _circuit_breaker.record_success()
            return response

        except CircuitBreakerOpenError:
            raise

        except Exception as exc:
            last_exc = exc

            if not _should_retry(exc):
                # 不可重试异常（400/401/参数错误等），直接失败
                _circuit_breaker.record_failure()
                logger.error("LLM 调用失败（不可重试）：%s", exc)
                raise

            if attempt < LLM_RETRY_MAX_ATTEMPTS:
                wait = min(
                    LLM_RETRY_MIN_WAIT * (2 ** (attempt - 1)),
                    LLM_RETRY_MAX_WAIT,
                )
                logger.warning(
                    "LLM 调用失败（第 %d/%d 次），%ds 后重试：%s",
                    attempt,
                    LLM_RETRY_MAX_ATTEMPTS,
                    wait,
                    exc,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "LLM 调用失败，重试耗尽（共 %d 次）：%s",
                    LLM_RETRY_MAX_ATTEMPTS,
                    exc,
                )

    # 重试耗尽
    _circuit_breaker.record_failure()
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("safe_llm_invoke: 重试耗尽但无异常记录")
