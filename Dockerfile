FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEVIN_PROXY_DB=/data/devin-proxy.db

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY devin_proxy ./devin_proxy

RUN useradd -m app && mkdir -p /data && chown app:app /data
USER app
VOLUME /data

EXPOSE 8317
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8317/healthz', timeout=8)"]

CMD ["python", "-m", "devin_proxy", "--host", "0.0.0.0", "--port", "8317"]
