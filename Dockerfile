# Dockerfile
# Minimales, produktionsbereites Image für das IBKR Equities Trading System

FROM python:3.12-slim

# Systemabhängigkeiten installieren (z.B. für SQLite oder Netzwerkanalyse falls nötig)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Zeitzone auf Deutschland/Berlin festlegen
ENV TZ=Europe/Berlin

WORKDIR /app

# Python-Abhängigkeiten installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Anwendungsdateien kopieren
COPY app/ app/
COPY migrations/ migrations/
COPY config.toml.example config.toml

# Verzeichnis für persistente SQLite-Daten und Log-Exporte erstellen
RUN mkdir -p /app/data

# PYTHONPATH festlegen
ENV PYTHONPATH=/app

# Einstiegspunkt ausführen
CMD ["python", "app/main.py"]
