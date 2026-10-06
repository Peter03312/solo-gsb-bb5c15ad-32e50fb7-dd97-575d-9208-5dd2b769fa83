FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 安装依赖（一键启动时完成）
COPY requirements.txt .
RUN pip install -r requirements.txt

# 拷贝应用代码
COPY app ./app

# SQLite 数据目录（可挂载卷持久化）
RUN mkdir -p /app/data
ENV DATABASE_PATH=/app/data/app.db \
    SEED_DEMO_DATA=1

EXPOSE 8000

# 启动时自动初始化数据库结构与演示数据
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
