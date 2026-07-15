"""
钉钉 Stream 模式 -- 纯转发层（新架构）。

新架构：钉钉 Stream 只负责收发消息，不处理业务逻辑。
收到消息后转发给 API /api/v1/message，收到响应后回复用户。
所有业务逻辑（预处理/AI/后处理）集中在 API 层。
"""

import os

import requests
from dingtalk_stream import (
    AsyncChatbotHandler,
    ChatbotMessage,
    Credential,
    DingTalkStreamClient,
)
from dotenv import load_dotenv
from starlette.concurrency import run_in_threadpool

load_dotenv()

CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET", "")

# API 转发地址（本地 FastAPI）
API_MESSAGE_URL = os.getenv("API_MESSAGE_URL", "http://localhost:8000/api/v1/message")
# 内部服务调用用的 API Key（与 config.API_KEY_SERVICE 一致）
API_KEY_SERVICE = os.getenv("API_KEY_SERVICE", "")


def _auto_bind_engineer(sender_nick: str, sender_staff_id: str, sender_user_id: str):
    """
    自动匹配并回填工程师身份（工号 + 钉钉 userId）。
    匹配决策委托给 engineer_matcher，本函数只负责调用与日志，不承载业务规则。
    """
    if not sender_nick:
        return
    try:
        from . import engineer_matcher

        result = engineer_matcher.match_and_bind(
            sender_nick=sender_nick,
            sender_staff_id=sender_staff_id,
            sender_user_id=sender_user_id,
        )
        if not result.matched:
            print(f"[钉钉] ⚠️ 工程师身份未绑定：{sender_nick}（{result.reason}）")
    except Exception as e:
        print(f"[钉钉] ⚠️ 工程师身份匹配失败：{e}")


class OpsAgentChatbot(AsyncChatbotHandler):
    """
    运维 Agent 聊天机器人处理器（纯转发层）。
    收到消息后转发给 API，收到响应后回复用户。
    """

    async def process(self, callback_message):
        """收到钉钉消息 -> 转发给 API -> 回复用户"""
        incoming_message = ChatbotMessage.from_dict(callback_message.data)

        sender_nick = incoming_message.sender_nick or "用户"
        print(f"[钉钉] 收到消息：{sender_nick}")

        # 自动匹配并回填工程师身份（工号 / 钉钉 userId）
        sender_id = getattr(incoming_message, "sender_id", "")
        sender_staff_id = getattr(incoming_message, "sender_staff_id", "")
        _auto_bind_engineer(sender_nick, sender_staff_id, sender_id)

        # 业务通道用的稳定用户标识（工号优先，回退 userId；与原行为一致）
        bind_id = sender_staff_id or sender_id

        # 提取文本
        text_list = incoming_message.get_text_list()
        if not text_list:
            print(f"[钉钉] 非文本消息（来自 {sender_nick}），已忽略")
            return

        question = "\n".join([t.strip() for t in text_list if t.strip()])
        if not question:
            return

        print(f"[钉钉] 转发给 API：{question[:80]}...")

        # ★ 转发给 API（纯转发，不处理业务）
        # 用线程池包装同步 requests.post，避免阻塞钉钉事件循环
        try:
            headers = {"X-API-Key": API_KEY_SERVICE} if API_KEY_SERVICE else {}
            resp = await run_in_threadpool(
                lambda: requests.post(
                    API_MESSAGE_URL,
                    json={
                        "source": "dingtalk",
                        "sender_id": bind_id,
                        "sender_name": sender_nick,
                        "content": question,
                        "metadata": {"staff_id": sender_staff_id},
                    },
                    headers=headers,
                    timeout=120,  # LLM 可能需要较长时间
                )
            )
            result = resp.json()
            reply = result.get("response", "处理出错，请重试。")
        except requests.exceptions.Timeout:
            print(f"[钉钉] API 转发超时")
            reply = "处理超时，请稍后重试，或联系 IT 工程师。"
        except Exception as e:
            print(f"[钉钉] API 转发失败：{e}")
            reply = "处理出错，请联系 IT 工程师。"

        # 回复用户
        try:
            self.reply_markdown("运维助手", reply, incoming_message)
            print(f"[钉钉] 已回复 {sender_nick}")
        except Exception as e:
            print(f"[钉钉] 回复发送失败：{e}")


def start_stream_bot():
    """启动钉钉 Stream 长连接，阻塞运行。"""
    print(
        f"[钉钉 Stream] CLIENT_ID={CLIENT_ID[:6]}***"
        if CLIENT_ID
        else "[钉钉 Stream] CLIENT_ID=(空)"
    )
    print(f"[钉钉 Stream] CLIENT_SECRET={'已配置' if CLIENT_SECRET else '(空)'}")

    if not CLIENT_ID or not CLIENT_SECRET:
        print("[钉钉 Stream] 未配置 CLIENT_ID / CLIENT_SECRET，跳过启动。")
        return

    credential = Credential(CLIENT_ID, CLIENT_SECRET)
    client = DingTalkStreamClient(credential)
    handler = OpsAgentChatbot()

    client.register_callback_handler(ChatbotMessage.TOPIC, handler)

    print(f"[钉钉 Stream] 正在连接（topic: {ChatbotMessage.TOPIC}）...")
    try:
        client.start_forever()
    except Exception as e:
        print(f"[钉钉 Stream] 连接异常：{e}")
        raise
