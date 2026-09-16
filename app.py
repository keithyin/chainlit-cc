"""
Chainlit + Claude Code CLI 风格交互
功能：token 流式、工具 COT、HITL 权限审批、停止（/stop 或内建停止按钮）、多会话、
      共享能力包（给同事用的 skills）、登录鉴权、每使用者独立工作目录、文件上传
运行: chainlit run app.py
"""
import asyncio
import contextlib
import json
import os
import re
import shutil
import uuid

import chainlit as cl
import db
from chainlit.data import get_data_layer
from chainlit.logger import logger
from chainlit.types import ThreadDict

from agent import (
    CAPABILITIES_DIR,
    WORKSPACES_ROOT,
    Conversation,
    TurnState,
    _ensure_reply,
    _running,
    abort_client,
    capability_catalog,
    cleanup_turn,
    get_client,
    release_client,
    render_message,
    shutdown_all_clients,
    stop_turn,
    workspace_dir,
)

# 导入即完成 Chainlit 鉴权回调注册（装饰器在 auth 模块内生效）
from auth import password_auth  # noqa: F401

# 导入即把 POST /register 挂到 Chainlit 的 FastAPI app 上（见 register.py）
import register  # noqa: F401

# 由本应用自己处理的斜杠命令。其余原样转发给 CLI —— 那里既有内置命令
# （/compact、/context…），也有共享能力包里的 skill（/<能力名>）。
APP_COMMANDS = {"/help", "/new", "/sessions", "/skills", "/stop"}


@cl.data_layer
def data_layer():
    return db.build()


@cl.on_app_startup
async def on_app_startup():
    # 建表必须早于第一次登录：登录会先查/建 users 表（见 db.init_schema 的说明）
    await db.init_schema(get_data_layer().engine)

HELP_TEXT = f"""👋 这是一个 Claude Code CLI 风格的交互界面。

**命令**
- 直接发消息 → 在激活会话中执行任务
- `/skills` → 共享能力面板（列出能力包，可一键调用）
- `/new [标题]` → 新建会话并切换
- `/sessions` → 列出会话（点选切换 / 停止）
- `/stop` → 停止激活会话正在运行的任务
- `/help` → 显示本帮助

其余以 `/` 开头的输入（`/compact`、`/context` 等内置命令，以及能力包里的 `/<能力名>`）
会原样交给 Claude Code 执行。

**工作目录**
`{WORKSPACES_ROOT}/<你的登录名>/` —— 你的会话产生的文件都落在这里，与其他使用者分开。
上传的文件也会被放进这个目录，然后按路径交给 Claude 处理。

**会话**
每个会话完全隔离（上下文、记忆互不可见）。
侧栏「历史」列的是**线程**（一次浏览器会话），里面可以装多个会话；
`/sessions` 列的是当前线程里的这些会话。刷新会自动接回上次的线程。

**工具权限**
`.env` 中 `ALLOWED_TOOLS` 允许列表内的工具直接执行，
其余工具会弹出审批卡片（批准 / 拒绝）后才执行
（CLI 内置只读命令如 ls/cat/echo 由 CLI 自动放行，不进审批）。
流式输出期间，输入框的停止按钮等价于 CLI 的 Escape。"""


# ---------------------------------------------------------------------------
# 会话管理
#
# cl.user_session["conversations"] 里存的是 **JSON 安全的镜像**，不是 Conversation
# 对象本身。原因：数据层恢复时 socket.py 会执行 user_sessions[session.id] =
# metadata.copy()，整体替换 user_session；而持久化用的编码器会把无法序列化的
# 对象（dataclass）一律变成 null。存对象的话恢复后就只剩一堆 None。
#
# 进程内缓存：(登录名, 浏览器会话 id, conv_id) -> Conversation，重启即空。
# 必须带上浏览器会话 id：一个使用者的两个标签页是两个独立会话，会话列表不能互相
# 串台（每个标签页有自己的 /new、/sessions）。恢复时 metadata 会把镜像整体换掉，
# 所以按 session 分桶正好与"一个线程 = 一个会话列表"对齐。
# ---------------------------------------------------------------------------

