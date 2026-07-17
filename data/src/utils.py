"""
公共工具函数 -- 全项目共享的辅助函数。

从各模块抽取的重复实现，统一维护。
"""


def extract_text(response) -> str:
    """
    从 LLM 响应中安全提取纯文本。

    兼容 str / list（多模态格式 [{"type":"text","text":"..."}]）/ 其他类型。
    原 _extract_text / _get_text 的统一版本，消除 5 处重复。
    """
    content = response.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", str(item)))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)
