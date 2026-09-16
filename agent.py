"""
SDK 集成层：Claude Agent SDK 消息流 → Chainlit UI 的映射、HITL 权限回调、
以及 turn 运行态（TurnState / _running 注册表）。

app.py 只负责用户意图（slash 命令、action 回调、会话持久化），依赖方向单向：
app.py -> agent.py。
"""
import asyncio
import contextlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

import chainlit as cl
import yaml
from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from dotenv import load_dotenv

# Agent 的工作目录 = 本文件所在目录（固定为项目目录）
WORKDIR = os.path.dirname(os.path.abspath(__file__))

# 应用专用的 Claude 配置根目录：每个会话一个子目录（CLAUDE_CONFIG_DIR）。
# 作用：各会话的 transcript 与 memory 完全互不可见（真正的多会话隔离），
# 且应用的 CLI 状态不触碰开发者的真实 ~/.claude。
# 放在 WORKDIR 之外：会话状态若落在 agent 工作目录里，模型自主
# Grep/Read 工作目录时会直接翻到其它会话的 transcript，造成跨会话泄漏。
CC_HOME = os.path.expanduser("~/.chainlit-cc")

# 共享能力包：capabilities/.claude/skills/<名字>/SKILL.md，所有会话共用。
# 用 add_dirs 而不是 plugins=[{"type": "local", ...}]：实测（SDK 0.2.152 + 本机 CLI）
# --plugin-dir 挂载的插件虽然出现在 init 的 plugins 字段里，但它的 skills 不会出现在
# skills / slash_commands 中，等于没生效；add_dirs 指向的目录会被按项目级扫描
# .claude/skills/，实测有效。
CAPABILITIES_DIR = os.path.join(WORKDIR, "capabilities")

# 每个使用者自己的工作目录根。
# 刻意放在 CC_HOME 之外：agent 的工作目录若嵌在 CC_HOME 里，默认的 Glob/ls
# 向上遍历一层就能看到其它会话的 transcript 与 memory，正是当初把 CC_HOME
# 移出项目目录要避免的跨会话泄漏。
WORKSPACES_ROOT = os.environ.get("WORKSPACES_DIR") or os.path.expanduser(
    "~/chainlit-cc-workspaces"
)

# 从同目录 .env 读取 base url / api key / model（不覆盖已有环境变量）
load_dotenv(os.path.join(WORKDIR, ".env"))

# HITL 审批等待超时（秒），超时自动拒绝
APPROVAL_TIMEOUT_S = int(os.environ.get("APPROVAL_TIMEOUT_S", "300"))


def _gateway_env() -> dict:
    """base url / api key 显式传给 Claude Code CLI 子进程。"""
    return {
        k: v
        for k, v in {
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL"),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
        }.items()
        if v
    }


def _allowed_tools() -> list:
    """.env 的 ALLOWED_TOOLS 允许列表：命中的工具直接执行，不弹审批。"""
    raw = os.environ.get("ALLOWED_TOOLS", "Read,Glob,Grep")
    return [t.strip() for t in raw.split(",") if t.strip()]


@dataclass
class Conversation:
    """一个会话（直接存 cl.user_session，各自独立的 claude_session_id）。"""

    id: str
    title: str
    workspace: str  # 本使用者的工作目录，作为 CLI 的 cwd
    claude_session_id: Optional[str] = None
    loaded_skills: Optional[list] = None  # init 消息里 CLI 实际加载到的 skill 名单


@dataclass
class TurnState:
    """一个正在进行的 turn 的运行态（_running 注册表，不持久化）。"""

    conv_id: str
    task: asyncio.Task
    tool_steps: dict = field(default_factory=dict)        # tool_use_id -> cl.Step
    pending_approvals: dict = field(default_factory=dict)  # tool_use_id -> (Future, 审批 Message)
    thinking_step: Optional[cl.Step] = None                # 惰性创建的 thinking step
    reply: Optional[cl.Message] = None                     # 惰性创建的回复 Message（首个 text 出现时才建）
    text_streamed: bool = False                            # 是否已流出过 text_delta（整块兜底用）
    stopped_by_slash: bool = False                         # /stop 触发时置位，决定是否补发"已停止"提示


# conv_id -> 正在运行的 turn
_running: dict = {}

