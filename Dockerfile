# Dockerfile
# Minimales, gehärtetes Multi-Stage Image für das IBKR Equities Trading System

# Stage 1: Builder
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

# Systemabhängigkeiten und aktuelle Build-Tools installieren
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir --target=/install -r requirements.txt

# Stage 2: Production Runtime
FROM python:3.12-slim-bookworm AS runner

# Systemabhängigkeiten installieren, Sicherheits-Upgrades anwenden & Aufräumen
RUN apt-get update && apt-get dist-upgrade -y && apt-get install -y --no-install-recommends \
    tzdata \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Zeitzone und PYTHONPATH festlegen
ENV TZ=Europe/Berlin
ENV PYTHONPATH=/app

WORKDIR /app

# Vorinstallierte Produktions-Pakete aus Builder kopieren
COPY --from=builder /install /usr/local/lib/python3.12/site-packages/

# Python Core Tools im Runtime Image upgraden (behebt Basis-Image CVEs in pip/setuptools)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Anwendungsdateien kopieren
COPY app/ app/
COPY migrations/ migrations/
COPY config.toml.example config.toml

# Verzeichnis für persistente SQLite-Daten erstellen
RUN mkdir -p /app/data

# Einstiegspunkt ausführen
CMD ["python", "app/main.py"]
