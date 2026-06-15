"""
钉钉 Stream 模式 —— 接收单聊消息 + 回复。
通过 WebSocket 长连接接收钉钉推送，无需公网 URL。
"""

import json
import os
from pathlib import Path

from dingtalk_stream import (
    AckMessage,
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


def _auto_fill_engineer_id(sender_nick: str, sender_id: str):
    """
    自动回填工程师的钉钉 UserID。
    如果发送者昵称匹配 engineers.json 中某位工程师，
    且该工程师的 dingtalk_user_id 为空，则自动填入。
    """
    if not sender_id or not sender_nick:
        return

    if not ENGINEERS_PATH.exists():
        return

    try:
        with open(ENGINEERS_PATH, "r", encoding="utf-8") as f:
            engineers = json.load(f)
    except Exception:
        return

    updated = False
    for e in engineers:
        if e.get("name") == sender_nick and not e.get("dingtalk_user_id"):
            e["dingtalk_user_id"] = sender_id
            updated = True
            print(f"[钉钉] 🔗 自动绑定：{sender_nick} → dingtalk_user_id={sender_id}")

    if updated:
        try:
            with open(ENGINEERS_PATH, "w", encoding="utf-8") as f:
                json.dump(engineers, f, ensure_ascii=False, indent=2)
            print(f"[钉钉] ✅ 已保存 engineers.json")
        except Exception as ex:
            print(f"[钉钉] ❌ 保存 engineers.json 失败：{ex}")


class OpsAgentChatbot(AsyncChatbotHandler):
    """
    运维 Agent 聊天机器人处理器。
    继承 AsyncChatbotHandler，重写 process 方法（同步）。
    SDK 会在后台线程池中执行 process，适合 LLM 长耗时操作。
    AsyncChatbotHandler.raw_process 会在线程池中调用 self.process()，
    并立即向钉钉返回 ACK_OK，不会阻塞消息确认。
    """

    def __init__(self, agent_app):
        super().__init__()
        self.agent_app = agent_app

    def process(self, callback_message):
        """
        SDK 在后台线程中调用。
        参数类型是 CallbackMessage，需要用 ChatbotMessage.from_dict 解析消息内容。
        """
        # 1. 从 CallbackMessage.data 中解析出 ChatbotMessage
        incoming_message = ChatbotMessage.from_dict(callback_message.data)

        sender_nick = incoming_message.sender_nick or "用户"
        conversation_type = incoming_message.conversation_type
        print(f"[钉钉 DEBUG] process 被调用！发送者: {sender_nick}, 会话类型: {conversation_type}")

        # 自动回填工程师的钉钉 UserID（仅在首次匹配时写入）
        sender_id = getattr(incoming_message, "sender_id", "")
        sender_staff_id = getattr(incoming_message, "sender_staff_id", "")
        print(f"[钉钉] 🆔 sender_id={sender_id}, sender_staff_id={sender_staff_id}")
        # 私聊 API 需要 staff_id，优先用它；若无则回退到 sender_id
        bind_id = sender_staff_id or sender_id
        _auto_fill_engineer_id(sender_nick, bind_id)


        # 2. 提取文本
        text_list = incoming_message.get_text_list()
        if not text_list:
            print(f"[钉钉] 非文本消息（来自 {sender_nick}），已忽略")
            return

        question = "\n".join([t.strip() for t in text_list if t.strip()])
        if not question:
            return

        print(f"[钉钉] 收到 {sender_nick} 的提问：{question[:80]}...")

        # 3. 构造 Agent 输入
        from .models import AgentState, Task

        state = AgentState(
            task=Task(
                title=question[:80],
                description=question,
                submitted_by=sender_nick,
            ),
            difficulty=None,
            knowledge_context="",
            final_response="",
            assigned_engineer="",
        )

        # 4. 运行 Agent
        difficulty = "?"
        try:
            result = self.agent_app.invoke(state)
            reply = result["final_response"]
            difficulty = result.get("difficulty", "?")
        except Exception as e:
            print(f"[钉钉] Agent 执行失败：{e}")
            reply = "处理出错，请联系 IT 工程师。"

        # 5. 使用 SDK 内置方法回复用户
        try:
            self.reply_markdown("运维助手", reply, incoming_message)
            print(f"[钉钉] 已回复 {sender_nick}（难度：{difficulty}）")
        except Exception as e:
            print(f"[钉钉] 回复发送失败：{e}")


def start_stream_bot(agent_app):
    """
    启动钉钉 Stream 长连接，阻塞运行。
    与 FastAPI 在不同线程中运行。
    """
    print(f"[钉钉 Stream] CLIENT_ID={CLIENT_ID[:6]}***" if CLIENT_ID else "[钉钉 Stream] CLIENT_ID=(空)")
    print(f"[钉钉 Stream] CLIENT_SECRET={'已配置' if CLIENT_SECRET else '(空)'}")

    if not CLIENT_ID or not CLIENT_SECRET:
        print("[钉钉 Stream] 未配置 CLIENT_ID / CLIENT_SECRET，跳过启动。")
        print("[钉钉 Stream] 请在 .env 文件中添加以下配置：")
        print("[钉钉 Stream]   DINGTALK_CLIENT_ID=你的AppKey")
        print("[钉钉 Stream]   DINGTALK_CLIENT_SECRET=你的AppSecret")
        return

    credential = Credential(CLIENT_ID, CLIENT_SECRET)
    client = DingTalkStreamClient(credential)
    handler = OpsAgentChatbot(agent_app)

    # 使用 SDK 内置的 ChatbotMessage.TOPIC 常量注册，确保 topic 精确匹配
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)

    print(f"[钉钉 Stream] 正在连接（topic: {ChatbotMessage.TOPIC}）...")
    try:
        client.start_forever()
    except Exception as e:
        print(f"[钉钉 Stream] 连接异常：{e}")
        raise
