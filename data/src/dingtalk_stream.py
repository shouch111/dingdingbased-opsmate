"""
钉钉 Stream 模式 -- 纯转发层（新架构）。

新架构：钉钉 Stream 只负责收发消息，不处理业务逻辑。
收到消息后转发给 API /api/v1/message，收到响应后回复用户。
所有业务逻辑（预处理/AI/后处理）集中在 API 层。
"""

import json
import os
from pathlib import Path

import requests
from dingtalk_stream import (
    AsyncChatbotHandler,
    ChatbotMessage,
    Credential,
    DingTalkStreamClient,
)
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET", "")
ENGINEERS_PATH = Path(__file__).parent.parent / "engineers.json"

# API 转发地址（本地 FastAPI）
API_MESSAGE_URL = "http://localhost:8000/api/v1/message"


def _auto_fill_engineer_id(sender_nick: str, sender_id: str):
    """自动回填工程师的钉钉 UserID（优先写 DB，JSON 降级）。"""
    if not sender_id or not sender_nick:
        return

    try:
        from . import db_manager

        if db_manager.save_engineer_dingtalk_id(sender_nick, sender_id):
            print(f"[钉钉] 🔗 自动绑定：{sender_nick} -> dingtalk_user_id={sender_id}")
            return
        return
    except Exception as e:
        print(f"[钉钉] ⚠️ DB 绑定失败，降级写 JSON：{e}")

    if not ENGINEERS_PATH.exists():
        return
    try:
        with open(ENGINEERS_PATH, "r", encoding="utf-8") as f:
            engineers = json.load(f)
        updated = False
        for e in engineers:
            if e.get("name") == sender_nick and not e.get("dingtalk_user_id"):
                e["dingtalk_user_id"] = sender_id
                updated = True
                print(f"[钉钉] 🔗 自动绑定(JSON)：{sender_nick} -> {sender_id}")
        if updated:
            with open(ENGINEERS_PATH, "w", encoding="utf-8") as f:
                json.dump(engineers, f, ensure_ascii=False, indent=2)
    except Exception as ex:
        print(f"[钉钉] ❌ JSON 绑定也失败：{ex}")


class OpsAgentChatbot(AsyncChatbotHandler):
    """
    运维 Agent 聊天机器人处理器（纯转发层）。
    收到消息后转发给 API，收到响应后回复用户。
    """

    def process(self, callback_message):
        """收到钉钉消息 -> 转发给 API -> 回复用户"""
        incoming_message = ChatbotMessage.from_dict(callback_message.data)

        sender_nick = incoming_message.sender_nick or "用户"
        print(f"[钉钉] 收到消息：{sender_nick}")

        # 自动回填工程师的钉钉 UserID
        sender_id = getattr(incoming_message, "sender_id", "")
        sender_staff_id = getattr(incoming_message, "sender_staff_id", "")
        bind_id = sender_staff_id or sender_id
        _auto_fill_engineer_id(sender_nick, bind_id)

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
        try:
            resp = requests.post(
                API_MESSAGE_URL,
                json={
                    "source": "dingtalk",
                    "sender_id": bind_id,
                    "sender_name": sender_nick,
                    "content": question,
                    "metadata": {"staff_id": sender_staff_id},
                },
                timeout=120,  # LLM 可能需要较长时间
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