_CONVS: dict = {}


def _user_key() -> tuple:
    user = cl.user_session.get("user")
    identifier = getattr(user, "identifier", None) or "anonymous"
    return (identifier, getattr(cl.context.session, "id", ""))


def _conv_object(cid: str, item: dict) -> Conversation:
    """从镜像条目重建（或取出缓存的）Conversation 对象。"""
    key = (*_user_key(), cid)
    conv = _CONVS.get(key)
    if conv is None:
        conv = Conversation(
            id=cid,
            title=item.get("title") or f"会话 {cid}",
            workspace=_workspace(),
            claude_session_id=item.get("claude_session_id"),
            loaded_skills=item.get("loaded_skills"),
        )
        _CONVS[key] = conv
    return conv


def _conversations() -> dict:
    """本会话的全部 Conversation，按 conv_id 索引。

    两个来源缺一不可：
      - 镜像：从 user_session / 线程 metadata 读到的持久化条目；
      - 进程内缓存：刚创建、还没同步进镜像的条目（_sync_convs 是拿本函数的
        结果去重写镜像的，只看镜像的话新会话会被自己擦掉）。
    兜底成 {}：客户端在 on_chat_start 跑完之前就发消息时，这里原本会拿到 None
    并在下面炸出 AttributeError。
    """
    out = {}
    raw = cl.user_session.get("conversations") or {}
    for cid, item in raw.items():
        if isinstance(item, dict):
            out[cid] = _conv_object(cid, item)
        # 非 dict 的历史脏值（旧版把 dataclass 塞进 user_session，落盘成了 null）跳过
    scope = _user_key()
    for key, conv in _CONVS.items():
        if key[:2] == scope:
            out[key[2]] = conv
    return out


def _register(conv: Conversation) -> None:
    """新建会话的唯一入口：先入缓存，再让 _sync_convs 把它写进镜像。"""
    _CONVS[(*_user_key(), conv.id)] = conv
    cl.user_session.set("active_conv", conv.id)
    _sync_convs()


def _sync_convs() -> None:
    """把 Conversation 上会被改动的字段写回 JSON 安全的镜像。

    模型那边会改 claude_session_id / loaded_skills（agent.py 的 render_message），
    所以每轮结束后都要同步一次，否则刷新恢复时接不回原来的 CLI 会话。
    """
    mirror = {}
    for cid, conv in _conversations().items():
        mirror[cid] = {
            "title": conv.title,
            "claude_session_id": conv.claude_session_id,
            "loaded_skills": conv.loaded_skills,
        }
    cl.user_session.set("conversations", mirror)


async def _flush_convs() -> None:
    """把会话镜像冲进线程 metadata（update_thread 是合并写，不覆盖别的键）。

    没有数据层时是空操作；主进程被硬杀时这是最后一次把 claude_session_id 落盘
    的机会（框架自己只在 socket 断开时写一次）。

    只能在 socket 事件上下文里调用：cl.context 与 cl.user_session 都读同一个
    ContextVar，离开上下文两者都抛（ChainlitContextException）。现有调用点
    （_run_turn 的 _finalize、/new、conv_switch）都在上下文里，_finalize 由
    asyncio.shield 起的 Task 会复制当前 context，所以也成立。
    """
    session = cl.context.session
    data_layer = get_data_layer()
    if data_layer is None or not session.thread_id or not session.has_first_interaction:
        return
    _sync_convs()
    with contextlib.suppress(Exception):
        await data_layer.update_thread(
            thread_id=session.thread_id,
            metadata={
                "cc_convs": cl.user_session.get("conversations") or {},
                "cc_active": cl.user_session.get("active_conv"),
            },
        )


def _active_conv():
    convs = _conversations()
    return convs.get(cl.user_session.get("active_conv"))


def _workspace() -> str:
    """本使用者的工作目录。首次访问时按登录名创建，之后复用。

    以登录名（而不是浏览器会话）为键：同事换标签页/重连后仍回到自己的工作目录。
    """
    ws = cl.user_session.get("workspace")
    if ws is None:
        user = cl.user_session.get("user")
        ws = workspace_dir(user.identifier if user else "anonymous")
        cl.user_session.set("workspace", ws)
    return ws


