"""HITL 权限审批的端到端验证：审批卡真的会弹、点了拒绝真的拦得住、点了批准真的执行。

审批卡走框架的 ask 通道（cl.AskActionMessage）：服务端 `sio.call("ask", ...)` 等客户端 ack，
浏览器点按钮时把那个 action 当 ack 回传。脚本没有浏览器，就在 @sio.on("ask") 里把要点的
action **return** 出去——python-socketio 的同步 Client 把 handler 的返回值当作 ack 数据。

刻意不走 POST /project/action 点审批：那条路已经不存在（tool_approval 没有 action 回调，
服务端会 404），而且它本来就绕过了前端的 disabled 判断——"审批按钮永远灰着点不动"这个 bug
正是这么漏过去的。现在脚本与浏览器走同一条通道。

与 test_e2e.py 同一套 socketio 直连手法（登录 cookie + connection_successful）。区别是这里要
等模型发起需要授权的工具调用，再用真实点击把审批结果送回 CLI，最后从**文件系统**上验证工具
到底跑没跑——只看回复文本无法区分"模型说它做了"和"工具真的执行了"。共 6 轮：批准、拒绝、
拒绝后继续、卡片在屏时停止、停止后再审批一轮（验串行锁没漏）、模型提问（AskUserQuestion
的答案回填与后果）。第 1 轮后面还插了一问，看模型怎么描述"这次是谁批准的"。

用法:
    APP_USERS="hitl:testpw123" CHAINLIT_HISTORY_DB=/tmp/hitl.db \
        chainlit run app.py --port 8124
    E2E_BASE=http://127.0.0.1:8124 E2E_USER=hitl E2E_PW=testpw123 python test_hitl.py

跑完会留下 <工作目录>/hitl-*.txt 与登录名同名的工作目录，清理见 test_e2e.py 的说明。
"""
import glob
import json
import os
import sys
import threading
import time
import uuid

import requests
import socketio

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8124")
USER = os.environ.get("E2E_USER", "hitl")
PW = os.environ.get("E2E_PW", "testpw123")
WORKSPACES_ROOT = os.environ.get("WORKSPACES_DIR") or os.path.expanduser(
    "~/chainlit-cc-workspaces"
)
WS = os.path.join(WORKSPACES_ROOT, USER)

# 应用的 CLI 状态根（agent.py 的 CC_HOME）：各会话的 transcript 在 <CC_HOME>/<会话id>/projects/。
# "人工批准告知模型"只能在这里查证——hook 注入的内容不进 SDK 的消息流，也不出现在 Chainlit 事件里。
CC_HOME = os.path.expanduser("~/.chainlit-cc")

# 模型不是确定性的：等审批卡出现要留足时间（弱模型一轮可能几十秒）
APPROVAL_WAIT_S = int(os.environ.get("E2E_APPROVAL_WAIT_S", "240"))
TURN_TIMEOUT_S = int(os.environ.get("E2E_TURN_TIMEOUT_S", "240"))
# "本轮结束"的静默窗口，见 turn_settled()
QUIET_S = float(os.environ.get("E2E_QUIET_S", "5"))

events = []
last_event_at = 0.0
lock = threading.Lock()
results = {}

# 当前这一轮要"点"的按钮。ask handler 跑在 socketio 的读线程里，靠它跨线程传值
# （CPython 里改 dict 的值是原子的）；必须在 send() 之前设好。
# ask_label 非空时优先按选项 label 点（提问卡），否则按 decision 点（审批卡）。
WANT = {"decision": "allow", "ask_label": None}

# 模拟"卡片在屏、用户还没点按钮"：同步 socketio 客户端收到事件后总会回 ack（handler 返回
# None 就发空 ack），想让 ask 悬着不答就只能让 handler 挂住不返回——浏览器里 ack 本来就是
# 点按钮那一刻才回的。
HOLD_ASK = threading.Event()
RELEASE_ASK = threading.Event()


def check(name: str, ok: bool, detail: str = ""):
    results[name] = bool(ok)
    print(f"  {'✅' if ok else '❌'} {name}{(' — ' + detail) if detail else ''}")


