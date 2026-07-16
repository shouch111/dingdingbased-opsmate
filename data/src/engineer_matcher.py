"""
工程师身份匹配层（独立模块）。

职责：根据发送者的身份字段（昵称、工号、钉钉 userId），在 engineers 表中
唯一确定对应工程师，并绑定 dingtalk_user_id。

绑定原则（按工号绑定）：
- dingtalk_user_id 仅在「工号确立」后写入，不以姓名/手机号单独绑定。
- 工号来源：钉钉消息回调自带的 sender_staffId（无需对接通讯录 API）。
- 名单无需预填工号：首次发消息时，用姓名/手机号定位到「未登记工号」的工程师，
  回填 staff_id 后即完成绑定；此后每条消息走工号直连，彻底不依赖姓名。

解耦原则：
- 本模块只接收纯字符串参数，不 import 任何消息来源 SDK（钉钉/企微/...），
  将来接入其他 IM 平台可直接复用。
- 数据访问统一通过 db_manager 的原子函数，不直接操作 ORM Session。
- 手机号剥离逻辑自包含，不复用 preprocess 的脱敏函数（用途不同，避免反向耦合）。

匹配流程：
1. 无工号 -> 无法按工号绑定，告警跳过
2. 工号直连：名单已登记该工号 -> 绑定 dingtalk_user_id
3. 首次登记：名单无此工号 -> 用姓名/手机号在「未绑工号」工程师中唯一定位
   -> 回填 staff_id + 绑定 dingtalk_user_id
4. 无法唯一确定（同名无法消歧 / 无匹配）-> 不绑定 + 告警（不阻断主流程）
"""

import logging
import re
from dataclasses import dataclass

from . import db_manager

logger = logging.getLogger(__name__)

# 手机号正则（与 preprocess 脱敏规则一致，但此处用于"提取"，用途不同，故独立定义）
_MOBILE_RE = re.compile(r"1[3-9]\d{9}")


@dataclass
class MatchResult:
    """匹配结果"""

    matched: bool  # 是否匹配到工程师并完成绑定
    engineer_id: int | None = None
    engineer_name: str = ""
    staff_id_filled: bool = False  # 本次是否回填了工号
    dingtalk_id_filled: bool = False  # 本次是否绑定了 userId
    reason: str = ""  # 命中路径 / 未命中原因（用于日志）


def _extract_mobile(text: str) -> str:
    """从文本中提取第一个手机号，无则返回空串"""
    if not text:
        return ""
    m = _MOBILE_RE.search(text)
    return m.group(0) if m else ""


def _strip_mobile(text: str) -> str:
    """剥离文本中的所有手机号，返回纯文本（去除首尾空格）"""
    if not text:
        return ""
    return _MOBILE_RE.sub("", text).strip()


def match_and_bind(
    sender_nick: str,
    sender_staff_id: str = "",
    sender_user_id: str = "",
) -> MatchResult:
    """
    按工号绑定工程师身份。

    参数：
        sender_nick      : 发送者昵称（可能含"姓名+手机号"，仅用于首次登记工号时的定位）
        sender_staff_id  : 发送者工号（钉钉 staffId，绑定的核心依据）
        sender_user_id   : 发送者钉钉 userId（被绑定的值，用于发私聊消息）

    返回：MatchResult（未命中时 matched=False，调用方据此告警，不阻断流程）
    """
    nick = sender_nick or ""
    staff_id = (sender_staff_id or "").strip()
    user_id = (sender_user_id or "").strip()
    mobile_in_nick = _extract_mobile(nick)
    pure_nick = _strip_mobile(nick)

    # ---------- 1. 无工号 -> 无法按工号绑定 ----------
    if not staff_id:
        logger.warning("无工号，无法按工号绑定：nick=%s -> 不绑定", nick)
        return MatchResult(matched=False, reason="无工号，无法按工号绑定")

    # ---------- 2. 工号直连：名单已登记该工号 ----------
    eng = db_manager.get_engineer_by_staff_id(staff_id)
    if eng:
        return _bind_user_id(eng, user_id, reason="工号直连")

    # ---------- 3. 首次登记：名单无此工号，定位「未绑工号」的工程师 ----------
    target = _locate_unbound_engineer(pure_nick, mobile_in_nick)
    if target:
        return _bind_staff_and_user(
            target, staff_id, user_id, reason="首次登记工号+绑定"
        )

    # ---------- 4. 无法定位 -> 不绑定 ----------
    _mobile_desc = "有" if mobile_in_nick else "无"
    logger.warning(
        "无法按工号定位工程师：nick=%s staff_id=%s mobile=%s -> 不绑定",
        nick, staff_id, _mobile_desc,
    )
    return MatchResult(
        matched=False,
        reason="工号未登记且姓名/手机号无法唯一确定",
    )


