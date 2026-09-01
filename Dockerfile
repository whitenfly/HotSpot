FROM python:3.12-slim

WORKDIR /app

# 系统依赖（lxml 编译所需，若 wheel 可用可精简）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY web/ ./web/
COPY prompts/ ./prompts/
COPY data_templates/ ./data_templates/

# 运行数据目录（可挂载卷持久化）
RUN mkdir -p /app/data

ENV HOTSPOT_PORT=3456
EXPOSE 3456

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3456/api/hotspot/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3456"]
