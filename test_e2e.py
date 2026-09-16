"""端到端验证（socketio 直连 + 登录 cookie），需要一个正在运行的应用实例。

覆盖：登录 → /skills 能力面板 → 非应用斜杠命令转发 → 技能真的被执行
      → 文件上传落进工作目录 → 工作目录隔离 → 自助注册。

用法:
    APP_USERS="alice:testpw123" chainlit run app.py --port 8124
    python test_e2e.py
    E2E_BASE=http://127.0.0.1:8000 E2E_USER=bob E2E_PW=xxx python test_e2e.py

注意：技能那一步会真的调用模型（几十秒），其余步骤大多只依赖本地行为。
自助注册那节（check_registration）不碰模型，可以单独跑。
"""
import os
import sys
import threading
import time
import uuid

import requests
import socketio

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8124")
USER = os.environ.get("E2E_USER", "alice")
PW = os.environ.get("E2E_PW", "testpw123")
# 与 agent.py 同名的环境变量、同样的默认值
WORKSPACES_ROOT = os.environ.get("WORKSPACES_DIR") or os.path.expanduser(
    "~/chainlit-cc-workspaces"
)

events = []
lock = threading.Lock()
results = {}


def check(name: str, ok: bool):
    results[name] = bool(ok)
    print(f"  {'✅' if ok else '❌'} {name}")


def rec(event, data=None):
    with lock:
        events.append((event, data))


def mark() -> int:
    with lock:
        return len(events)


def new_messages(since=0):
    with lock:
        return [d for e, d in events[since:] if e == "new_message" and d]


def actions_of(since=0):
    """动作走独立的 action 事件，不在 new_message 里。"""
    with lock:
        return [d for e, d in events[since:] if e == "action" and d]


def text_of(since=0):
    """回复文本可能分布在 new_message / stream_start / update_message / stream_token。

    注意：stream_token 的载荷里只有 message id、没有 step 类型，所以 thinking step
    的 token 也会被收集进来——打印出来看着像推理泄漏进正文，其实是本辅助函数的
    粗粒度导致的假象（应用里 thinking 走独立的 cl.Step）。用于子串断言没问题。
    """
    parts = []
    acc = {}
    with lock:
        chunk = list(events[since:])
    for e, d in chunk:
        if e == "stream_token":
            acc.setdefault(d.get("id"), "")
            acc[d["id"]] += d.get("token", "")
            continue
        if e in ("new_message", "update_message", "stream_start") and isinstance(d, dict):
            if d.get("type") == "assistant_message" and d.get("output"):
                parts.append(d["output"])
    parts.extend(acc.values())
    return "\n".join(parts)


