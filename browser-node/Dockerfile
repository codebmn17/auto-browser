FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    fluxbox \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxkbcommon0 \
    novnc \
    procps \
    socat \
    websockify \
    x11vnc \
    xvfb \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright==1.52.0 \
  && playwright install chromium

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5900 6080 9222 9223

CMD ["/entrypoint.sh"]