def _locate_unbound_engineer(pure_nick: str, mobile_in_nick: str) -> dict | None:
    """
    在「未登记工号」的工程师中，用姓名/手机号唯一定位一人。
    用于首次绑定：消息带了工号，但名单该工程师的 staff_id 尚为空。
    """
    # 手机号直查（最可靠）
    if mobile_in_nick:
        eng = db_manager.get_engineer_by_mobile(mobile_in_nick)
        if eng and not eng.get("staff_id"):
            return eng

    # 纯姓名匹配（仅取未绑工号的候选）
    candidates = [e for e in _find_by_pure_name(pure_nick) if not e.get("staff_id")]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # 同名：用手机号在候选中消歧
        if mobile_in_nick:
            for c in candidates:
                if c.get("mobile") == mobile_in_nick:
                    return c
        names = ",".join(c["name"] for c in candidates)
        _mobile_desc = "有" if mobile_in_nick else "无"
        logger.warning(
            "同名无法消歧：候选=%s mobile=%s -> 不绑定",
            names, _mobile_desc,
        )
    return None


def _find_by_pure_name(pure_nick: str) -> list[dict]:
    """
    用剥离手机号后的纯姓名匹配工程师。
    先精确匹配 name == pure_nick；未中则名单 name 可能也带手机号，
    降级为遍历全部工程师剥离后比较。
    """
    if not pure_nick:
        return []
    exact = db_manager.get_engineers_by_name(pure_nick)
    if exact:
        return exact
    matched = []
    for e in db_manager.load_engineers_from_db():
        if _strip_mobile(e.get("name", "")) == pure_nick:
            matched.append(e)
    return matched


def _bind_user_id(eng: dict, user_id: str, reason: str) -> MatchResult:
    """工号已登记，仅绑定 dingtalk_user_id（原值为空时写入，不覆盖）。"""
    filled = bool(user_id and not eng.get("dingtalk_user_id"))
    if filled:
        db_manager.update_engineer_binding(eng["id"], dingtalk_user_id=user_id)
    _filled_desc = "是" if filled else "否(已有)"
    logger.info(
        "%s：%s(id=%s) 绑定 dingtalk_user_id=%s",
        reason, eng["name"], eng["id"], _filled_desc,
    )
    return MatchResult(
        matched=True,
        engineer_id=eng["id"],
        engineer_name=eng["name"],
        dingtalk_id_filled=filled,
        reason=reason,
    )


def _bind_staff_and_user(
    eng: dict, staff_id: str, user_id: str, reason: str
) -> MatchResult:
    """首次登记工号 + 绑定 dingtalk_user_id。"""
    filled_dt = bool(user_id and not eng.get("dingtalk_user_id"))
    db_manager.update_engineer_binding(
        eng["id"],
        staff_id=staff_id,
        dingtalk_user_id=user_id if filled_dt else None,
    )
    _filled_desc = "是" if filled_dt else "否(已有)"
    logger.info(
        "%s：%s(id=%s) 回填 staff_id + 绑定 dingtalk_user_id=%s",
        reason, eng["name"], eng["id"], _filled_desc,
    )
    return MatchResult(
        matched=True,
        engineer_id=eng["id"],
        engineer_name=eng["name"],
        staff_id_filled=True,
        dingtalk_id_filled=filled_dt,
        reason=reason,
    )
