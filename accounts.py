"""自注册账号的口令哈希与 user_accounts 表读写。

只做两件事：把口令变成可存的一行字符串、把输入口令和那行字符串比一比。
不认识 Chainlit，也不认识 HTTP——注册端点在 register.py，登录校验在 auth.py，
两边都只调这里的函数。

用 stdlib 的 scrypt 而不是 bcrypt/argon2：这是唯一零新增依赖的内存硬 KDF，
省得为一个 40 行的功能往镜像里多塞一个需要编译的包。
"""
import asyncio
import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone

from chainlit.data import get_data_layer
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# 参数写进存储串里，将来想调大 n 不需要动表结构：老记录按自己的参数验，
# 新记录按新参数存（verify 用 dklen=len(期望值)，所以改 dklen 也兼容）。
#
# n=2**14 实测约 28ms，够挡住离线爆破又不至于让登录卡顿。再往上（n=2**15）
# 会撞上 OpenSSL 默认 32MiB 的 maxmem 上限直接报错，调大必须同时传 maxmem。
_N, _R, _P = 2**14, 8, 1
_DKLEN = 32
_SALT_BYTES = 16

# 用户名不存在时拿它跑一遍完整的 KDF，用来把"账号存在与否"的响应时间抹平：
# 否则凭"秒回"就能枚举出哪些用户名是真的。盐和摘要都是 0，永远比不中。
_DUMMY = "scrypt${}${}${}${}${}".format(
    _N, _R, _P,
    base64.b64encode(b"\x00" * _SALT_BYTES).decode(),
    base64.b64encode(b"\x00" * _DKLEN).decode(),
)

def _engine():
    """账号表和聊天记录共用同一个 SQLite（见 db.py），不另开连接池。"""
    layer = get_data_layer()
    if layer is None or layer.engine is None:
        raise RuntimeError("数据层尚未初始化，账号功能不可用")
    return layer.engine


def hash_password(password: str) -> str:
    """产出 `scrypt$n$r$p$b64(salt)$b64(dk)` 形式的自描述串。"""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验口令。任何解析失败都返回 False，绝不抛——这个函数在登录路径上。"""
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(dk_b64, validate=True)
        if not salt or not expected:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n), r=int(r), p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    # 定长比较，避免按字符逐位泄露前缀（与 auth.py 里对 APP_USERS 的处理一致）
    return hmac.compare_digest(actual, expected)


async def create(username: str, password: str) -> bool:
    """建号。返回 False 表示用户名已存在。

    靠主键冲突判重而不是先 SELECT 再 INSERT：后者在并发下有竞态窗口。
    """
    stored = await asyncio.to_thread(hash_password, password)
    try:
        async with _engine().begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO user_accounts ("username", "passwordHash", "createdAt") '
                    "VALUES (:username, :passwordHash, :createdAt)"
                ),
                {
                    "username": username,
                    "passwordHash": stored,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                },
            )
    except IntegrityError:
        return False
    return True


async def verify(username: str, password: str) -> bool:
    """校验账号口令。用户名不存在时也跑满一次 KDF（见 _DUMMY）。"""
    async with _engine().connect() as conn:
        result = await conn.execute(
            text('SELECT "passwordHash" FROM user_accounts WHERE "username" = :username'),
            {"username": username},
        )
        row = result.first()
    if row is None:
        await dummy_verify(password)
        return False
    # KDF 要 28ms 的纯 CPU：直接 await 会卡住正在给别的会话流式输出的 event loop
    return await asyncio.to_thread(verify_password, password, row[0])


async def dummy_verify(password: str) -> None:
    """白跑一次 KDF，把"这个名字不存在/不走库"的耗时对齐到真实校验。"""
    await asyncio.to_thread(verify_password, password, _DUMMY)