def _save_uploads(workspace: str, elements) -> list:
    """把用户上传的文件复制进工作目录，返回落盘后的路径。

    Chainlit 把上传文件存在会话自己的临时目录里，agent 看不到；复制进工作目录
    之后（工作目录就是 CLI 的 cwd）模型就能直接读。

    文件名来自浏览器，不能直接当路径用；同名文件加序号后缀，避免后来的上传
    悄悄覆盖掉工作目录里已有的文件。
    """
    saved = []
    for el in elements or []:
        src = getattr(el, "path", None)
        if not src or not os.path.isfile(src):
            continue  # 只处理落在本地磁盘的上传（配置了 data layer 时可能是 url）
        name = re.sub(r"[^\w.\-]", "_", os.path.basename(getattr(el, "name", "") or ""))
        dest = os.path.abspath(os.path.join(workspace, name or "upload"))
        if os.path.dirname(dest) != os.path.abspath(workspace):
            continue  # 名字里带了路径成分（如 ..），丢弃
        stem, ext = os.path.splitext(dest)
        n = 1
        while os.path.exists(dest):
            dest = f"{stem}-{n}{ext}"
            n += 1
        shutil.copyfile(src, dest)
        saved.append(dest)
    return saved


@cl.on_chat_start
async def on_chat_start():
    conv = Conversation(id=uuid.uuid4().hex[:8], title="会话 1", workspace=_workspace())
    _register(conv)
    await cl.Message(content=HELP_TEXT).send()


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    """刷新 / 点侧栏历史时接回原会话。

    Chainlit 自己会把历史消息重放给前端，这里只负责恢复应用侧的状态：会话列表
    （claude_session_id 在里面，决定下一轮接不接得上原来的 CLI 会话）。
    """
    md = thread.get("metadata") or {}
    if isinstance(md, str):  # SQLite 里存的是 TEXT
        with contextlib.suppress(json.JSONDecodeError):
            md = json.loads(md)
    items = md.get("cc_convs") if isinstance(md, dict) else None
    if not items:
        # 开数据层之前的老线程，或元数据被 redact 了：退化成新建，总比空列表好
        await on_chat_start()
        return

    cl.user_session.set("conversations", items)
    active = md.get("cc_active")
    cl.user_session.set("active_conv", active if active in items else next(iter(items)))

    # 必须清掉：框架只在 __init__ 里赋值一次，而 connection_successful 每次 socket
    # 重连都会重跑这段恢复逻辑。不清的话网络一抖就重放整条线程。
    cl.context.session.thread_id_to_resume = None

    await cl.Message(
        content=f"♻️ 已恢复上次的会话（共 {len(items)} 个）。"
                f"`/sessions` 查看切换，`/new` 新建。"
    ).send()


# ---------------------------------------------------------------------------
# turn 驱动
# ---------------------------------------------------------------------------