def rec(event, data=None):
    global last_event_at
    with lock:
        events.append((event, data))
        last_event_at = time.time()


def mark() -> int:
    with lock:
        return len(events)


def since(m: int):
    with lock:
        return list(events[m:])


def text_of(m: int) -> str:
    """把本轮所有可能的文本载体拼起来（见 test_e2e.py 的同名函数）。"""
    parts, acc = [], {}
    for e, d in since(m):
        if e == "stream_token":
            acc.setdefault(d.get("id"), "")
            acc[d["id"]] += d.get("token", "")
            continue
        if e in ("new_message", "update_message", "stream_start") and isinstance(d, dict):
            if d.get("type") == "assistant_message" and d.get("output"):
                parts.append(d["output"])
    parts.extend(acc.values())
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 审批卡与按钮
#
# ask 通道下卡片不再走 new_message（AskActionMessage 只发 ask 事件，前端收到后才把消息
# 上屏），所以卡面要从 ask 的 msg.output 里取；按钮则仍是普通的 action 事件，只是挂在
# 卡面消息上（action.forId == spec.step_id），前端就是按 spec.keys 过滤出这张卡的按钮的。
# ---------------------------------------------------------------------------

def asks(m: int) -> list:
    """本轮的 ask 事件：[{"msg": 卡面 StepDict, "spec": AskActionSpec}]。"""
    return [d for e, d in since(m) if e == "ask" and isinstance(d, dict)]


def card_of(m: int) -> str:
    """本轮第一张审批卡的正文。"""
    got = asks(m)
    return ((got[0].get("msg") or {}).get("output") or "") if got else ""


def spec_of(m: int) -> dict:
    """本轮第一张卡的 spec（step_id / keys / timeout / type）。"""
    got = asks(m)
    return (got[0].get("spec") or {}) if got else {}


def buttons_on(m: int, card: dict) -> list:
    """挂在这张卡上的按钮（服务端先发 action 事件，再发 ask 事件）。"""
    keys = set((card.get("spec") or {}).get("keys") or [])
    return [d for e, d in since(m)
            if e == "action" and isinstance(d, dict) and d.get("id") in keys]


def buttons_of(m: int) -> list:
    """挂在本轮第一张卡上的按钮。"""
    got = asks(m)
    return buttons_on(m, got[0]) if got else []


def decisions(buttons: list) -> set:
    return {(b.get("payload") or {}).get("decision") for b in buttons}


def tool_steps(m: int) -> dict:
    """本轮的 tool step：id -> 最后一次载荷。

    同一个工具调用只该有一个 step——权限回调与 _render_assistant 都会建，重复建会在 COT
    里留下两张同样的 step（先建的那张永远没 output）。归并后数量本身就是断言项。
    """
    out = {}
    for e, d in since(m):
        if e in ("new_message", "update_message") and isinstance(d, dict) \
                and d.get("type") == "tool":
            out[d.get("id")] = d
    return out


def actions_seen() -> list:
    """目前收到的全部 action 事件（handler 线程里用）。"""
    with lock:
        return [d for e, d in events if e == "action" and isinstance(d, dict)]


def socket_drops(m: int) -> int:
    """本轮里 socket 断过几次。

    断开期间服务端发出的审批卡会丢（emit 打在已断的连接上），客户端自动重连后也补不回来，
    那一轮就只能干等审批超时——看着像"审批卡没弹/功能坏了"，其实是连接断了。测试要把
    这两种情况分开报，别把偶发断连算成功能缺陷。
    """
    return sum(1 for e, _ in since(m) if e == "socket_disconnect")


