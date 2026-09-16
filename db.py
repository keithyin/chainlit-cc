"""聊天记录持久化：SQLite 数据层。

Chainlit 的数据层要负责两件事：把每一步（用户消息、助手消息、工具步骤）写进库，
以及把线程元数据存下来供刷新后恢复。这里用官方自带的 SQLAlchemyDataLayer + 本地
SQLite，不引入额外服务。

表结构必须严格对齐 Chainlit 绑定的列名：它按 dict 的键拼 `INSERT INTO steps (...)`
语句，列对不上时 execute_sql 会**吞掉异常只记一条 warning**，表现为"看着没报错但
什么都没存"。所以下面每列都是照着 step.py 的 Step.to_dict() / message.py 的
Message.to_dict() 的并集列的，别随手删。
"""
import os

from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from sqlalchemy import text

from agent import WORKDIR

DB_PATH = os.environ.get("CHAINLIT_HISTORY_DB") or os.path.join(
    WORKDIR, "chainlit_history.db"
)

# sqlite+aiosqlite:/// 后面跟绝对路径时要凑满四个斜杠（三个是 URL 分隔符，
# 第四个才是根目录），否则会当成相对路径。
CONNINFO = f"sqlite+aiosqlite:///{os.path.abspath(DB_PATH)}"

# 一轮长任务会连写很多步骤；sqlite3 默认 5 秒锁等待太短，容易直接报 database is locked
CONNECT_ARGS = {"timeout": 30}

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        "id" TEXT PRIMARY KEY,
        "identifier" TEXT NOT NULL UNIQUE,
        "createdAt" TEXT NOT NULL,
        "metadata" TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS threads (
        "id" TEXT PRIMARY KEY,
        "createdAt" TEXT,
        "name" TEXT,
        "userId" TEXT,
        "userIdentifier" TEXT,
        "tags" TEXT,
        "metadata" TEXT
    );
    """,
    # 布尔列用 INTEGER：布尔值经 SQLite 往返后必须是真布尔，前端按 true/false 用
    """
    CREATE TABLE IF NOT EXISTS steps (
        "id" TEXT PRIMARY KEY,
        "name" TEXT,
        "type" TEXT,
        "threadId" TEXT,
        "parentId" TEXT,
        "command" TEXT,
        "modes" TEXT,
        "streaming" INTEGER,
        "waitForAnswer" INTEGER,
        "isError" INTEGER,
        "metadata" TEXT,
        "tags" TEXT,
        "input" TEXT,
        "output" TEXT,
        "createdAt" TEXT,
        "start" TEXT,
        "end" TEXT,
        "generation" TEXT,
        "showInput" TEXT,
        "defaultOpen" INTEGER,
        "autoCollapse" INTEGER,
        "language" TEXT
    );
    """,
    # 本应用不发 element（没有 storage_provider），但读取线程时要 SELECT 这张表
    """
    CREATE TABLE IF NOT EXISTS elements (
        "id" TEXT PRIMARY KEY,
        "threadId" TEXT,
        "type" TEXT,
        "url" TEXT,
        "chainlitKey" TEXT,
        "name" TEXT,
        "display" TEXT,
        "objectKey" TEXT,
        "size" TEXT,
        "props" TEXT,
        "page" INTEGER,
        "autoPlay" INTEGER,
        "playerConfig" TEXT,
        "language" TEXT,
        "forId" TEXT,
        "mime" TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS feedbacks (
        "id" TEXT PRIMARY KEY,
        "forId" TEXT,
        "threadId" TEXT,
        "value" INTEGER,
        "comment" TEXT
    );
    """,
    # 自助注册账号的口令（读写都在 accounts.py）。
    #
    # 刻意不放进 Chainlit 的 users 表：那一行的 metadata 每次登录都会被
    # sql_alchemy.create_user 覆盖，而且会随 GET /user 原样发给浏览器——
    # 口令哈希放那儿既会丢又会泄。users 的列名又是 Chainlit 绑定死的，
    # 加列得 ALTER TABLE，而现网已经有一个存量库，CREATE TABLE IF NOT EXISTS
    # 是这里唯一免迁移的建表手段，所以另起一张表。
    """
    CREATE TABLE IF NOT EXISTS user_accounts (
        "username" TEXT PRIMARY KEY,
        "passwordHash" TEXT NOT NULL,
        "createdAt" TEXT NOT NULL
    );
    """,
    'CREATE INDEX IF NOT EXISTS idx_steps_thread ON steps ("threadId");',
    'CREATE INDEX IF NOT EXISTS idx_elements_thread ON elements ("threadId");',
]


def build() -> SQLAlchemyDataLayer:
    """@cl.data_layer 回调。Chainlit 首次用到数据层时调用一次。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return SQLAlchemyDataLayer(conninfo=CONNINFO, connect_args=CONNECT_ARGS)


async def init_schema(engine) -> None:
    """建表。必须在处理第一个请求之前跑完。

    登录会走 authenticate_user -> data_layer.get_user / create_user，users 表不
    存在时那一步会抛异常并被 auth 层静默兜住：登录照样成功，但 user.id 为 None，
    于是 threads.userIdentifier 全写 NULL，侧栏空白、恢复也被拒。症状很安静，
    所以宁可在这里直接炸掉。
    """
    async with engine.begin() as conn:
        for statement in SCHEMA:
            await conn.execute(text(statement))