# conv_id -> 常驻 ClaudeSDKClient：轮次间复用同一 CLI 子进程，避免每轮冷启动。
# model / env 在 connect 时固化，运行中改 .env 需等该会话进程断开重连才生效。
_clients: dict = {}


def stop_turn(conv_id: str, by_slash: bool = False) -> bool:
    """统一停止入口（Escape 语义）：取消 turn 所在 task，
    _run_turn 的 finally 负责 interrupt 常驻客户端并收尾 UI。"""
    turn = _running.get(conv_id)
    if turn is None:
        return False
    turn.stopped_by_slash = by_slash
    turn.task.cancel()
    return True


# ---------------------------------------------------------------------------
# 每轮 options
# ---------------------------------------------------------------------------

def _conf_dir(conv: Conversation) -> str:
    """本会话的 CLAUDE_CONFIG_DIR（不存在则创建）。"""
    d = os.path.join(CC_HOME, conv.id)
    os.makedirs(d, exist_ok=True)
    return d


def workspace_dir(user_id: str) -> str:
    """本使用者的工作目录（不存在则创建，权限 0700）。

    名字里的路径分隔符等字符会被替换掉：identifier 来自登录名，不能让它
    通过 `../` 之类的内容决定目录位置。
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", user_id).strip("._") or "anonymous"
    d = os.path.join(WORKSPACES_ROOT, safe)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _skill_description(path: str) -> str:
    """取 SKILL.md frontmatter 的 description（读不出就返回空串）。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if not text.startswith("---"):
            return ""
        meta = yaml.safe_load(text.split("---", 2)[1]) or {}
        return str(meta.get("description", "")).strip()
    except Exception:
        # 单个 skill 写坏了不该让整个能力面板打不开
        return ""


def capability_catalog() -> list:
    """扫描共享能力包，返回 [{"name", "description"}]，按名字排序。

    读磁盘而不是读 init 消息：面板要在会话建立之前就能显示。
    CLI 实际加载了什么，另由 Conversation.loaded_skills 反映。
    """
    root = os.path.join(CAPABILITIES_DIR, ".claude", "skills")
    if not os.path.isdir(root):
        return []
    catalog = []
    for name in sorted(os.listdir(root)):
        skill_md = os.path.join(root, name, "SKILL.md")
        if os.path.isfile(skill_md):
            catalog.append({"name": name, "description": _skill_description(skill_md)})
    return catalog


def build_options(conv: Conversation) -> ClaudeAgentOptions:
    """每个客户端连接构建一次：允许列表自动放行，其余全部走 can_use_tool 弹窗审批。
    CLAUDE_CONFIG_DIR 按会话隔离：各会话的 transcript / memory 互不可见。
    cwd 是本使用者自己的工作目录，不是应用源码目录——使用者不该改到应用本身。
    resume 仅在新连接时生效：同一子进程内轮次天然连续。"""
    return ClaudeAgentOptions(
        cwd=conv.workspace,
        # 共享能力包按"项目级 skill"挂进来。详见 CAPABILITIES_DIR 处的说明。
        add_dirs=[CAPABILITIES_DIR],
        env={**_gateway_env(), "CLAUDE_CONFIG_DIR": _conf_dir(conv)},
        allowed_tools=_allowed_tools(),
        permission_mode=None,  # default：允许列表之外都会触发权限回调
        can_use_tool=make_can_use_tool(conv.id),
        include_partial_messages=True,  # 逐 token 流式
        resume=conv.claude_session_id,
        # 开启全部已发现的 skill；SDK 会顺带把 Skill 工具加进 allowed_tools，
        # 并在 setting_sources 未设时补 ["user", "project"]。
        skills="all",
        model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5"),
    )


async def get_client(conv: Conversation) -> ClaudeSDKClient:
    """本会话的常驻客户端：首次使用时创建并连接，后续轮次直接复用。"""
    client = _clients.get(conv.id)
    if client is not None:
        return client
    client = ClaudeSDKClient(options=build_options(conv))
    await client.connect()
    _clients[conv.id] = client
    return client


