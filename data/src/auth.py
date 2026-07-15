"""
API 鉴权中间件 -- 基于 API Key + 角色的访问控制。

角色与可访问路径（见 config.API_ROLES）：
- service : POST /api/v1/message、POST /task（内部服务/机器人）
- readonly: GET /tasks、/engineers、/memories（只读查询）
- admin    : 全部接口

校验逻辑（基于"命中角色对该路径的权限"，不依赖路由侧声明角色）：
1. 请求头 X-API-Key 与 config.ROLE_KEYS 中某角色的 Key 比对，命中角色
2. 检查请求路径是否在该角色允许集合内（admin 通配放行）
3. 留空 Key 的角色不启用，无法用该角色访问

安全要点：
- 失败统一返回 401/403，错误信息不区分"无 Key / Key 错"，防探测
- admin 角色通配放行，隐含 service + readonly 权限
"""

from fastapi import HTTPException, Request, status

from .config import API_ROLES, ROLE_KEYS


def _role_can_access(role: str, path: str) -> bool:
    """
    判断角色是否有权访问指定路径。

    admin 通配放行；其他角色按 config.API_ROLES 中的显式集合判断。
    """
    allowed = API_ROLES.get(role, set())
    if "*" in allowed:
        return True
    return path in allowed


def verify_api_key(request: Request) -> str:
    """
    FastAPI 依赖：校验 API Key 并按角色鉴权。

    校验顺序：Key 存在 -> Key 有效（命中某角色）-> 该角色有权访问当前路径。
    返回命中的角色名（供日志/审计使用）。

    路由侧无需声明所需角色，权限完全由 config.API_ROLES 的路径集合决定。
    """
    provided_key = request.headers.get("x-api-key", "")

    # 统一错误：不区分"没带 Key"和"Key 错误"，防探测
    if not provided_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权：缺少 API Key",
        )

    # 查找 Key 对应的角色
    matched_role = None
    for role, role_key in ROLE_KEYS.items():
        if provided_key == role_key:
            matched_role = role
            break

    if matched_role is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权：API Key 无效",
        )

    # 角色权限校验
    path = request.url.path
    if not _role_can_access(matched_role, path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足",
        )

    return matched_role
