# Claude Agent SDK 自带 CLI 二进制（自包含，约 216MB），镜像里不需要装 node
FROM python:3.12-slim

WORKDIR /app

# agent 会在容器里执行 Bash，不该以 root 跑
RUN useradd --create-home --uid 10001 app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 刻意不 COPY .env：密钥一律走环境变量，不进镜像层
COPY agent.py app.py auth.py db.py chainlit.md ./
COPY capabilities ./capabilities
COPY public ./public
COPY .chainlit ./.chainlit

# 聊天记录库的落点（compose 里挂 history 卷）。放在 chown 之前，让挂载点归 app
RUN mkdir -p /app/state

RUN chown -R app:app /app
USER app
ENV HOME=/home/app

EXPOSE 8000
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000"]