async def release_client(conv_id: str) -> None:
    """断开客户端并杀掉子进程；下一轮 get_client 凭 resume 重连续接。"""
    client = _clients.pop(conv_id, None)
    if client is not None:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def abort_client(conv_id: str) -> None:
    """停止路径（Escape 语义）：先 interrupt 让 CLI 干净收尾，再断开。
    interrupt 有界等待，CLI 无响应时直接断开（下轮 resume 续接）。"""
    client = _clients.get(conv_id)
    if client is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(client.interrupt(), timeout=10.0)
    await release_client(conv_id)


async def shutdown_all_clients() -> None:
    """应用退出时断开全部常驻子进程。"""
    for conv_id in list(_clients):
        await release_client(conv_id)


# ---------------------------------------------------------------------------
# HITL：权限回调
# ---------------------------------------------------------------------------

async def _finalize_approval(turn: TurnState, tid: str, note: str) -> None:
    """结算一条审批：从 pending 摘除、取消未决 Future、更新消息并移除按钮。"""
    item = turn.pending_approvals.pop(tid, None)
    if item is None:
        return
    fut, msg = item
    if not fut.done():
        fut.cancel()
    msg.content += f"\n\n{note}"
    await msg.remove_actions()
    await msg.update()


def make_can_use_tool(conv_id: str) -> CanUseTool:
    """构建绑定本会话的权限回调。

    常驻客户端的 options 在 connect 时固化，回调只捕获 conv_id，
    执行时从 _running 解析当前 turn（不能捕获某个 turn 的旧引用）。
    SDK 在独立 asyncio task 中调用回调；多个工具可能并发请求权限，
    Future 按 tool_use_id 区分。
    """

    async def can_use_tool(tool_name, tool_input, ctx):
        turn = _running.get(conv_id)
        if turn is None:
            # 正常不会发生（回调只在 turn 运行中触发）
            return PermissionResultDeny(message="当前没有运行中的 turn，无法审批该工具调用")
        tid = ctx.tool_use_id or uuid.uuid4().hex

        # 1) COT 里落一个 tool step，后续由 ToolResultBlock 回填 output
        step = cl.Step(name=tool_name, type="tool")
        step.input = tool_input
        await step.send()
        turn.tool_steps[tid] = step

        # 2) 审批消息（按钮挂在 Message 上，Step 上的 action 渲染无保证）
        desc = "\n".join(x for x in (ctx.title, ctx.description) if x)
        body = (
            f"🔐 权限请求：**{tool_name}**\n\n"
            f"{desc}\n\n"
            f"```json\n{json.dumps(tool_input, ensure_ascii=False, indent=2)}\n```"
        )
        msg = cl.Message(
            content=body,
            actions=[
                cl.Action(name="tool_approval", label="✅ 批准",
                          payload={"conv": turn.conv_id, "tid": tid, "decision": "allow"}),
                cl.Action(name="tool_approval", label="❌ 拒绝",
                          payload={"conv": turn.conv_id, "tid": tid, "decision": "deny"}),
            ],
        )
        await msg.send()

        # 3) 等待用户点击（app.py 的 action 回调 set_result，同一 event loop）
        fut = asyncio.get_running_loop().create_future()
        turn.pending_approvals[tid] = (fut, msg)
        try:
            decision, _ = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            await _finalize_approval(turn, tid, "⏰ 等待超时，已自动拒绝")
            return PermissionResultDeny(message="权限审批超时，该工具调用已被自动拒绝")
        except asyncio.CancelledError:
            raise  # 停止路径：UI 收尾统一交给 cleanup_turn
        await _finalize_approval(turn, tid, "✅ 已批准" if decision == "allow" else "❌ 已拒绝")
        if decision == "allow":
            return PermissionResultAllow()
        return PermissionResultDeny(message="用户拒绝了该工具的执行")

    return can_use_tool


# ---------------------------------------------------------------------------
# SDK 消息流 → Chainlit UI 映射
# ---------------------------------------------------------------------------

async def _ensure_thinking_step(turn: TurnState) -> cl.Step:
    if turn.thinking_step is None:
        turn.thinking_step = cl.Step(name="thinking", type="run")
        await turn.thinking_step.send()
    return turn.thinking_step


async def _ensure_reply(turn: TurnState) -> cl.Message:
    """回复 Message 惰性创建：首个 text 出现时才建。
    SDK 消息流里 thinking 先于 text，于是 thinking step 天然排在回复上方。"""
    if turn.reply is None:
        turn.reply = cl.Message(content="")
        await turn.reply.send()
    return turn.reply


