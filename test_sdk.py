"""
Claude Agent SDK 独立测试脚本（不依赖 chainlit）
运行: python test_sdk.py "你的问题"
      python test_sdk.py            # 交互式多轮（resume 续接上下文），exit 退出
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    UserMessage,
    query,
)

# Agent 的工作目录 = 本文件所在目录
WORKDIR = os.path.dirname(os.path.abspath(__file__))

# 从同目录 .env 读取 base url / api key / model（不覆盖已有环境变量）
load_dotenv(os.path.join(WORKDIR, ".env"))

session_id: str | None = None  # 上一轮的会话 id，用于多轮对话


def build_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=WORKDIR,
        # base url / api key 显式传给 Claude Code CLI 子进程
        env={
            k: v
            for k, v in {
                "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL"),
                "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
            }.items()
            if v
        },
        # 只读工具集，安全起见先不给 Write/Bash
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="acceptEdits",   # 自动同意文件编辑
        include_partial_messages=True,   # 开启逐 token 流式输出
        resume=session_id,
        model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5"),
    )


async def ask(prompt: str) -> None:
    """发一条消息并把 SDK 返回的所有消息类型都打印出来，方便观察 API。"""
    global session_id

    async for m in query(prompt=prompt, options=build_options()):
        # ---- 逐 token 流式事件 ----
        if isinstance(m, StreamEvent):
            ev = m.event
            if (
                ev.get("type") == "content_block_delta"
                and ev.get("delta", {}).get("type") == "text_delta"
            ):
                # 文本增量：直接打到 stdout，模拟流式输出
                sys.stdout.write(ev["delta"]["text"])
                sys.stdout.flush()
            # 其余事件（message_start / content_block_start / tool_use 等）想看就取消注释：
            # else:
            #     print(f"\n[stream] {ev.get('type')}", file=sys.stderr)

        # ---- 完整消息（非流式的对照） ----
        elif isinstance(m, AssistantMessage):
            # print(f"\n[assistant] {m.content}", file=sys.stderr)
            pass
        elif isinstance(m, UserMessage):
            pass
        elif isinstance(m, SystemMessage):
            pass

        # ---- 会话结束 ----
        elif isinstance(m, ResultMessage):
            session_id = m.session_id
            print("\n" + "-" * 60, file=sys.stderr)
            print(
                f"[result] subtype={m.subtype} is_error={m.is_error} "
                f"duration={m.duration_ms}ms turns={m.num_turns} "
                f"cost=${m.total_cost_usd or 0:.4f} session={m.session_id}",
                file=sys.stderr,
            )


async def main() -> None:
    # 命令行模式: python test_sdk.py "xxx"
    if len(sys.argv) > 1:
        await ask(" ".join(sys.argv[1:]))
        return

    # 交互模式
    print("输入问题测试 Claude Agent SDK，exit 退出")
    while True:
        try:
            prompt = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if prompt.lower() in ("exit", "quit", "q"):
            break
        if not prompt:
            continue
        try:
            await ask(prompt)
        except Exception as e:
            print(f"❌ 出错: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
