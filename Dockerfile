FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TPF_PORT=8000

WORKDIR /app

# rtree / shapely 的 wheel 自带 GEOS，但 slim 镜像仍需要这几个基础库
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY run.py ./

RUN mkdir -p data/uploads data/outputs data/state data/logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://127.0.0.1:${TPF_PORT}/api/health || exit 1

CMD ["python", "run.py"]