def on_ask(data):
    """前端收到 ask 时把消息上屏、把 loading 置 false，用户点按钮时调 ack 回调；
    这里把这两步合成一步：记下卡面，然后 return 要点的那个按钮 = 回 ack。

    按 payload 区分两类卡：提问卡的选项按钮带 {"label": 选项原文}（按 WANT["ask_label"] 点），
    审批卡带 {"decision": ...}（按 WANT["decision"] 点）。多选题的「✅ 选好了」带 {"done": True}，
    与两者都不匹配时会回空 ack——服务端按"超时"同路处理（返回已选内容），不会误放行。

    绝不能抛异常：handler 抛异常时 socketio 不会发 ack，服务端会一直等到超时。
    """
    rec("ask", data)
    spec = data.get("spec") or {}
    if spec.get("type") != "action":
        return None  # 文本 ask（「✍️ 其他」的输入框）本脚本用不到，空 ack = 没作答
    if HOLD_ASK.is_set():
        RELEASE_ASK.wait(timeout=180)  # 挂住 = 用户还没点；释放后回空 ack（服务端多半已不等了）
        return None
    keys = set(spec.get("keys") or [])
    label, want = WANT.get("ask_label"), WANT["decision"]
    for action in actions_seen():
        p = action.get("payload") or {}
        if action.get("id") in keys and label and p.get("label") == label:
            return action
    for action in actions_seen():
        if action.get("id") in keys and (action.get("payload") or {}).get("decision") == want:
            return action  # 服务端只读 name/label/payload，原样回传即可
    return None  # 兜底：空 ack 服务端按超时同路处理（判拒绝），不会误放行


def hook_injections(since_ts: float) -> list:
    """刚才这轮里，CLI 收下的 PostToolUse 附加上下文（type=attachment 的
    attachment.type == "hook_additional_context"），按时间筛掉旧轮次留下的。

    这是"人工批准告知模型"唯一的确定性证据：它既不进 SDK 的消息流，也不出现在 Chainlit 的
    任何事件里（探针实测：同轮与下一轮模型都能凭不可猜测的代号召回，但 SDK 侧看不到这条消息）。
    """
    out = []
    for path in glob.glob(os.path.join(CC_HOME, "*", "projects", "**", "*.jsonl"),
                          recursive=True):
        try:
            if os.path.getmtime(path) < since_ts:
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if "hook_additional_context" in line:
                        out.append(json.loads(line).get("attachment") or {})
        except (OSError, ValueError):
            continue  # 正在被写的半行 / 别的进程删了文件，不该让断言崩掉
    return out


def check_stop_race() -> None:
    """停止路径的两种时序都必须退休卡片（纯逻辑：不连服务器、不叫模型）。

    真竞态在端到端里没法稳定复现——谁先到取决于 CLI 的 interrupt/断开与控制请求取消的先后，
    但两种次序都实测出现过（2026-09-17：先到的是空 ack 那一侧时，卡片没人退休，停在
    "原文 + 按钮"：点按钮 404，输入框还被 askUser 锁着）。这里把两种次序直接摆出来验。
    """
    import asyncio
    from types import SimpleNamespace

    import agent

    class FakeAction:
        def __init__(self):
            self.removed = False

        async def remove(self):
            self.removed = True

    class FakeAsk:
        """够用的替身：_retire_card/_detach_card 只用到 content / update / actions。"""

        def __init__(self):
            self.content = "🔐 权限请求：**Write**"
            self.actions = [FakeAction(), FakeAction()]

        async def update(self):
            pass

    def new_turn():
        return agent.TurnState(conv_id="race", task=None, session=SimpleNamespace(id="race"))

    async def order_cleanup_first() -> FakeAsk:
        """次序一：cleanup_turn 先收尾（换空列表 + 退休），空 ack 之后才回来。"""
        turn, ask = new_turn(), FakeAsk()
        turn.pending_asks.append(ask)
        await agent.cleanup_turn(turn, True)
        await agent._detach_card(turn, ask)
        return ask

    async def order_ack_first() -> tuple:
        """次序二：ack 先回来（摘除），cleanup_turn 之后才跑。"""
        turn, ask = new_turn(), FakeAsk()
        turn.pending_asks.append(ask)
        agent._running[turn.conv_id] = turn  # 本轮还在跑
        try:
            await agent._detach_card(turn, ask)
            left = list(turn.pending_asks)
        finally:
            agent._running.pop(turn.conv_id, None)
        return ask, left

    a1 = asyncio.run(order_cleanup_first())
    check("停止竞态：cleanup 先收尾时，晚到的 ack 也把卡片退休了",
          getattr(a1, "retired", False) and a1.content.count("⏹ 已取消") == 1
          and all(x.removed for x in a1.actions),
          repr(a1.content[-20:]))
    a2, left = asyncio.run(order_ack_first())
    check("停止竞态：ack 先回来时只摘除、不抢着退休（结论由调用方写）",
          not getattr(a2, "retired", False) and not left)