async def _render_stream_event(m: StreamEvent, turn: TurnState):
    ev = m.event
    if ev.get("type") != "content_block_delta":
        return
    delta = ev.get("delta", {})
    if delta.get("type") == "text_delta":
        turn.text_streamed = True
        reply = await _ensure_reply(turn)
        await reply.stream_token(delta.get("text", ""))
    elif delta.get("type") == "thinking_delta":
        # 已验证本网关会逐 token 产生 thinking_delta；若某模型不产生，
        # 下方 ThinkingBlock 整块兜底仍保证顺序正确
        step = await _ensure_thinking_step(turn)
        await step.stream_token(delta.get("thinking", ""))


async def _render_assistant(m: AssistantMessage, turn: TurnState):
    for b in m.content:
        if isinstance(b, ToolUseBlock):
            # 走过 HITL 的工具回调里已建过 step，这里只为允许列表内工具补建
            if b.id not in turn.tool_steps:
                step = cl.Step(name=b.name, type="tool")
                step.input = b.input
                await step.send()
                turn.tool_steps[b.id] = step
        elif isinstance(b, TextBlock):
            # 兜底：网关不发 text_delta 时按整块写入
            if not turn.text_streamed:
                turn.text_streamed = True
                reply = await _ensure_reply(turn)
                await reply.stream_token(b.text)
        elif isinstance(b, ThinkingBlock):
            # 兜底：没有 thinking_delta 流时按整块写入
            if turn.thinking_step is None:
                step = await _ensure_thinking_step(turn)
                step.output = b.thinking
                await step.update()


async def _render_user(m: UserMessage, turn: TurnState):
    # 工具结果以 UserMessage(content=[ToolResultBlock]) 送达
    if not isinstance(m.content, list):
        return  # 注入的原始用户消息回显，忽略
    for b in m.content:
        if not isinstance(b, ToolResultBlock):
            continue
        step = turn.tool_steps.get(b.tool_use_id)
        if step is None:
            continue  # 子 agent 等未建 step 的工具结果
        step.output = b.content
        step.is_error = bool(b.is_error)
        await step.update()


async def _render_result(m: ResultMessage, turn: TurnState):
    if m.is_error:
        reply = await _ensure_reply(turn)
        with contextlib.suppress(Exception):
            await reply.stream_token(f"\n\n❌ 执行出错: {m.subtype}")
    if not (turn.reply is not None and turn.reply.content) and m.result:
        # 兜底：整轮没有任何流式输出时，展示 result 全文
        reply = await _ensure_reply(turn)
        await reply.stream_token(m.result)


async def render_message(m, conv: Conversation, turn: TurnState) -> None:
    # 凡带 session_id 的消息都取最后一次的值，保证被强杀的轮次 resume 不断链
    sid = getattr(m, "session_id", None)
    if sid:
        conv.claude_session_id = sid

    if isinstance(m, StreamEvent):
        await _render_stream_event(m, turn)
    elif isinstance(m, AssistantMessage):
        await _render_assistant(m, turn)
    elif isinstance(m, UserMessage):
        await _render_user(m, turn)
    elif isinstance(m, ResultMessage):
        await _render_result(m, turn)
    elif isinstance(m, SystemMessage) and m.subtype == "init":
        # CLI 在会话开始时报告它实际加载到的 skill / 斜杠命令。
        # 留档用于核对能力包有没有真的挂上（配置链路很容易静默失效）。
        conv.loaded_skills = list(m.data.get("skills") or [])
    # 其余 SystemMessage（task_*/hook_* 等）忽略


async def cleanup_turn(turn: TurnState, interrupted: bool) -> None:
    """turn 收尾：结算挂起审批、收口 thinking step、移出运行注册表。"""
    note = "⏹ 已取消" if interrupted else "⏹ 本轮已结束，审批未处理"
    for tid in list(turn.pending_approvals):
        with contextlib.suppress(Exception):
            await _finalize_approval(turn, tid, note)
    if turn.thinking_step is not None:
        with contextlib.suppress(Exception):
            await turn.thinking_step.update()
    _running.pop(turn.conv_id, None)
