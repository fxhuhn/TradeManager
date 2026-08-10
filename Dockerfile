# Dockerfile
# Minimales, gehärtetes Multi-Stage Image für das IBKR Equities Trading System

# Stage 1: Builder
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

# Build-Abhängigkeiten installieren
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Virtuelle Umgebung erstellen & upgraden
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

# Stage 2: Production Runtime
FROM python:3.12-slim-bookworm AS runner

# Systemabhängigkeiten installieren, Sicherheits-Upgrades anwenden & Aufräumen
RUN apt-get update && apt-get dist-upgrade -y && apt-get install -y --no-install-recommends \
    tzdata \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Virtuelle Umgebung aus Builder kopieren
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# System-Python Pakete entfernen (behebt CVE-2025-47273 in setuptools 70.3.0)
RUN pip install --no-cache-dir pip --target /tmp/pip-tmp \
 && /tmp/pip-tmp/pip uninstall -y pip setuptools wheel 2>/dev/null; \
    rm -rf /tmp/pip-tmp /usr/local/lib/python3.12/site-packages/pip* \
           /usr/local/lib/python3.12/site-packages/setuptools* \
           /usr/local/lib/python3.12/site-packages/wheel* \
           /usr/local/lib/python3.12/site-packages/pkg_resources

# Zeitzone und PYTHONPATH festlegen
ENV TZ=Europe/Berlin
ENV PYTHONPATH=/app

WORKDIR /app

# Anwendungsdateien kopieren
COPY app/ app/
COPY migrations/ migrations/
COPY config.toml.example config.toml

# Verzeichnis für persistente SQLite-Daten erstellen
RUN mkdir -p /app/data

# Einstiegspunkt ausführen
CMD ["python", "app/main.py"]
