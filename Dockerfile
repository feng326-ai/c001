# Python 基础镜像 (官方 slim-bookworm tag，注意 tag 顺序为 variant-suite)
FROM python:3.11-slim-bookworm

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TZ=Asia/Shanghai

# 工作目录（与 docker-compose 的 /app/wxsearch、/app/logs 挂载对齐）
WORKDIR /app

# 系统依赖：postgresql-client 提供 pg_dump/pg_restore，供数据备份任务使用
# （bookworm 默认 v15，与 postgres:15 服务端兼容）。装完清理 apt 缓存缩小镜像。
RUN echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4 \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 复制项目代码
COPY ./wxsearch ./wxsearch
COPY ./docs ./docs

# 创建日志目录
RUN mkdir -p /app/logs

# 暴露端口
EXPOSE 8000

# 健康检查（shell 形式，运行时读取 DATABASE_URL）
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import os,psycopg2; psycopg2.connect(os.environ['DATABASE_URL'])" || exit 1

# 启动命令 (由 docker-compose 覆盖)
CMD ["python", "-m", "wxsearch.api.main"]
