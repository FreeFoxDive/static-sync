FROM python:3.12-slim

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin syncer \
    && mkdir -p /data/public /data/state \
    && chown -R syncer:syncer /data

WORKDIR /app
COPY syncer/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY syncer/app /app/app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config

EXPOSE 9999
# 进程启动后若为 root，会 chown 数据目录并降权到 syncer。
CMD ["python", "-m", "app.main"]
