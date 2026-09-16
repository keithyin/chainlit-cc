# 共享能力包

这里的 skill 会通过网页端提供给**所有使用者**（你和你的同事）。放进来的东西就是共享出去的
能力，跟着仓库走、进版本控制。

## 为什么是这个奇怪的目录结构

```
capabilities/
└── .claude/
    └── skills/
        └── <能力名>/
            └── SKILL.md
```

`.claude/skills/` 这层嵌套是 Claude Code 发现 skill 的规定路径，不能省。应用通过
`add_dirs=[capabilities/]` 把这个目录挂进每个会话，CLI 就会按"项目级 skill"扫描它。

**为什么不用 `plugins=[{"type":"local",...}]`**：实测（claude-agent-sdk 0.2.152 + 本机 CLI），
`--plugin-dir` 挂载的插件虽然出现在 init 消息的 `plugins` 字段里，但它的 skills **不会**出现在
`skills` / `slash_commands` 中，等于没生效。`add_dirs` 这条路实测有效。

**为什么不放在 `~/.claude/skills/`**：应用给每个会话设了独立的 `CLAUDE_CONFIG_DIR`（用来隔离
各会话的 transcript 和 memory），而个人 skill 目录是 `$CLAUDE_CONFIG_DIR/skills` —— 指向了
每会话的空目录，所以你本机的个人 skill 在这个应用里是**看不到**的。这是隔离的代价，也是为什么
共享能力必须放在项目里。

## 加一个能力

1. 建目录 `capabilities/.claude/skills/<能力名>/`
2. 在里面写 `SKILL.md`，开头必须是 frontmatter：

   ```markdown
   ---
   name: <能力名>
   description: <做什么 + 什么时候用，一两句话>
   ---

   正文：步骤、约定、注意事项
   ```

3. 重启服务（或让同事发 `/reload-skills`），在网页端发 `/skills` 就能看到

命名用英文小写加连字符（`weekly-report`），因为它同时是斜杠命令名。

## 怎么被调用

- **同事主动调用**：输入 `/<能力名>`，或点 `/skills` 面板上的按钮
- **模型自动调用**：当对话内容和 `description` 匹配时，模型会自己用 Skill 工具调起来

两种都要靠 `description` 写得准。写得太宽（"帮助处理文档"）会被乱触发，太窄则永远不会被想起。

## 注意

- **不是沙箱**：没被启用/没列进名单的 skill，其文件仍在磁盘上，Read/Bash 照样能读到。
  **不要往 skill 里放密钥、token、内部口令。**
- skill 目录里可以放脚本和模板，正文里用相对路径引用即可（整个 `capabilities/` 目录已挂给会话）。
- 这些文件是给模型读的指令，不是给终端执行的代码，普通 markdown 即可。
