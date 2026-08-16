FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config ./config
COPY dashboard ./dashboard

RUN mkdir -p /app/logs /app/data

# 헬스체크: 메인 프로세스가 상태파일을 주기적으로 갱신하는지 확인
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import time,os,sys; p='/app/data/heartbeat.txt'; \
    sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<180 else 1)"

CMD ["python", "-m", "src.main"]
