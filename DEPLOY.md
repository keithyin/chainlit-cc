# 部署：把网页端分享给同事

面向**部署者**（你），不是使用者。使用者看应用里的 `/help`。

## 分享出去之前必须做的三件事

1. **设 `APP_USERS`**（`.env`）——引导/管理员账号，建议留着。
   格式 `名字:密码,名字:密码`，例如：

   ```
   APP_USERS="alice:一个只有你知道的密码,bob:给同事的密码"
   ```

   登录名同时决定各人的工作目录：`~/chainlit-cc-workspaces/<登录名>/`。
   同事也可以自己在登录页注册（见第 3 条），但**它是唯一不随聊天记录库一起丢失的凭据**：
   误删了 `chainlit_history.db` 时，只剩下它能进得去。一个都不设的话，
   库里没有账号就没人能登录了。

2. **确认 `CHAINLIT_AUTH_SECRET` 已设**（`.env` 里已生成一个）。
   改动它会让所有已登录的人退出登录。

3. **认清你分享出去的是什么**：使用者会以**你的 API key**、在一台**你能执行命令的机器**上
   让模型跑工具。`Read`/`Glob`/`Grep` 是免审批的，也就是说同事可以让 agent 读这台机器上
   任何进程有权读的文件。

   **而且注册是全开放的**：任何能打开 `/public/register.html` 的人都能给自己开一个账号，
   然后立刻获得上面这些能力。这比"只分享给信得过的人"宽得多——**暴露到公网等于把 API key
   给公网**。要么待在 VPN / 零信任网关 / 反向代理的认证之后，要么别开。
   （端点本身没有限流也没有验证码；真要公开，至少在 nginx 上加 `limit_req`。
   要关掉注册：注释掉 `app.py` 里的 `import register` 那行，重启。）

## 本机跑（只给自己用）

```sh
chainlit run app.py
```

## 容器跑

```sh
# .env 里配好 APP_USERS / CHAINLIT_AUTH_SECRET / ANTHROPIC_API_KEY
docker compose up -d --build
```

- 端口默认只绑 `127.0.0.1:8000`，外面访问不到——这是故意的。
- 网关在宿主机上时，compose 里用 `GATEWAY_URL` 指定（默认 `host.docker.internal:15721`）。
  注意不能直接用 `.env` 里的 `ANTHROPIC_BASE_URL`：那里的 `127.0.0.1` 在容器内指向容器自己。
- 两个卷：`cc-state`（会话 transcript/memory）、`workspaces`（同事的工作文件）。

## 让同事访问到

要暴露到网络，**前面必须有一层带 TLS 的反向代理**，并且：

- 只暴露 443，应用端口不要直接对公网开
- 反向代理要正确转发 WebSocket（Chainlit 走 `/ws/socket.io`），
  否则界面能开但一发消息就断
- 有内网 VPN / 零信任网关的话，优先走那条，别直接开公网

nginx 的关键两行：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;   # agent 跑长任务时别被掐断
}
```

## 收紧（多人使用时建议做）

- **权限**：`ALLOWED_TOOLS` 越窄越安全（默认 `Read,Glob,Grep`，写操作和 Bash 全走审批）。
  放开什么就审批什么，别整体放开 `Bash`。
- **沙箱**：SDK 支持 `sandbox` 选项（限制文件系统与网络），还没接。见 README 的路线图。
- **成本**：`max_budget_usd` / `max_turns` 还没设，同事的消耗没有上限。
- **工作目录**：每人一个目录已隔离文件，但 `Read` 免审批意味着彼此的工作文件
  （以及上传的文件，都落在各自工作目录里）在文件系统层面仍可互读。真正的强隔离要一人一个容器。
- **上传**：`.chainlit/config.toml` 里当前是 `accept = ["*/*"]`、20 个文件、单个 500MB，
  同事能往你的磁盘上写大文件。按需收紧 `accept`（MIME）与 `max_size_mb`。
- **撤账号**：自注册账号没有管理界面，只能手工删行
  （`sqlite3 chainlit_history.db "delete from user_accounts where username='bob'"`），
  删掉后那个人就登录不了，工作目录和聊天记录还在。
  从 `APP_USERS` 里摘名字也一样，**而且必须连库里的同名行一起删**：
  名字不在 `APP_USERS` 里时，同名的自注册账号会重新生效。

## 聊天记录持久化

会话状态与聊天记录存在 SQLite 里（`db.py`），默认路径是项目目录下的
`chainlit_history.db`，容器里由 `CHAINLIT_HISTORY_DB` 指向 `/app/state/`（挂在
`history` 命名卷上）。它让刷新/重连能接回原来的会话，也让侧栏能列出历史线程。

**自注册账号也在这个文件里**（`user_accounts` 表，存的是 scrypt 哈希）：

- 备份它就是备份了所有人的账号；拷走它等于拷走账号（哈希本身算不出来，但别乱传）
- 删掉它，聊天记录和自注册账号一起清零；`APP_USERS` 里的引导账号不受影响
- 轮换 `CHAINLIT_AUTH_SECRET` 只会让所有人重新登录，不影响已存的口令
- 数据层要求**单进程**运行：会话、CLI 客户端、运行态都在进程内，SQLite 也有文件锁。
  不要给 uvicorn 加 `--workers`。
- 备份就是拷这一个文件；删掉它等于清空全部聊天记录（CLI 侧的 transcript 不受影响，
  仍在 `cc-state` 卷里）。

## 还没做的（Phase 2/3）

成本展示、diff 视图、审批策略记忆、MCP、hooks、plan mode、sandbox。