def warn_if_dropped(m: int, label: str) -> None:
    """本轮 socket 断过就点名：断连期间的事件是真丢了，别让它冒充功能缺陷。"""
    drops = socket_drops(m)
    if drops:
        print(f"  ⚠️  {label} 期间 socket 断过 {drops} 次，本轮事件缺失是断连造成的")


def wait_until(pred, timeout: int) -> bool:
    """轮询等一个条件成立（模型什么时候发起工具调用不可预测）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.5)
    return False


def wait_for_file(path: str, timeout: int = 20) -> bool:
    """等文件出现。点完批准后"工具执行"与"本轮收尾"是并发的，判定的静默窗口可能早于
    工具真正落盘，所以断言前给一小段等待。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(path):
            return True
        time.sleep(0.5)
    return False


def turn_settled(m: int, timeout: int = TURN_TIMEOUT_S, quiet: float = QUIET_S) -> bool:
    """等本轮真正结束。

    不能只看 task_end：ask 通道在等用户点按钮时先发一次 task_end（把输入框让出去）、点完
    再由 send_ask_user 的 finally 补一次 task_start（收回来），所以一轮里会有多个 task_end，
    只有最后一个是"本轮结束"。判定取"见过 task_end，且之后安静 quiet 秒没有新事件"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            has_end = any(e == "task_end" for e, _ in events[m:])
            last_at = last_event_at
        if has_end and time.time() - last_at >= quiet:
            return True
        time.sleep(0.5)
    return False


def send(sio, text: str):
    sio.emit("client_message", {
        "message": {
            "id": uuid.uuid4().hex,  # Chainlit 断言必须是 v4 UUID
            "output": text,
            "type": "user_message",
            "name": "User",
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "fileReferences": [],
    })


def login(user=USER, pw=PW) -> str:
    s = requests.Session()
    r = s.post(f"{BASE}/login", data={"username": user, "password": pw}, timeout=15)
    assert r.status_code == 200, f"登录失败 {r.status_code}"
    cookie = s.cookies.get("access_token")
    assert cookie, "没拿到 access_token cookie"
    return cookie


def write_prompt(path: str, code: str) -> str:
    """逼模型真的调工具写文件：这些工具的写语义保证会触发审批
    （只读命令如 ls/cat/echo 会被 CLI 自动放行，弹不出卡）。"""
    return (f"用工具在 {path} 创建文件，文件内容正好是这一行：{code}\n"
            f"必须真的调用工具去写，不要在回复里粘贴内容代替执行。写完只回复「完成」。")


def main() -> int:
    print("--- 停止路径的时序自检（纯逻辑）---")
    check_stop_race()

    os.makedirs(WS, exist_ok=True)
    cookie = login()
    print(f"✅ 登录成功（{USER} @ {BASE}）")

    sid = uuid.uuid4().hex
    sio = socketio.Client()
    sio.on("*", lambda event, data=None: rec(event, data))
    sio.on("ask", on_ask)  # 显式 handler 优先于 "*" 通配
    # connect / disconnect 是保留事件，不走 "*" 通配，要显式记下来
    sio.on("connect", lambda *a: rec("socket_connect", None))
    sio.on("disconnect", lambda *a: rec("socket_disconnect", None))
    sio.connect(BASE, socketio_path="/ws/socket.io",
                auth={"sessionId": sid, "clientType": "webapp"},
                headers={"Cookie": f"access_token={cookie}"},
                transports=["polling"])
    sio.emit("connection_successful")  # 不显式发这个，服务端不跑 on_chat_start
    time.sleep(3)

    # ---- 1. 批准：工具真的执行 ----
    code_allow = "ALLOW-" + uuid.uuid4().hex[:12]
    path_allow = os.path.join(WS, f"hitl-{code_allow}.txt")
    WANT["decision"] = "allow"  # 必须在 send 之前：handler 在另一个线程里读它
    m = mark()
    t0 = time.time()  # transcript 里的 hook 注入按时间筛，只认这之后的
    send(sio, write_prompt(path_allow, code_allow))
    print("\n--- 第 1 轮（自动点批准）---")
    ended = turn_settled(m, timeout=APPROVAL_WAIT_S)
    buttons = buttons_of(m)
    print(f"审批卡 {len(asks(m))} 张 / 本轮结束={ended}")
    warn_if_dropped(m, "第 1 轮")
    print("卡面：\n" + (card_of(m)[:400] or "(无)"))
    check("弹出了审批卡", bool(asks(m)))
    check("卡面带工具名与入参", "权限请求" in card_of(m) and "{" in card_of(m))
    check("卡片成对出现（批准+拒绝）", decisions(buttons) == {"allow", "deny"})
    check("按钮挂在卡面消息上（前端的渲染门）",
          bool(buttons) and all(b.get("forId") == spec_of(m).get("step_id")
                                for b in buttons))
    check("点批准后工具真的执行了", wait_for_file(path_allow))
    if os.path.isfile(path_allow):
        with open(path_allow, encoding="utf-8") as f:
            check("写入内容与请求一致", code_allow in f.read())
    steps = tool_steps(m)
    check("本轮只有一张 tool step（没有重复建）", len(steps) == 1, f"实际 {len(steps)} 张")
    check("工具结果回填到了 step 上",
          any(path_allow in (s.get("output") or "") for s in steps.values()))
    check("批准后本轮正常结束", ended)

    # ---- 1b. 人工批准有没有告知模型（用户实测的原始症状：模型把它总结成"被自动放行"）----
    # 断言落在 CLI 的 transcript 上，不落在模型的措辞上：模型怎么说不受我们控制（弱模型尤其
    # 不听话），而"注入到底发生没发生"是确定性的。下面那一问只打印出来给人看。
    inj = hook_injections(t0)
    detail = (f"{len(inj)} 条：" + json.dumps(inj[0].get("content"), ensure_ascii=False)) if inj else "0 条"
    check("人工批准以 hook 附加上下文写进了模型上下文", bool(inj), detail[:150])
    m1b = mark()
    send(sio, "刚才那次写文件的工具调用是怎么被批准的？用一句话回答。")
    settled1b = turn_settled(m1b, timeout=APPROVAL_WAIT_S)
    ans1b = text_of(m1b)
    print(f"\n--- 第 1 轮（续）本轮结束={settled1b}｜模型怎么描述「谁批准的」（人眼观察，不作断言）---")
    print((ans1b[:300] or "(无输出)"))

    # ---- 2. 拒绝：工具被拦住 ----
    code_deny = "DENY-" + uuid.uuid4().hex[:12]
    path_deny = os.path.join(WS, f"hitl-{code_deny}.txt")
    WANT["decision"] = "deny"
    m2 = mark()
    send(sio, write_prompt(path_deny, code_deny))
    print("\n--- 第 2 轮（自动点拒绝）---")
    ended2 = turn_settled(m2, timeout=APPROVAL_WAIT_S)
    print(f"审批卡 {len(asks(m2))} 张 / 本轮结束={ended2}")
    warn_if_dropped(m2, "第 2 轮")
    print("卡面：\n" + (card_of(m2)[:400] or "(无)"))
    check("拒绝了也有审批卡", bool(asks(m2)))
    check("点拒绝后文件没被创建", not os.path.exists(path_deny))
    steps2 = tool_steps(m2)
    check("被拒的那轮也只有一张 tool step", len(steps2) == 1, f"实际 {len(steps2)} 张")
    # 拒绝的语义有没有传回模型，看的是 tool_result 而不是模型的措辞：CLI 会把
    # PermissionResultDeny 的 message 原样作为 is_error 的 tool_result 喂给模型，
    # 而它同时被回填到 step.output 上。拿模型复述来断言会误判——实测弱模型在这里
    # 只吐了一串复读文本（"user asks: what was the result when..."）。
    check("拒绝以 tool_result 的形式回填（语义传回了模型）",
          any(s.get("isError") and "拒绝" in (s.get("output") or "")
              for s in steps2.values()),
          repr([(s.get("isError"), (s.get("output") or "")[:40]) for s in steps2.values()]))
    check("拒绝后本轮没有卡死（能收尾）", ended2)

    # ---- 3. 被拒之后还能继续对话：服务端没卡在 ask 状态上 ----
    m3 = mark()
    send(sio, "只回复两个字：好的")
    settled3 = turn_settled(m3, timeout=APPROVAL_WAIT_S)
    ans = text_of(m3)
    print("\n--- 拒绝后的下一轮 ---\n" + (ans[:200] or "(无输出)"))
    check("被拒后还能继续下一轮并拿到回复", settled3 and bool(ans.strip()))

    # ---- 4. 卡片还等着点击时按停止：卡该被退休、按钮该撤、输入框该解锁 ----
    code_stop = "STOP-" + uuid.uuid4().hex[:12]
    path_stop = os.path.join(WS, f"hitl-{code_stop}.txt")
    HOLD_ASK.set()  # 让 next ask 悬着不答
    m4 = mark()
    send(sio, write_prompt(path_stop, code_stop))
    print("\n--- 第 4 轮（卡片在屏时按停止）---")
    check("停止前卡片确实在等点击", wait_until(lambda: bool(asks(m4)), APPROVAL_WAIT_S))
    if asks(m4):
        send(sio, "/stop")
        # 等停止走完 interrupt + cleanup：此期间客户端读线程还挂在那张卡的 ack 上，
        # 收尾事件都排在缓冲区里，放开之后才读得到。
        time.sleep(12)
        RELEASE_ASK.set()
        time.sleep(3)
    HOLD_ASK.clear()
    RELEASE_ASK.clear()
    ended4 = turn_settled(m4, timeout=90)
    warn_if_dropped(m4, "第 4 轮")
    spec4, evs4 = spec_of(m4), since(m4)
    updates4 = [d for e, d in evs4 if e == "update_message" and isinstance(d, dict)
                and d.get("id") == spec4.get("step_id")]
    keys4 = set(spec4.get("keys") or [])
    print(f"本轮结束={ended4} / 卡面更新 {len(updates4)} 次")
    print("卡面尾部：" + repr((updates4[-1].get("output") or "")[-20:]) if updates4 else "卡面未更新")
    check("停止后卡面写上「⏹ 已取消」",
          any("⏹ 已取消" in (d.get("output") or "") for d in updates4))
    check("停止后按钮被撤掉（留着的话点了 404）",
          sum(1 for e, d in evs4 if e == "remove_action" and isinstance(d, dict)
              and d.get("id") in keys4) == 2)
    check("停止后发了 clear_ask（否则输入框一直锁着）",
          any(e == "clear_ask" for e, _ in evs4))
    check("停止后本轮收尾", ended4)
    check("停止后文件没生成", not os.path.exists(path_stop))

    # ---- 5. 停止之后再审批一轮：串行锁没被漏掉，审批链路照常 ----
    code_again = "AGAIN-" + uuid.uuid4().hex[:12]
    path_again = os.path.join(WS, f"hitl-{code_again}.txt")
    WANT["decision"] = "allow"
    m5 = mark()
    send(sio, write_prompt(path_again, code_again))
    print("\n--- 第 5 轮（停止后再走一次审批）---")
    ended5 = turn_settled(m5, timeout=APPROVAL_WAIT_S)
    print(f"审批卡 {len(asks(m5))} 张 / 本轮结束={ended5}")
    warn_if_dropped(m5, "第 5 轮")
    check("停止后审批链路照常（没被漏掉的锁卡住）", bool(asks(m5)))
    check("停止后这一轮的工具正常执行", wait_for_file(path_again))
    check("停止后这一轮正常收尾", ended5)

    # ---- 6. 模型提问（AskUserQuestion）：答案回填 + 后果可判定 ----
    # 这条链路此前是断的：AskUserQuestion 走的是通用审批卡，点批准 → allow 带回空的 answers →
    # CLI 回 "The user did not answer the questions."，模型以为用户拒答。
    code_ask = "ASK-" + uuid.uuid4().hex[:12]
    path_ask = os.path.join(WS, f"hitl-{code_ask}.txt")
    WANT["ask_label"] = "苹果"  # 提问卡上点这个选项
    WANT["decision"] = "allow"  # 后面写文件的审批卡照旧点批准
    m6 = mark()
    send(sio, (
        "第一步：必须调用 AskUserQuestion 工具问我一个问题，"
        "question 字段固定写「选择哪一项？」，options 固定给两个："
        '[{"label":"苹果","description":"红的"},{"label":"香蕉","description":"黄的"}]，'
        "multiSelect 为 false。\n"
        f"第二步：拿到我的回答后，用 Write 在 {path_ask} 写一行，"
        "内容正好是我在选项里选的那一个词，不要写别的词、不要粘贴到回复里。写完只回复「完成」。"
    ))
    print("\n--- 第 6 轮（模型提问：答案回填）---")
    ended6 = turn_settled(m6, timeout=APPROVAL_WAIT_S)
    WANT["ask_label"] = None  # 之后的轮次还要按 decision 点审批卡
    warn_if_dropped(m6, "第 6 轮")
    cards6 = asks(m6)
    # 按选项定位提问卡，不按题面：模型没照抄题面时链路照样是通的，断言不该因此误报
    qcard = next((c for c in cards6 if "苹果" in ((c.get("msg") or {}).get("output") or "")
                  and "香蕉" in ((c.get("msg") or {}).get("output") or "")), None)
    qbody = ((qcard or {}).get("msg") or {}).get("output") or ""
    print(f"本轮 ask 卡 {len(cards6)} 张 / 本轮结束={ended6}")
    print("提问卡面：\n" + (qbody[:400] or "(无)"))
    steps6 = tool_steps(m6)
    ask_step = next((s for s in steps6.values() if s.get("name") == "AskUserQuestion"), None)
    ask_out = (ask_step or {}).get("output") or ""
    print("AskUserQuestion 的 tool step 输出：" + repr(ask_out[:200]))
    check("弹出了提问卡（卡面有题面和两个选项）",
          bool(qcard) and "选择哪一项" in qbody)
    qbuttons = buttons_on(m6, qcard) if qcard else []
    qlabels = [b.get("label") for b in qbuttons]
    check("提问卡是选项按钮 +「其他」，且都挂在卡面消息上（前端的渲染门）",
          "苹果" in qlabels and "香蕉" in qlabels and any("其他" in (x or "") for x in qlabels)
          and all(b.get("forId") == (qcard.get("spec") or {}).get("step_id") for b in qbuttons),
          repr(qlabels))
    check("AskUserQuestion 只有一个 tool step（没重复建）",
          sum(1 for s in steps6.values() if s.get("name") == "AskUserQuestion") == 1)
    # 答案有没有真的回到模型：看 CLI 生成的 tool_result，不看模型措辞
    check("答案回填给了模型（有选项原文、没有 did not answer）",
          "苹果" in ask_out and "did not answer" not in ask_out, repr(ask_out[:100]))
    check("选中的答案影响了模型后续行为（文件真的写了）", wait_for_file(path_ask))
    if os.path.isfile(path_ask):
        with open(path_ask, encoding="utf-8") as f:
            written = f.read()
        print("文件内容：" + repr(written[:60]))
        check("写入内容就是被选中的那个词（不是另一个）",
              "苹果" in written and "香蕉" not in written)
    check("提问轮正常收尾", ended6)

    # 诊断用：ask 通道会让位一次 task_end，所以这个数字通常 >1
    ends = [e for e, _ in since(0) if e == "task_end"]
    print(f"\n本轮共出现 {len(ends)} 次 task_end（不能当结束信号用）")
    check("全程 socket 没断过（断连会让该轮事件凭空消失）", socket_drops(0) == 0)

    sio.disconnect()

    print("\n=== 判定 ===")
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"{sum(results.values())}/{len(results)} 通过")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
