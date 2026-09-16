"""
用户目录与登录鉴权。

凭据从环境变量 `APP_USERS` 读，格式 `名字:密码,名字:密码`。
未配置时**拒绝所有登录**（安全默认）：这个应用会让使用者以应用身份执行工具，
不能在没有凭据的情况下暴露出去。

Chainlit 要求鉴权开启时必须设置 `CHAINLIT_AUTH_SECRET`（签发会话 JWT 用），
未设置会在启动时报错。
"""
import hmac
import os
from typing import Optional

import chainlit as cl
from chainlit.logger import logger


def _users() -> dict:
    """解析 APP_USERS。格式不正确的条目直接跳过，避免半个凭据被当成有效账号。"""
    users = {}
    for item in os.environ.get("APP_USERS", "").split(","):
        name, sep, password = item.strip().partition(":")
        if sep and name.strip() and password:
            users[name.strip()] = password
    return users


@cl.password_auth_callback
async def password_auth(username: str, password: str) -> Optional[cl.User]:
    expected = _users().get(username.strip())
    # compare_digest 做定长比较，避免按字符逐位泄露密码长度/前缀
    if expected is not None and hmac.compare_digest(expected, password):
        logger.info("登录成功: %s", username)
        return cl.User(identifier=username.strip(), display_name=username.strip())
    logger.warning("登录失败: %s", username)
    return None


if not _users():
    logger.warning(
        "未配置 APP_USERS，所有登录都会被拒绝。"
        '在 .env 里加一行启用，例如 APP_USERS="alice:换成你的密码"'
    )
