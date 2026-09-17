"""
SDK 集成层：Claude Agent SDK 消息流 → Chainlit UI 的映射、HITL 权限回调
（含模型提问 AskUserQuestion 的选项卡与答案回填）、以及 turn 运行态
（TurnState / _running 注册表）。

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
from chainlit.context import init_ws_context
from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
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
    # 本轮所属的 Chainlit 浏览器会话（WebsocketSession）：回调里重绑上下文、审批串行锁都用它
    session: object
    tool_steps: dict = field(default_factory=dict)        # tool_use_id -> cl.Step
    pending_asks: list = field(default_factory=list)      # 正在等点击的 cl.AskActionMessage
    approved: set = field(default_factory=set)            # 被人工批准过的 tool_use_id，见 _post_tool_use_hook
    # 本轮是怎么结束的（cleanup_turn 写，晚于收尾才拿到 ack 的卡片沿用同一措辞，见 _detach_card）
    ended_note: str = "⏹ 已取消"
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
        # PostToolUse 只在工具成功执行后触发（matcher=None = 所有工具）。
        # 唯一用途见 _post_tool_use_hook：把"这次是人工批准的"告知模型。
        hooks={"PostToolUse": [HookMatcher(hooks=[_post_tool_use_hook(conv.id)])]},
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

# 浏览器会话 id -> 审批串行锁。
# 前端的 askUserState 是单值 atom：同一标签页里两张审批卡同时在场时，后一张会顶掉前一张
# 的按钮，先超时的那张还会发 ask_timeout 把状态清空——结果两张都点不动。所以同一标签页内
# 串行。键取浏览器会话而不是 Conversation：一个标签页可以同时跑两个会话的轮次；
# 也不能用全局锁，否则同事之间的审批会互相阻塞。
_ask_locks: dict = {}


def _ask_lock(session_id: str) -> asyncio.Lock:
    lock = _ask_locks.get(session_id)
    if lock is None:
        lock = _ask_locks[session_id] = asyncio.Lock()
    return lock


async def _retire_card(ask, note: str) -> None:
    """给卡片收尾（写结论 + 撤按钮），且只做一次。

    停止路径上还挂在屏的卡片如果没人退休，就会停在"原文 + 按钮"：点按钮 404（action 回调
    已随 turn 结束消失），输入框还被 askUser 锁着。而"谁先发现本轮结束"是不确定的——见
    _detach_card——所以两个入口都调这里，靠 retired 标记保证只做一次。
    """
    if getattr(ask, "retired", False):
        return
    ask.retired = True
    ask.content += f"\n\n{note}"
    with contextlib.suppress(Exception):
        await ask.update()
    # 提问卡是 AskUserMessage（文本 ask），没有 actions：getattr 兜一下
    for action in getattr(ask, "actions", None) or []:
        with contextlib.suppress(Exception):
            await action.remove()


async def _detach_card(turn: TurnState, ask) -> None:
    """卡片不再等点击了，从待退休列表里摘掉。

    多数情况就是简单摘除（本轮还在跑，收尾时 cleanup_turn 只需要管还挂在屏上的卡）。
    但有一种竞态：cleanup_turn 在停止时就把 pending_asks 整体换空了，而这张卡的 await 是
    在那之后才返回的（客户端在停止后补回一个空 ack——`send_ask_user` 的 `if user_res:`
    不成立就直接 return None，比控制请求的取消先到）。这时列表里已经没有它、cleanup_turn
    也早就跑完了，没人会再来退休它 → 在这里补上。
    """
    if _running.get(turn.conv_id) is turn:
        with contextlib.suppress(ValueError):
            turn.pending_asks.remove(ask)
        return
    await _retire_card(ask, turn.ended_note)  # 本轮已收尾，沿用 cleanup_turn 的措辞


async def _send_ask(turn: TurnState, ask) -> Optional[dict]:
    """在串行锁内发一张卡并等用户操作，返回 ack 数据（超时/空 ack 统一为 None）。

    锁只包住"一张卡在屏"的窗口，不包住整个 can_use_tool：一次问多题时若整段持锁，另一个
    会话的审批会卡在这里——它卡着时连卡都发不出去，用户看到的是"发了消息没反应"，比两种卡
    互顶更难诊断。粒度对齐前端 askUserState（单值 atom）的占用窗口。

    取消路径不摘卡片：停止时 cleanup_turn 正是靠 pending_asks 找到还挂在屏上的卡去退休
    （写结论、撤按钮、发 clear_ask），这里摘掉它就等于让那张卡烂在界面上。
    """
    async with _ask_lock(turn.session.id):
        turn.pending_asks.append(ask)
        try:
            res = await ask.send()
        except asyncio.CancelledError:
            raise
        else:
            await _detach_card(turn, ask)
            return res


async def _close_card(ask, body: str, note: str) -> None:
    """正常收尾一张卡：写回"原文 + 结论"（框架会把正文覆盖成 "**Selected:** 按钮文字"，
    超时更是英文的 "Timed out..."，题面/入参/结论就丢了），并把它标记成已定稿。

    已定稿的卡不再动——停止路径（_retire_card）可能已经先写上「⏹ 已取消」，这里再补一句
    "等待超时"就把那句话盖掉了（空 ack 比取消先到时正是这个次序）。
    """
    if getattr(ask, "retired", False):
        return
    ask.retired = True
    ask.content = f"{body}\n\n{note}"
    with contextlib.suppress(Exception):
        await ask.update()


def _parse_questions(tool_input: dict) -> Optional[list]:
    """把 AskUserQuestion 的入参收成可渲染的问题列表；拿不出可渲染的问题就返回 None。

    只归一化渲染要用的字段，不重做 schema 校验（题数 1-4、每题选项 2-4、题面唯一性由 CLI
    的 zod 挡在前面，非法入参根本到不了这里）。preview 有意不渲染（HTML 片段），但仍随
    updated_input 整体回带：updatedInput 是整体替换，少带字段模型会以为自己没给过。
    """
    questions = (tool_input or {}).get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    out = []
    for q in questions:
        text = q.get("question") if isinstance(q, dict) else None
        options = q.get("options") if isinstance(q, dict) else None
        if not isinstance(text, str) or not text:
            return None
        if not isinstance(options, list) or len(options) < 2:
            return None
        opts = []
        for o in options:
            label = o.get("label") if isinstance(o, dict) else None
            if not isinstance(label, str) or not label:
                return None
            # description 只用于卡面展示，缺了不影响答案
            opts.append({"label": label, "description": str(o.get("description") or "")})
        out.append({
            "question": text,  # 逐字节原样：它同时是回填 answers 的 key
            "header": str(q.get("header") or "问题"),
            "multiSelect": bool(q.get("multiSelect")),
            "options": opts,
        })
    return out


def _question_body(q: dict, picked: Optional[list] = None) -> str:
    """提问卡正文。题面用 ``` 包住：模型给的题面常有 markdown，裸放会被渲染成标题/列表。"""
    lines = [f"❓ **{q['header']}**", "", f"```\n{q['question']}\n```", ""]
    for i, o in enumerate(q["options"], 1):
        lines.append(f"{i}. **{o['label']}** —— {o['description']}" if o["description"]
                     else f"{i}. **{o['label']}**")
    if picked:
        lines += ["", f"已选：{'、'.join(picked)}"]
    return "\n".join(lines)


async def _ask_free_text(turn: TurnState, q: dict) -> Optional[str]:
    """「其他」：用文本 ask 收自由输入。

    必须走框架的 ask 通道（AskUserMessage）：自建 cl.Message 等回答会把 loading 原子在整个
    等待期间置真，输入框锁死（审批卡当初"按钮点不动"就是同一个坑）。空输入按未作答处理，
    CLI 那边 answers 里没有"空答案"这种值。
    """
    ask = cl.AskUserMessage(content=f"✍️ 请填写你的答案（问题：{q['question']}）",
                            timeout=APPROVAL_TIMEOUT_S)
    res = await _send_ask(turn, ask)
    return ((res or {}).get("output") or "").strip() or None


async def _ask_one(turn: TurnState, q: dict) -> Optional[str]:
    """单选题：每个选项一个按钮 +「其他」。返回 label 原文或用户自填原文，没答返回 None。"""
    body = _question_body(q)
    actions = [cl.Action(name="ask_question", label=o["label"], payload={"label": o["label"]})
               for o in q["options"]]
    actions.append(cl.Action(name="ask_question", label="✍️ 其他（自己输入）",
                             payload={"other": True}))
    ask = cl.AskActionMessage(content=body, actions=actions, timeout=APPROVAL_TIMEOUT_S)
    payload = ((await _send_ask(turn, ask)) or {}).get("payload") or {}

    if payload.get("other"):
        text = await _ask_free_text(turn, q)
        await _close_card(ask, body, f"✍️ 已填写：{text}" if text else "⏭ 未作答（没有输入内容）")
        return text
    if payload.get("label"):
        await _close_card(ask, body, f"✅ 已选：{payload['label']}")
        return payload["label"]
    # 超时 / 空 ack：按未作答回填，CLI 会给模型 "did not answer"（原生兜底，不是错误）
    await _close_card(ask, body, "⏰ 等待超时，未作答")
    return None


async def _ask_multi(turn: TurnState, q: dict) -> Optional[list]:
    """多选题：反复弹卡逐项点选，点「✅ 选好了」结束。

    Chainlit 的 ask 一次只能有一张卡、一次只能 ack 一个按钮，所以多选只能串行弹：每轮卡面
    写清"已选"、把已选项从下一张卡里去掉。一项没选就点选好了 = 未作答。
    """
    picked: list = []
    while True:
        rest = [o for o in q["options"] if o["label"] not in picked]
        body = _question_body(q, picked)
        actions = [cl.Action(name="ask_question", label=o["label"], payload={"label": o["label"]})
                   for o in rest]
        actions.append(cl.Action(name="ask_question", label="✍️ 其他（自己输入）",
                                 payload={"other": True}))
        actions.append(cl.Action(name="ask_question", label="✅ 选好了", payload={"done": True}))
        ask = cl.AskActionMessage(content=body, actions=actions, timeout=APPROVAL_TIMEOUT_S)
        payload = ((await _send_ask(turn, ask)) or {}).get("payload") or {}

        if payload.get("done"):
            await _close_card(ask, body, f"✅ 已选：{'、'.join(picked)}" if picked else "⏭ 未作答")
            return picked or None
        if payload.get("other"):
            text = await _ask_free_text(turn, q)
            await _close_card(ask, body, f"✍️ 已填写：{text}" if text else "⏭ 未作答")
            return (picked + [text]) if text else (picked or None)
        if payload.get("label"):
            # 前端的 askUserState 只有一个"当前卡"：上一张卡还没撤干净时点它的按钮，
            # ack 会落到这张新卡上，同一 label 于是可能进来两次——去重，免得答案重复
            if payload["label"] not in picked:
                picked.append(payload["label"])
            await _close_card(ask, body, f"✅ 已选：{'、'.join(picked)}")
            continue
        await _close_card(ask, body, "⏰ 等待超时，未作答")
        return picked or None


async def collect_answers(turn: TurnState, questions: list) -> dict:
    """按序把每题渲染成卡并收集答案。

    answers 的 key 是 question 逐字节原文，value 是 label 原文（多选为 list[str]）；
    未作答的题不放 key——CLI 用"key 在不在"区分没答与答了（塞哨兵值会被当成别的分支）。
    一道都没答就是 {}，CLI 会给模型 "The user did not answer the questions."（原生兜底）。
    """
    answers: dict = {}
    for q in questions:
        picked = await (_ask_multi(turn, q) if q["multiSelect"] else _ask_one(turn, q))
        if picked:
            answers[q["question"]] = picked
    return answers


def make_can_use_tool(conv_id: str) -> CanUseTool:
    """构建绑定本会话的权限回调。

    常驻客户端的 options 在 connect 时固化，回调只捕获 conv_id，
    执行时从 _running 解析当前 turn（不能捕获某个 turn 的旧引用）。
    回调可以放心停在这里等人：SDK 每个控制请求起一个独立 task，卡住不影响它的
    消息读取循环，也不影响 receive_response() 的消费。
    """

    async def can_use_tool(tool_name, tool_input, ctx):
        turn = _running.get(conv_id)
        if turn is None:
            # 正常不会发生（回调只在 turn 运行中触发）
            return PermissionResultDeny(message="当前没有运行中的 turn，无法审批该工具调用")

        # SDK 的回调 task 继承的是"首次 connect 那一轮"的 Chainlit 上下文快照，而浏览器
        # 刷新后 Chainlit 换了新 session、_clients 仍按 conv_id 复用旧 client——不重绑的话，
        # 下面发出去的审批卡与 tool step 都会打进已断开的 socket（界面看不见，只能干等超时）。
        # 必须赶在建 Step/Message 之前。
        init_ws_context(turn.session)

        tid = ctx.tool_use_id or uuid.uuid4().hex

        # 1) COT 里落一个 tool step，后续由 ToolResultBlock 回填 output。
        # 通常 AssistantMessage 里的 ToolUseBlock 先到（_render_assistant 已按真实
        # tool_use_id 建过 step），再轮到本回调；无条件再建一个会在 COT 里留下两张
        # 同样的 step，而且注册表被覆盖后结果只回填到后建的那张，先建的那张永远是空的。
        step = turn.tool_steps.get(tid)
        if step is None:
            step = cl.Step(name=tool_name, type="tool")
            step.input = tool_input
            await step.send()
            turn.tool_steps[tid] = step

        # 2) 模型提问（AskUserQuestion）要的不是权限，是答案——答案只能靠 updated_input 回填：
        # CLI 拿 updatedInput 整体替换 input 并用它重跑工具，而 allow 结果本身没有任何文本字段。
        # 少了这个分支，allow 会原样回填空的 input（answers 恒为 {}），CLI 于是回
        # "The user did not answer the questions."，模型误以为用户拒答。
        if tool_name == "AskUserQuestion":
            questions = _parse_questions(tool_input)
            if questions is None:
                # 不退回审批卡：那张卡只能放行/拒绝，放行后同样落到"没人作答"，模型会换个问法
                # 再问一次。deny 的 message 才让它知道该修入参。
                return PermissionResultDeny(
                    message="AskUserQuestion 的入参无法渲染成提问卡（缺 questions 或选项少于两个），"
                            "请修正入参后重试"
                )
            answers = await collect_answers(turn, questions)
            # 超时/中止也按 allow 返回已收集到的答案（可能为 {}）：对齐 CLI 原生的 AFK 语义
            # （"No response — proceed using your best judgment"），不是错误。
            return PermissionResultAllow(updated_input={**tool_input, "answers": answers})

        # 3) 其余工具走审批卡。用框架的 ask 通道，而不是自建 Message + action 回调：自建卡片
        # 的按钮会被前端的 loading 原子卡死——socket 的 process_message 是 task_start() →
        # on_message() → task_end()，而等点击正好发生在 on_message 内部，于是整段等待期间
        # loading 恒为 true，ActionButton 的 disabled={loading || isRunning} 让它永远点不动。
        # ask 事件会让前端显式 setLoading(false)，按钮才可点。
        desc = "\n".join(x for x in (ctx.title, ctx.description) if x)
        body = (
            f"🔐 权限请求：**{tool_name}**\n\n"
            f"{desc}\n\n"
            f"```json\n{json.dumps(tool_input, ensure_ascii=False, indent=2)}\n```"
        )
        ask = cl.AskActionMessage(
            content=body,
            actions=[
                cl.Action(name="tool_approval", label="✅ 批准",
                          payload={"decision": "allow"}),
                cl.Action(name="tool_approval", label="❌ 拒绝",
                          payload={"decision": "deny"}),
            ],
            # 超时（socketio TimeoutError）与 ack 空数据都归一到返回 None，下面统一按拒绝处理
            timeout=APPROVAL_TIMEOUT_S,
        )
        res = await _send_ask(turn, ask)

        decision = ((res or {}).get("payload") or {}).get("decision")

        # 卡面写回原文 + 结论（见 _close_card）：这张卡是本次审批的审计记录，入参另有一份
        # 在 COT 的 tool step 上。
        note = {"allow": "✅ 已批准", "deny": "❌ 已拒绝"}.get(
            decision, "⏰ 等待超时，已自动拒绝"
        )
        await _close_card(ask, body, note)

        if decision == "allow":
            # 记下来给 PostToolUse hook 用：allow 结果没有文本字段，模型只看得到工具输出，
            # 会把"人工批准"误当成"自动放行"（现场实测它就是这么总结的）。
            turn.approved.add(tid)
            return PermissionResultAllow()
        if decision == "deny":
            return PermissionResultDeny(message="用户拒绝了该工具的执行")
        return PermissionResultDeny(message="权限审批超时，该工具调用已被自动拒绝")

    return can_use_tool


# 人工批准的告知语。allow 结果没有文本通道，PostToolUse hook 的 additionalContext 是唯一
# 正规的告知通道（CLI 会把它包成一条 isMeta 的 <system-reminder> 消息写进上下文）。
_APPROVED_NOTE = (
    "网页界面的提示：这次工具调用不是自动放行的，是用户在审批卡上人工点了「批准」才执行的。"
    "请把它当作「用户已看过这次操作并同意」的信号，不要声称自己无法获得用户确认。"
)


def _post_tool_use_hook(conv_id: str):
    """构建 PostToolUse hook：给刚被人工批准过的调用补一句"这是人批的"。

    只认 tool_use_id（全局唯一），自动放行的工具不在集合里、不插话；被拒/失败的调用
    PostToolUse 根本不触发（失败走 PostToolUseFailure），所以不必再判 is_error。
    回调虽跑在 SDK 读循环 spawn 的独立 task 里，这里只做 dict 操作、不碰 Chainlit 上下文。
    """

    async def hook(hook_input, tool_use_id, _ctx):
        tid = tool_use_id or (hook_input or {}).get("tool_use_id")
        turn = _running.get(conv_id)
        if turn is None or not tid or tid not in turn.approved:
            return {}
        turn.approved.discard(tid)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": _APPROVED_NOTE,
            }
        }

    return hook


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
    """turn 收尾：退休还挂在屏上的审批卡/提问卡、收口 thinking step、移出运行注册表。"""
    note = "⏹ 已取消" if interrupted else "⏹ 本轮已结束，审批未处理"
    turn.ended_note = note  # 晚到的卡片（_detach_card）沿用同一措辞
    # 整体换空：回调侧 _detach_card 拿到的是旧列表，不会和这里重复处理同一张卡
    asks, turn.pending_asks = turn.pending_asks, []
    for ask in asks:
        # 等点击的 task 随客户端断开被取消，AskActionMessage.send 走不到自己的收尾，
        # 卡片会停在"原文 + 两个按钮"。按钮必须撤：tool_approval 已经没有 action 回调了，
        # 前端再点它只会拿到 404。
        await _retire_card(ask, note)
    if asks:
        with contextlib.suppress(Exception):
            # 前端收到 ask 时 askUser 有值，输入框被 useChatData().disabled 锁着；不清掉的
            # 话，用户点完停止还得刷新页面才能继续发消息。
            # （被取消的 send_ask_user 会在 finally 补发一次 task_start——它把
            # task_end/task_start 当"让出/收回输入框"用。正常时序下那发生在 abort_client 的
            # interrupt 期间、早于这里；只有极端迟到才会让输入框保持禁用，刷新即恢复。）
            await cl.context.emitter.clear("clear_ask")
    if turn.thinking_step is not None:
        with contextlib.suppress(Exception):
            await turn.thinking_step.update()
    _running.pop(turn.conv_id, None)
    if not any(t.session.id == turn.session.id for t in _running.values()):
        _ask_locks.pop(turn.session.id, None)  # 该标签页没有别的轮次了，锁不必留着
