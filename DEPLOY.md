# 部署：把网页端分享给同事

面向**部署者**（你），不是使用者。使用者看应用里的 `/help`。

## 分享出去之前必须做的三件事

1. **设 `APP_USERS`**（`.env`）。不设则**所有登录都被拒绝**——这是刻意的安全默认。
   格式 `名字:密码,名字:密码`，例如：

   ```
   APP_USERS="alice:一个只有你知道的密码,bob:给同事的密码"
   ```

   登录名同时决定各人的工作目录：`~/chainlit-cc-workspaces/<登录名>/`。

2. **确认 `CHAINLIT_AUTH_SECRET` 已设**（`.env` 里已生成一个）。
   改动它会让所有已登录的人退出登录。

3. **认清你分享出去的是什么**：使用者会以**你的 API key**、在一台**你能执行命令的机器**上
   让模型跑工具。`Read`/`Glob`/`Grep` 是免审批的，也就是说同事可以让 agent 读这台机器上
   任何进程有权读的文件。只分享给信得过的人，或者按下面的"收紧"一节做隔离。

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

## 聊天记录持久化

会话状态与聊天记录存在 SQLite 里（`db.py`），默认路径是项目目录下的
`chainlit_history.db`，容器里由 `CHAINLIT_HISTORY_DB` 指向 `/app/state/`（挂在
`history` 命名卷上）。它让刷新/重连能接回原来的会话，也让侧栏能列出历史线程。

- 数据层要求**单进程**运行：会话、CLI 客户端、运行态都在进程内，SQLite 也有文件锁。
  不要给 uvicorn 加 `--workers`。
- 备份就是拷这一个文件；删掉它等于清空全部聊天记录（CLI 侧的 transcript 不受影响，
  仍在 `cc-state` 卷里）。

## 还没做的（Phase 2/3）

成本展示、diff 视图、审批策略记忆、MCP、hooks、plan mode、sandbox。
