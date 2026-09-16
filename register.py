"""自助注册：POST /register。

把注册表单的 JSON 变成一个账号，并**直接种下登录 cookie**——注册完就进应用，
不用再登一次。校验规则、口令哈希、落库分别由这里、accounts.py、db.py 负责。

两个只能这么做的地方：

1. 路由挂在 `chainlit.server.app` 上而不是它内部的 router 上：`chainlit run`
   是先 include 完 router 再加载本模块的，往 router 里加已经来不及；而 app 是
   模块级对象，后加的路由照样生效。**只敢用 POST**——SPA 兜底路由
   `GET /{full_path:path}` 在前，同路径的 GET 会被它先吃掉（所以注册页是
   public/register.html 这个静态文件，而不是本模块返回的 HTML）。
2. cookie 是自己签的：`chainlit.auth` 把 `create_jwt` / `set_auth_cookie` 导出了，
   调用它们得到的会话与走 /login 完全等价。

**改这个文件后必须真重启**（`--watch` 不够）：重载会在同一个进程里重跑模块，
路由被注册第二遍，而 Starlette 按注册顺序取第一个匹配，改的 handler 不会生效。

没有 CSRF token，也没有限流：`config.toml` 的 `allow_origins = ["*"]` 让跨站预检也能过，
所以第三方页面能诱导浏览器"注册一个新账号并登录进去"（login CSRF）。影响仅限于把受害者
换到一个攻击者建的账号上（看不到任何原有数据），而且 Chainlit 自己的 POST /login 本来
就有同样的性质，所以不单独处理。限流同理，交给反向代理，见 DEPLOY.md。
"""
import re

import chainlit as cl
import accounts
import auth
from chainlit.auth import create_jwt, set_auth_cookie
from chainlit.config import config
from chainlit.data import get_data_layer
from chainlit.logger import logger
from chainlit.server import app as cl_app
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

USERNAME_HINT = "用户名只能是 2-32 位的字母、数字、_ . -，且首尾必须是字母或数字"
PASSWORD_HINT = "密码至少 8 位（最多 128 位）"
TAKEN_HINT = "用户名已被占用"

# 字符集与 agent.workspace_dir 的清洗规则对齐（它把 [^A-Za-z0-9_.-] 换成 _，
# 再 strip 掉首尾的 ._）。首尾限定为字母数字是为了不被 strip 折叠：否则
# `bob` 和 `_bob` 会共用同一个工作目录。
USERNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,30}[A-Za-z0-9])?")


class RegisterBody(BaseModel):
    username: str
    password: str


def _reject(reason: str, status: int) -> JSONResponse:
    """错误一律不走 401：前端对 401 的处理是直接跳登录页，会盖掉提示。"""
    return JSONResponse({"error": reason}, status_code=status)


@cl_app.post(f"{config.run.root_path}/register")
async def register_account(body: RegisterBody, request: Request) -> JSONResponse:
    name = body.username.strip()
    if not 2 <= len(name) <= 32 or not USERNAME_RE.fullmatch(name):
        return _reject(USERNAME_HINT, 400)

    # 不 strip 口令本身（空格是合法字符），只挡纯空白
    if not 8 <= len(body.password) <= 128 or not body.password.strip():
        return _reject(PASSWORD_HINT, 400)

    if auth.is_env_user(name):
        # 与重名同一句应答：对外不暴露"这个名字是管理员账号"，原因只进日志
        logger.warning("注册被拒：%s 已在 APP_USERS 里", name)
        return _reject(TAKEN_HINT, 409)

    if not await accounts.create(name, body.password):
        return _reject(TAKEN_HINT, 409)

    user = cl.User(identifier=name, display_name=name)
    # 预建 users 行，别等第一个已鉴权请求再去建：那一步失败时登录照样成功，
    # 但 user.id 会是 None，于是 threads.userIdentifier 全写 NULL（见 db.init_schema）
    if (layer := get_data_layer()) is not None:
        try:
            await layer.create_user(user)
        except Exception as e:
            logger.error("注册后预建 users 行失败: %s", e)

    response = JSONResponse({"success": True})
    set_auth_cookie(request, response, create_jwt(user))
    logger.info("注册成功: %s", name)
    return response