async def _run_turn(conv: Conversation, prompt: str):
    turn = TurnState(conv_id=conv.id, task=asyncio.current_task())
    _running[conv.id] = turn

    client = None
    interrupted = False
    crashed = False
    try:
        client = await get_client(conv)
        await client.query(prompt)
        async for m in client.receive_response():
            await render_message(m, conv, turn)
    except asyncio.CancelledError:
        # 停止路径：内建停止按钮或 /stop 取消了本 task。
        # 立即重抛：finally 先执行清理，随后 CancelledError 自然向上传播
        # （若在 finally 里裸 raise，异常已被 except 吞掉，会抛 RuntimeError）。
        interrupted = True
        raise
    except Exception as e:
        logger.exception("turn 出错")
        crashed = True
        with contextlib.suppress(Exception):
            reply = await _ensure_reply(turn)
            await reply.stream_token(f"\n\n❌ 出错: {e}")
    finally:
        # 进程与 UI 收尾统一放进 shield：即使清理期间任务再次
        # 被取消（CancelledError 是 BaseException，suppress(Exception) 拦不住），
        # 清理任务也会独立跑完，避免 _running 泄漏导致会话永久"忙"。
        async def _finalize():
            if interrupted:
                # Escape 语义：interrupt 后断开（下一轮凭 resume 重连续接）
                with contextlib.suppress(Exception):
                    await abort_client(conv.id)
            elif crashed:
                # 进程可能已坏：直接断开，下一轮 fresh connect + resume
                with contextlib.suppress(Exception):
                    await release_client(conv.id)
            with contextlib.suppress(Exception):
                await cleanup_turn(turn, interrupted)
            with contextlib.suppress(Exception):
                # 模型那边可能刚写好新的 claude_session_id，落盘后再更新气泡
                await _flush_convs()
            with contextlib.suppress(Exception):
                if turn.reply is not None:
                    await turn.reply.update()
            if interrupted and turn.stopped_by_slash:
                # 内建按钮路径框架已发 "Task manually stopped."，这里不重复
                with contextlib.suppress(Exception):
                    await cl.Message(content="⏹ 已停止。").send()

        try:
            await asyncio.shield(_finalize())
        except BaseException:
            pass


@cl.on_message
async def on_message(user_message: cl.Message):
    text = (user_message.content or "").strip()
    if text.startswith("/"):
        cmd = text.split(maxsplit=1)[0].lower()
        if cmd in APP_COMMANDS:
            if user_message.elements:
                # 这些命令不经过模型，附件不会被处理；明说，别让文件静默消失
                await cl.Message(
                    content=f"⚠️ `{cmd}` 是应用自己的命令，不处理附件。"
                            f"要和文件一起发的话，请用普通消息。"
                ).send()
            await _handle_slash(text)
            return
        # 其余斜杠命令不拦截：SDK 会把 prompt 开头的 /name 当命令派发（而不是
        # 当普通消息），于是 CLI 内置命令和能力包里的 skill 都能直接用。

    conv = _active_conv()
    if conv is None:
        await cl.Message(content="没有可用会话，先发送 /new 新建一个。").send()
        return
    if conv.id in _running:
        await cl.Message(
            content=f"⏳ 会话「{conv.title}」正在处理中，发送 /stop 可停止。"
        ).send()
        return

    # 上传的文件先落进工作目录，再把路径写进 prompt——CLI 的 cwd 就是工作目录，
    # 模型按路径直接读。不做多模态内容块：那样只对图片且依赖网关支持，
    # 落盘对任意文件类型都成立，也和"工作目录即工作区"的模型一致。
    uploaded = _save_uploads(conv.workspace, user_message.elements)
    if not text and not uploaded:
        await cl.Message(content="消息是空的。").send()
        return
    if uploaded:
        listing = "\n".join(f"- {p}" for p in uploaded)
        text = f"{text}\n\n（已把上传的文件放进你的工作目录：\n{listing}\n）".strip()
    await _run_turn(conv, text)


# ---------------------------------------------------------------------------
# slash 命令
# ---------------------------------------------------------------------------

async def _handle_slash(text: str):
    """分派本应用自己的命令。调用方已按 APP_COMMANDS 过滤，其余命令不进这里。"""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        await cl.Message(content=HELP_TEXT).send()
    elif cmd == "/new":
        conv = Conversation(
            id=uuid.uuid4().hex[:8],
            title=arg or f"会话 {len(_conversations()) + 1}",
            workspace=_workspace(),
        )
        _register(conv)
        await _flush_convs()
        await cl.Message(content=f"🆕 已新建会话「{conv.title}」并切换过去。").send()
    elif cmd == "/sessions":
        await _cmd_sessions()
    elif cmd == "/skills":
        await _cmd_skills()
    elif cmd == "/stop":
        conv = _active_conv()
        if conv and stop_turn(conv.id, by_slash=True):
            logger.info("slash /stop: 停止会话 %s", conv.id)
        else:
            await cl.Message(content="当前没有正在运行的任务。").send()


