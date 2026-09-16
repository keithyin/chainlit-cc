"""用户目录与登录鉴权。

凭据有两个来源，`APP_USERS` 优先：

1. 环境变量 `APP_USERS`，格式 `名字:密码,名字:密码`。引导/管理员通道：它是唯一
   不随聊天记录库一起丢失的凭据，库没了也还能靠它进来。
2. 数据库 `user_accounts` 表里的自助注册账号（读写见 accounts.py）。

同名时以 `APP_USERS` 为准且**不回落**到数据库——规则要可预测："APP_USERS 里的
名字只能用 APP_USERS 的密码"。注册端会直接把这种名字拒掉（409），所以库里一般
不会有同名行；真要撤销某个人，得把 `APP_USERS` 里的条目和库里的行一起删。

两个来源都没有时**拒绝所有登录**（安全默认）：这个应用会让使用者以应用身份执行
工具，不能在没有凭据的情况下暴露出去。

Chainlit 要求鉴权开启时必须设置 `CHAINLIT_AUTH_SECRET`（签发会话 JWT 用），
未设置会在启动时报错。
"""
import hmac
import os
from typing import Optional

import chainlit as cl
import accounts
from chainlit.logger import logger


def _users() -> dict:
    """解析 APP_USERS。格式不正确的条目直接跳过，避免半个凭据被当成有效账号。"""
    users = {}
    for item in os.environ.get("APP_USERS", "").split(","):
        name, sep, password = item.strip().partition(":")
        if sep and name.strip() and password:
            users[name.strip()] = password
    return users


def is_env_user(name: str) -> bool:
    """这个名字是否被 APP_USERS 占用（注册端用它挡撞名）。"""
    return name in _users()


@cl.password_auth_callback
async def password_auth(username: str, password: str) -> Optional[cl.User]:
    name = username.strip()
    expected = _users().get(name)
    if expected is not None:
        if hmac.compare_digest(expected, password):
            ok = True
        else:
            # 名字走的是 env 通道时，密码错也要花掉一次 KDF 的时间：
            # 不然"秒回"就等于告诉对方这个名字是管理员账号。
            await accounts.dummy_verify(password)
            ok = False
    else:
        ok = await accounts.verify(name, password)

    if ok:
        logger.info("登录成功: %s", name)
        return cl.User(identifier=name, display_name=name)
    logger.warning("登录失败: %s", name)
    return None


if not _users():
    logger.info(
        "未配置 APP_USERS：只能靠自助注册的账号登录，建议留一个引导账号"
        '（.env 里加一行，例如 APP_USERS="alice:换成你的密码"）——'
        "聊天记录库丢失时它是唯一还能进门的凭据。"
    )
