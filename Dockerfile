# 已迁移：请使用仓库根下 deploy/docker/backend.Dockerfile
# docker compose -f deploy/docker-compose.yml 会自动引用。
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    MEDIAMTX_VERSION=v1.19.3
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip curl ca-certificates ffmpeg \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -U pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
        -i https://pypi.tuna.tsinghua.edu.cn/simple
COPY . .
RUN mkdir -p tools/mediamtx \
    && if [ ! -f tools/mediamtx/mediamtx ]; then \
         curl -fsSL \
           "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz" \
           | tar -xz -C tools/mediamtx mediamtx; \
       fi \
    && chmod +x tools/mediamtx/mediamtx
RUN mkdir -p /var/gcms/captures /var/gcms/mobile_ftp uploads/mobile && chmod +x startup.sh || true
EXPOSE 8080 8082 8889 8554
CMD ["python3", "main.py"]