def wait_task_end(since=0, timeout=240) -> bool:
    """等到本轮的 task_end。任务超时不算失败，交由具体断言判定。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            if any(e == "task_end" for e, _ in events[since:]):
                return True
        time.sleep(0.5)
    return False


def login(user=USER, pw=PW) -> str:
    s = requests.Session()
    r = s.post(f"{BASE}/login", data={"username": user, "password": pw}, timeout=15)
    assert r.status_code == 200, f"登录失败 {r.status_code}"
    cookie = s.cookies.get("access_token")
    assert cookie, "没拿到 access_token cookie"
    return cookie


def register(name: str, pw: str) -> requests.Response:
    """POST /register（自助注册端点，见 register.py）。"""
    return requests.post(
        f"{BASE}/register", json={"username": name, "password": pw}, timeout=15
    )


def send(sio, text: str, file_refs=None):
    sio.emit("client_message", {
        "message": {
            "id": uuid.uuid4().hex,  # Chainlit 断言必须是 v4 UUID
            "output": text,
            "type": "user_message",
            "name": "User",
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "fileReferences": file_refs or [],
    })


def upload(cookie: str, sid: str, name: str, content: str) -> str:
    """POST /project/file，返回服务端给的 file id。"""
    r = requests.post(
        f"{BASE}/project/file",
        params={"session_id": sid},
        files={"file": (name, content.encode(), "text/plain")},
        cookies={"access_token": cookie},
        timeout=20,
    )
    assert r.status_code == 200, f"上传失败 {r.status_code}: {r.text[:200]}"
    return r.json()["id"]


def click(cookie: str, sid: str, action) -> int:
    payload = {"sessionId": sid, "action": {
        "id": action.get("id") or uuid.uuid4().hex,
        "name": action["name"],
        "label": action.get("label", ""),
        "payload": action.get("payload", {}),
    }}
    r = requests.post(f"{BASE}/project/action", json=payload,
                      cookies={"access_token": cookie}, timeout=20)
    return r.status_code


def check_registration():
    """第 7 节：自助注册。不调用模型，可单独跑：

        python -c "import test_e2e as t; t.check_registration()"
    """
    name = "e2e-" + uuid.uuid4().hex[:8]
    check("注册返回 200", register(name, PW).status_code == 200)
    check("重名注册返回 409", register(name, PW).status_code == 409)
    check("非法用户名返回 400", register("a b", PW).status_code == 400)
    check("短密码返回 400", register(name + "x", "short").status_code == 400)
    # alice 在 APP_USERS 里，注册端不该把管理员名字让出去
    check("APP_USERS 里的名字不能注册", register(USER, PW).status_code == 409)

    # 注册接口自己种 cookie = 注册即登录，不发第二次请求
    s = requests.Session()
    auto = name + "-a"
    r = s.post(f"{BASE}/register", json={"username": auto, "password": PW}, timeout=15)
    me = s.get(f"{BASE}/user", timeout=15) if r.status_code == 200 else None
    check("注册后直接进入登录态",
          bool(me) and me.status_code == 200 and me.json().get("identifier") == auto)

    cookie = login(name, PW)
    check("注册的账号能正常登录", bool(cookie))

    m = mark()
    sid = uuid.uuid4().hex
    sio = socketio.Client()
    sio.on("*", lambda event, data=None: rec(event, data))
    sio.connect(BASE, socketio_path="/ws/socket.io",
                auth={"sessionId": sid, "clientType": "webapp"},
                headers={"Cookie": f"access_token={cookie}"},
                transports=["polling"])
    sio.emit("connection_successful")
    time.sleep(3)
    check("注册的账号能建立会话", bool(new_messages(m)))
    check("注册的账号有自己的工作目录",
          os.path.isdir(os.path.join(WORKSPACES_ROOT, name)))
    sio.disconnect()


def main() -> int:
    cookie = login()
    print(f"✅ 登录成功（{USER} @ {BASE}）")

    sid = uuid.uuid4().hex
    sio = socketio.Client()
    sio.on("*", lambda event, data=None: rec(event, data))
    sio.connect(BASE, socketio_path="/ws/socket.io",
                auth={"sessionId": sid, "clientType": "webapp"},
                headers={"Cookie": f"access_token={cookie}"},
                transports=["polling"])
    # 前端在 connect 之后会显式告知服务端，Chainlit 收到才会跑 on_chat_start
    sio.emit("connection_successful")
    time.sleep(3)
    print(f"✅ socketio 已连接；on_chat_start 发来 {len(new_messages())} 条消息")

    # ---- 1. /skills 能力面板 ----
    m = mark()
    send(sio, "/skills")
    wait_task_end(m, timeout=30)
    panel = text_of(m)
    actions = actions_of(m)
    skill_actions = [a for a in actions if a.get("name") == "skill_invoke"]
    print("\n--- /skills 面板 ---\n" + (panel[:400] or "(无输出)"))
    print("按钮:", [a["label"] for a in actions])
    check("能力面板列出 example", "example" in panel)
    check("能力面板带 skill_invoke 按钮", bool(skill_actions))

    # ---- 2. 非应用斜杠命令应转发给 CLI，而不是被本地拦成"未知命令" ----
    m = mark()
    send(sio, "/definitely-not-a-command")
    wait_task_end(m, timeout=180)
    fwd = text_of(m)
    print("\n--- /definitely-not-a-command ---\n" + (fwd[:400] or "(无输出)"))
    check("命令转发到 CLI", "Unknown command" in fwd)
    check("未被本地拦成未知命令", "未知命令" not in fwd)

    # ---- 3. 文件上传：落进工作目录，且模型真的读到了内容 ----
    ws = os.path.join(WORKSPACES_ROOT, USER)
    code = "UPLOAD-" + uuid.uuid4().hex[:12]  # 不可猜测，答对即证明真读了文件
    note = f"仓库代号：{code}\n"
    refs = [
        {"id": upload(cookie, sid, f"note-{code}.txt", note)},
        {"id": upload(cookie, sid, f"note-{code}.txt", note + "第二个同名文件\n")},
        {"id": upload(cookie, sid, "../escaped.txt", "路径型文件名\n")},
    ]
    m = mark()
    # 指名道姓只读这一个文件：同一条消息里还有两个探针文件，含糊的提问会让弱模型
    # 忙着罗列文件而答不到点上（断言就测不出"路径有没有送进去"）。
    send(sio, f"只读工作目录里的 note-{code}.txt 这一个文件，"
              f"把其中的「仓库代号」原样回复，只回复代号本身，不要解释。",
         file_refs=refs)
    wait_task_end(m, timeout=240)
    reply = text_of(m)
    print("\n--- 上传后提问 ---\n" + (reply[:600] or "(无输出)"))
    first = os.path.join(ws, f"note-{code}.txt")
    second = os.path.join(ws, f"note-{code}-1.txt")
    landed = ""
    if os.path.isfile(first):
        with open(first, encoding="utf-8") as f:
            landed = f.read()
    check("上传文件落到工作目录", landed == note)
    check("同名上传不覆盖", os.path.isfile(second))
    check("模型读到了上传内容", code in reply)
    check("路径型文件名收敛在工作目录内",
          os.path.isfile(os.path.join(ws, "escaped.txt"))
          and not os.path.exists(os.path.join(WORKSPACES_ROOT, "escaped.txt")))

    # ---- 4. 附件 + 应用内命令：明确告知不处理，而不是静默吞掉 ----
    m = mark()
    orphan = upload(cookie, sid, "orphan.txt", "不应被处理\n")
    send(sio, "/help", file_refs=[{"id": orphan}])
    wait_task_end(m, timeout=30)
    warn = text_of(m)
    check("应用内命令带附件时有提示", "不处理附件" in warn)
    check("应用内命令的附件未被复制", not os.path.isfile(os.path.join(ws, "orphan.txt")))

    # ---- 5. 点击能力按钮 → 技能真的被执行 ----
    if skill_actions:
        m = mark()
        click(cookie, sid, skill_actions[0])
        wait_task_end(m, timeout=240)
        skill = text_of(m)
        print("\n--- 能力执行输出 ---\n" + (skill[:500] or "(无输出)"))
        check("点击按钮执行了 skill", "CAPABILITY-PIPELINE-OK" in skill)
    else:
        check("点击按钮执行了 skill", False)

    # ---- 6. 工作目录按登录名隔离 ----
    check("工作目录已创建", os.path.isdir(ws))

    sio.disconnect()

    # ---- 7. 自助注册 ----
    print("\n--- 自助注册 ---")
    check_registration()

    print("\n=== 判定 ===")
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {name}")
    passed = sum(results.values())
    print(f"{passed}/{len(results)} 通过")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
