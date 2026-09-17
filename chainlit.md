# Claude Code 能力网页版

把 Claude Code 的能力做成网页：同事打开浏览器登录就能用，不需要装 CLI。

- **流式输出**：正文逐 token 流式，工具调用以 COT step 展示（input / output）
- **共享能力包**：`capabilities/` 里的 skill 对所有使用者可用，发 `/skills` 看清单、一键调用
- **Human-in-the-loop**：`ALLOWED_TOOLS` 允许列表内的工具直接执行，其余弹出审批卡片（批准 / 拒绝）。
  注意：CLI 内置的只读 Bash 命令（`ls`/`cat`/`echo` 等）会被 CLI 自动放行、不进入审批流；
  写操作（重定向、`touch`、`rm` …）才会弹出审批。
  卡片走框架的 ask 通道，同一标签页同一时间只有一张卡；等审批时输入框是锁住的，得先在卡片上选一个。
  超时（默认 300 秒，见 `APPROVAL_TIMEOUT_S`）自动按拒绝处理，卡面会写明结论。
  批准过的工具，Claude 会收到一条 `<system-reminder>` 提示知道"这是人工批准、不是自动放行"
  （allow 结果本身没有文本通道，只能靠 PostToolUse hook 的 additionalContext 告知）。
- **Claude 提问**：Claude 调 `AskUserQuestion` 时弹提问卡——单选每个选项一个按钮，多选连弹几张、
  点「✅ 选好了」结束，两种都能点「✍️ 其他」自己输入。选中的答案会原样回填给 Claude
  （超时按"未作答"回填，对齐 CLI 原生的 AFK 语义）。
  **别把 `AskUserQuestion` 写进 `ALLOWED_TOOLS`**：那样 CLI 会直接放行、不走本应用的权限回调，
  提问会以"无人作答"收场。
- **中断**：流式期间输入框的停止按钮（等价 CLI 的 Escape），或发送 `/stop`
- **独立工作目录**：每个人一个目录（`~/chainlit-cc-workspaces/<登录名>/`），互不干扰
- **文件上传**：输入框回形针上传的文件会放进你的工作目录，Claude 按路径读取
- **多会话**：同一人的各会话上下文与 memory 完全隔离
- **自助注册**：登录页下方的「注册」链接（或直接打开 `/public/register.html`），注册完自动登录

## 命令

| 命令 | 说明 |
|---|---|
| 直接发消息 | 在激活会话中执行任务 |
| `/skills` | 共享能力面板（列出能力包，一键调用） |
| `/new [标题]` | 新建会话并切换 |
| `/sessions` | 列出会话，点选切换 / 停止 |
| `/stop` | 停止激活会话正在运行的任务 |
| `/help` | 显示帮助 |

其余以 `/` 开头的输入会原样交给 Claude Code：

- CLI 内置命令：`/compact`、`/context`、`/cost` 等
- 共享能力包里的 skill：`/<能力名>`

## 加一个共享能力

在 `capabilities/.claude/skills/<能力名>/SKILL.md` 写一个带 frontmatter 的 skill 即可，
写法见 `capabilities/README.md`。放进那个目录就是对所有使用者生效。

## 账号

在登录页点「注册」自助建号（没有审批，谁都能注册），注册成功即自动登录。
之后可以在登录页用同一个用户名密码登录。

- 用户名 2-32 位，只能用字母、数字和 `_ . -`，且首尾必须是字母或数字；区分大小写
- 密码至少 8 位
- 用户名同时决定工作目录：`~/chainlit-cc-workspaces/<用户名>/`
- 没有邮箱验证、没有找回密码、没有管理界面。账号存在聊天记录那个 SQLite 库里
  （`user_accounts` 表，口令用 scrypt 加盐哈希）——删掉那个库，自注册账号就一起没了
- `APP_USERS` 里的名字优先级最高，也注册不了（会提示已被占用），
  它是部署者留的引导账号（见 `DEPLOY.md`）

## 配置（`.env`）

| 变量 | 说明 |
|---|---|
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | 模型网关连接 |
| `ALLOWED_TOOLS` | 工具允许列表（逗号分隔，支持 `Bash(git log:*)` 规则语法） |
| `APP_USERS` | 引导/管理员账号（`名字:密码,名字:密码`），可选。**它优先于自注册账号**；两个来源都没有凭据时拒绝所有登录 |
| `CHAINLIT_AUTH_SECRET` | 会话签名密钥，改动会让所有人退出登录 |
| `APPROVAL_TIMEOUT_S` | 审批等待超时秒数（默认 300） |
| `WORKSPACES_DIR` | 每使用者工作目录根（默认 `~/chainlit-cc-workspaces`） |

分享给同事的部署步骤见 `DEPLOY.md`。

## 运行

```bash
chainlit run app.py
```

浏览器打开 http://localhost:8000。

端到端自检（需要应用已在跑，且该实例的 `APP_USERS` 里有测试账号）：

```bash
APP_USERS="alice:testpw123" chainlit run app.py --port 8124   # 另开一个终端
python test_e2e.py
```

覆盖登录、自助注册、能力面板、命令转发、能力调用、文件上传、工作目录隔离。
其中能力调用与上传提问会真的走模型，整体约几分钟。

审批卡（HITL）单独一套：批准 / 拒绝 / 被拒后继续 / 卡片在屏时停止 / 停止后再审批一轮，
会真的等审批弹卡再自动点按钮，并从文件系统上核对工具到底跑没跑（约几分钟，含 5 轮模型调用）。

```bash
APP_USERS="hitl:testpw123" CHAINLIT_HISTORY_DB=/tmp/hitl.db \
  chainlit run app.py --port 8124   # 另开一个终端（和上一条同一个端口，别同时跑）
E2E_BASE=http://127.0.0.1:8124 E2E_USER=hitl E2E_PW=testpw123 python test_hitl.py
```

脚本用 socketio 直连收发（无浏览器），所以点不到 DOM 上的禁用态——「按钮可点」这件事仍要人眼确认。

## 实现要点

- 会话列表存于进程内存（`cl.user_session`），应用重启后丢失；
  各会话的 CLI 状态（transcript / memory）保留在 `~/.chainlit-cc/<会话id>/`，不会串到其他会话。
- 应用的 CLI 子进程不读写开发者真实的 `~/.claude`（通过 `CLAUDE_CONFIG_DIR` 隔离到 `~/.chainlit-cc/`）。
  **代价**：你个人 `~/.claude/skills/` 里的能力在这个应用里看不到——所以共享能力必须放在
  `capabilities/`（应用通过 `add_dirs` 挂载它）。
- 每个会话保持一个常驻 CLI 子进程，轮次间复用（无每轮冷启动）；
  停止（`/stop` / 停止按钮）以 Escape 语义中断，下一轮凭 resume 续接；
  运行中修改 `.env` 对已连接的会话不生效（需该会话断开重连）；应用退出时全部断开。
- Agent 的工作目录是**使用者自己的工作目录**，不是应用源码目录。
- 自注册账号走独立的一张表（`user_accounts`，见 `db.py` / `accounts.py`），口令用 stdlib
  的 `scrypt` 加盐哈希，不引第三方依赖。没有塞进 Chainlit 的 `users` 表：那张表的
  metadata 每次登录都会被覆盖，还会随 `GET /user` 发给浏览器。
- 注册端点是挂在 Chainlit 自己的 FastAPI app 上的 `POST /register`（`register.py`），
  注册页是 `public/register.html`；登录页那个「注册」链接由 `public/ui.js` 注入
  （Chainlit 的登录页没有注册钩子，只能这样挂）。
