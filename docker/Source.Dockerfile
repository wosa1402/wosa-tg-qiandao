FROM python:3.12-slim

ARG TZ=Asia/Shanghai
ENV TZ=${TZ}
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /opt/tg-signer

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && \
    echo ${TZ} > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md README_EN.md LICENSE ./
COPY tg_signer ./tg_signer

# 可选：安装额外依赖组（例如 speedup）
# 示例：docker build --build-arg PIP_EXTRAS=speedup -f docker/Source.Dockerfile -t tg-signer:latest .
ARG PIP_EXTRAS=""
RUN pip install --no-cache-dir -U pip && \
    if [ -n "${PIP_EXTRAS}" ]; then \
      pip install --no-cache-dir ".[${PIP_EXTRAS}]"; \
    else \
      pip install --no-cache-dir .; \
    fi

ENTRYPOINT ["tg-signer"]
CMD ["--help"]