async def _cmd_skills():
    """共享能力面板。

    清单来自磁盘（会话建立前也能看）；CLI 实际加载数只有建连后才知道，
    单独列出来用于核对能力包有没有真的挂上——这条链路很容易静默失效。
    """
    conv = _active_conv()
    catalog = capability_catalog()
    if not catalog:
        await cl.Message(
            content=(
                f"能力包是空的。把 skill 放进 "
                f"`{CAPABILITIES_DIR}/.claude/skills/<能力名>/SKILL.md` 就会出现在这里，"
                f"写法见 `{CAPABILITIES_DIR}/README.md`。"
            )
        ).send()
        return

    lines = "\n".join(
        f"- **{c['name']}** — {c['description'] or '（SKILL.md 没写 description）'}"
        for c in catalog
    )
    loaded = ""
    if conv is not None and conv.loaded_skills is not None:
        loaded = f"\n\nCLI 本会话实际加载 {len(conv.loaded_skills)} 个 skill。"
    await cl.Message(
        content=(
            f"📦 共享能力包（{len(catalog)} 个）：\n\n{lines}{loaded}\n\n"
            f"点按钮直接调用，或输入 `/<能力名>`。"
        ),
        actions=[
            cl.Action(name="skill_invoke", label=f"▶ {c['name']}",
                      payload={"skill": c["name"]})
            for c in catalog
        ],
    ).send()


async def _cmd_sessions():
    convs = _conversations()
    active_id = cl.user_session.get("active_conv")
    actions = []
    for conv in convs.values():
        marker = "👉" if conv.id == active_id else "•"
        busy = " ⏳运行中" if conv.id in _running else ""
        actions.append(cl.Action(
            name="conv_switch",
            label=f"{marker} {conv.title} ({conv.id}){busy}",
            payload={"conv": conv.id},
        ))
        if conv.id in _running:
            actions.append(cl.Action(name="turn_stop", label="⏹ 停止",
                                     payload={"conv": conv.id}))
    await cl.Message(
        content=f"📋 共 {len(convs)} 个会话，点击按钮切换（运行中的可停止）：",
        actions=actions,
    ).send()


# ---------------------------------------------------------------------------
# action 回调（HTTP /project/action 触发，与 websocket 同一 event loop）
# ---------------------------------------------------------------------------

@cl.action_callback("tool_approval")
async def on_tool_approval(action: cl.Action):
    turn = _running.get(action.payload.get("conv", ""))
    if turn is None:
        return  # turn 已结束：幂等忽略
    item = turn.pending_approvals.get(action.payload.get("tid", ""))
    if item is None:
        return
    fut, _ = item
    if fut.done():
        return  # 防双击 / 点击已过期
    fut.set_result((action.payload.get("decision", "deny"), ""))


@cl.action_callback("conv_switch")
async def on_conv_switch(action: cl.Action):
    conv_id = action.payload.get("conv")
    convs = _conversations()
    if conv_id not in convs:
        return
    cl.user_session.set("active_conv", conv_id)
    await _flush_convs()
    await cl.Message(content=f"🔀 已切换到会话「{convs[conv_id].title}」。").send()


@cl.action_callback("turn_stop")
async def on_turn_stop(action: cl.Action):
    if stop_turn(action.payload.get("conv"), by_slash=True):
        logger.info("action turn_stop: 停止会话 %s", action.payload.get("conv"))


@cl.action_callback("skill_invoke")
async def on_skill_invoke(action: cl.Action):
    """能力面板上的按钮：等价于用户手输 /<能力名>。"""
    conv = _active_conv()
    if conv is None:
        return
    if conv.id in _running:
        await cl.Message(
            content=f"⏳ 会话「{conv.title}」正在处理中，发送 /stop 可停止。"
        ).send()
        return
    await _run_turn(conv, f"/{action.payload.get('skill', '')}")


@cl.on_stop
async def on_stop():
    # 内建停止按钮已取消 current task，清理在 _run_turn 的 finally 完成
    logger.info("on_stop: 用户点击了内建停止按钮")


@cl.on_app_shutdown
async def on_app_shutdown():
    # 退出时断开全部常驻 CLI 子进程，避免残留
    await shutdown_all_clients()
