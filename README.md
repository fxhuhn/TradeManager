# TradeManager - IBKR Equities Trading System

[![pytest](https://github.com/fxhuhn/TradeManager/actions/workflows/pytest.yml/badge.svg)](https://github.com/fxhuhn/TradeManager/actions/workflows/pytest.yml)
[![quality_security](https://github.com/fxhuhn/TradeManager/actions/workflows/quality_security.yml/badge.svg)](https://github.com/fxhuhn/TradeManager/actions/workflows/quality_security.yml)

TradeManager ist ein automatisiertes End-of-Day (EOD) Handelssystem für US-Aktien über die Trader Workstation (TWS) oder das IB Gateway von Interactive Brokers (IBKR).

Das System basiert vollständig auf asynchronem Python (`asyncio` und `ib_async`) und ist für Robustheit, Fehlertoleranz und eine saubere Trennung von Business-Logik (Functional Core) und System-Schnittstellen (Imperative Shell) konzipiert.

---

## Projektstruktur

```text
TradeManager/
├── .github/workflows/    # CI/CD Pipelines (Build, Tests, Ruff)
├── app/                  # Quellcode der Hauptanwendung
│   ├── cli/              # CLI-Diagnosetools (Systemstatus & Überwachung)
│   ├── core/             # Konfiguration, Logging, Datenbankverbindung, Datenmodelle
│   ├── services/         # CSV Watcher, Alert Watcher, Telegram-Benachrichtigungen, Kontometriken
│   ├── trading/          # Order-Generierung, Worker-Schleifen, TWS-Callbacks, Settlement
│   └── main.py           # Haupteinstiegspunkt
├── data/                 # SQLite-Datenbank, Logdateien (lokal ignoriert)
├── doc/                  # PDF-Konzept und ausführliches Nutzerhandbuch
├── migrations/           # SQL-Datenbankmigrationen
├── scripts/              # Hilfs- und Diagnose-Skripte (z. B. TWS-Verbindungstest)
├── tests/                # Unittests und Systemsimulationen
├── Dockerfile            # Docker-Image-Definition
├── docker-compose.yml    # Docker-Compose für Containerisierung
├── config.toml           # Strukturelle Parameter für Verbindung und Timeouts
├── pytest.ini            # Pytest-Konfiguration
└── requirements.txt      # Abhängigkeiten (inkl. Test-Bibliotheken)
```

---

## Voraussetzungen

- **Python 3.12+**
- Eine laufende Instanz von **Interactive Brokers TWS** (Trader Workstation) oder **IB Gateway**.
  - In der TWS unter *Configuration -> API -> Settings*:
    - Haken bei *Enable ActiveX and Socket Clients* setzen.
    - Port merken (Standard für Live: `7496`, für Paper: `7497`).

---

## Installation & Setup

1. **Repository klonen:**
   ```bash
   git clone https://github.com/fxhuhn/TradeManager.git
   cd TradeManager
   ```

2. **Virtuelle Umgebung erstellen und aktivieren:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # Auf macOS/Linux
   # oder
   .venv\Scripts\activate     # Auf Windows
   ```

3. **Abhängigkeiten installieren:**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Konfiguration einrichten:**
   Kopieren Sie die `.env.example` zu `.env` und tragen Sie Ihre API-Schlüssel ein:
   ```bash
   cp .env.example .env
   ```
   Passen Sie bei Bedarf die Parameter in `config.toml` an (z. B. den TWS-Port).

---

## Anwendung ausführen

Starten Sie das System direkt über Python:
```bash
python app/main.py
```

### Ausführung mit Docker

Das System kann vollständig containerisiert betrieben werden:
```bash
docker-compose up -d --build
```

---

## System-Status & Diagnose (CLI)

Der aktuelle Betriebszustand (Datenbank, Kontostand & Margin, verarbeitete CSV-Archivdateien, Order-Status, fehlerhafte/stornierte Orders und heutiges Settlement) kann jederzeit über das CLI-Diagnosetool abgefragt werden:

### Im laufenden Docker-Container:
```bash
docker exec trading-app python -m app.cli.status
```

### Lokal in der virtuellen Umgebung:
```bash
python -m app.cli.status
```

**Beispielausgabe:**
```text
============================================================
📊 TRADEMANAGER SYSTEM-STATUSBERICHT
============================================================
Datenbank: 🟢 Erreichbar

💼 Kontostand & Margin (Stand: 2026-09-04 12:54:10):
  - Net Liquidation (Equity) : $ 125,450.20
  - Genutzte Margin (Maint)  : $  38,120.00
  - Freie Mittel (Available) : $  87,330.20
  - Konto-Cushion            : 🟢 69.6%
  - Buying Power             : $ 349,320.80

📁 Letzte Archiv-Dateien:
  ✅ orders_2026_09_04.csv.bak
  ✅ orders_2026_09_03.csv.bak

📋 Orders nach Status:
  - Cancelled      : 377
  - Filled         : 122
  - Submitted      : 4

💰 Heutige Abrechnung (Settlement):
  - Abgeschlossene Gruppen : 0
  - Realisierter Net PnL   : 🟢 $ 0.00
  - Kommissionen gesamt    : $ 0.00
============================================================
```


---

## Tests & Qualitätssicherung

Die Qualitätssicherung läuft automatisiert über GitHub Actions, kann aber auch lokal ausgeführt werden.

### Unittests ausführen

Führen Sie alle Tests mittels `pytest` aus:
```bash
pytest
```

Mit Code-Abdeckung (Coverage):
```bash
pytest --cov=app --cov-report=term-missing
```

### Code-Style & Linting (Ruff)

Das Projekt nutzt `ruff` zur Einhaltung der PEP-8-Richtlinien und Qualitätsstandards.

Linter ausführen:
```bash
ruff check .
```

Automatische Formatierung prüfen:
```bash
ruff format --check .
```
