# Dockerfile
# Gehärtetes Multi-Stage Image für das IBKR Equities Trading System

# ── Stage 1: Builder ──
FROM python:3.12-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ──
FROM python:3.12-slim-bookworm

# Sicherheits-Upgrades + tzdata, verwundbare System-Pakete entfernen
RUN apt-get update && apt-get dist-upgrade -y \
    && apt-get install -y --no-install-recommends tzdata \
    && apt-get purge -y --auto-remove \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* \
              /usr/local/lib/python3.12/site-packages/{pip,pip-*,setuptools,setuptools-*,wheel,wheel-*,pkg_resources}

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    TZ=Europe/Berlin \
    PYTHONPATH=/app

WORKDIR /app
COPY app/ app/
COPY migrations/ migrations/
COPY config.toml.example config.toml
RUN mkdir -p /app/data

CMD ["python", "app/main.py"]
