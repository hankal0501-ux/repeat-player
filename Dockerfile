FROM python:3.11-slim

# ffmpeg + 한국어 폰트 + curl(헬스체크용)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-noto-cjk \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 복사 → 캐시 활용
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 앱 복사 (mobile/ 등 불필요한 폴더 제외는 .dockerignore)
COPY . .

# 영구 데이터 디렉터리 (Render Disk 마운트)
ENV RP_DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 5757

# Render는 PORT 환경변수를 자동 설정함
CMD ["python", "server.py"]
