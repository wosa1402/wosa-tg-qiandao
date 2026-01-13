FROM python:3.12-slim

ARG TZ=Asia/Shanghai
ENV TZ=${TZ}
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && \
    echo ${TZ} > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md README_EN.md LICENSE ./
COPY tg_signer ./tg_signer
COPY docs ./docs

RUN pip install -U pip && \
    pip install ".[web]"

ENV HOST=0.0.0.0
ENV PORT=8000
EXPOSE 8000

CMD ["tg-signer-web"]

