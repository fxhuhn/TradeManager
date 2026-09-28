---
description: "Systematischer Workflow zum Auffinden und Klassifizieren von Dead Code, ungenutzten Funktionen und Variablen in app/ mit 3-Stufen-Entscheidungsmatrix (Löschen vs. Deprecated vs. Whitelist) unter python-security."
trigger: "/audit-dead-code"
---

# Dead-Code & Security Audit Workflow (`/audit-dead-code` & `/audit_dead_code`)

Dieser Workflow definiert den verbindlichen Standardablauf zur systematischen Identifikation, Klassifizierung und Bereinigung von ungenutztem Code, verwaisten Funktionen und toten Variablen im Quellcode von `app/`. Er verhindert das versehentliche Löschen von Event-Handlern, Broker-Attributen und defensiven Sicherheits-Mechanismen (`python-security`).

> [!IMPORTANT]
> **Voraussetzung für die Ausführung:**
> Vor jeder Code-Analyse oder -Modifikation MÜSSEN die obligatorischen Schritte aus [.agents/AGENTS.md](../AGENTS.md) eingehalten werden:
> 1. **Step 1:** Architektur-Inspektion von [architecture.md](../../architecture.md) & [references/architecture.md](../../references/architecture.md).
> 2. **Step 2:** Skill-Aktivierung: `python-auditor`, `python-security`, `python-craftsman`.

---

## 1. Warum reicht dieser Ansatz zum direkten Auffinden von Dead Code?

Ein einzelnes Tool (wie reines `ruff` oder `vulture`) ist in komplexen asynchronen Finanz- und Event-Systemen **nicht ausreichend**:
- **`ruff`** arbeitet rein dateibasiert: Es erkennt ungenutzte lokale Variablen (`F841`) oder ungenutzte Importe (`F401`), sieht aber nicht, wenn eine Funktion in Datei A von keinem anderen Modul importiert oder aufgerufen wird.
- **`vulture`** sucht global nach ungenutztem Code, produziert bei Interactive Brokers (`ib_async`) und SQLite jedoch gravierende **False Positives** (z. B. TWS-Order-Attribute wie `transmit`, `whatIf`, `ocaGroup` oder Event-Callbacks wie `on_order_status`). Ein blindes Löschen würde die Orderübertragung zerstören!
- **`scan_dead_code.py` (AST Cross-Reference)** schließt diese Lücke: Es durchsucht den gesamten AST über `app/`, `tests/` und CLI-Skripte hinweg, zählt echte Namens- und Attribut-Referenzen und gleicht Fundstellen mit `vulture_whitelist.py` und `architecture.md` ab.

**Ergebnis:** Durch die Kombination aus `ruff` + `vulture` + `scan_dead_code.py` + `python-security` wird 100 % des toten Codes deterministisch aufgespürt, ohne Broker-Invarianten oder Sicherheitswächter zu gefährden.

---

## 2. Der Standard-Prompt für die Audit-Ausführung

Verwende bei der Übergabe an einen Agenten oder zur manuellen Beauftragung den folgenden präzisen Prompt:

```markdown
Führe einen systematischen Dead-Code- und Sicherheits-Audit für das Verzeichnis `app/` durch.

### Vorgehensweise:
1. Führe den AST-Cross-Reference-Scanner aus:
   `python .agents/skills/python-auditor/scripts/scan_dead_code.py`
2. Prüfe lokale Variablen und Importe:
   `.venv/bin/ruff check app/`
3. Prüfe statische Heuristiken:
   `.venv/bin/vulture app/ vulture_whitelist.py`
4. Führe einen Sicherheits-Scan gemäß `python-security` durch:
   `.venv/bin/bandit -ll -x tests -r app`

### Bewertungs- und Klassifizierungs-Matrix:
Ordne jede gefundene Stelle strikt einer der drei Kategorien zu:

- 🔴 **Kategorie A: Sofort löschen (Direct Deletion)**
  - Private Hilfsfunktion (`_name`) oder lokale Variable.
  - 0 Referenzen in `app/` und 0 Referenzen in `tests/`.
  - Keine dynamische Reflection, keine TWS-Attributbelegung.
  - Löschung verändert kein externes Verhalten und alle 686+ Tests bleiben grün.

- 🟡 **Kategorie B: Zunächst als Deprecated markieren (Deprecation)**
  - Öffentliche Funktion oder Klasse, die in `architecture.md` dokumentiert ist oder in `tests/` getestet wird, aber in der aktuellen Laufzeit-Pipeline (`main.py`) nicht aufgerufen wird.
  - Handhabung: Nicht löschen! Stattdessen mit Python 3.12 `@warnings.deprecated("Grund; Entfernung geplant in vX.Y")` annotieren, Docstring ergänzen und in `architecture.md` als veraltet kennzeichnen.

- 🟢 **Kategorie C: Invariante / Nicht löschen (Whitelist / Keep)**
  - Dynamischer IBKR Event-Callback (`on_*` in `callbacks.py`).
  - TWS/IBKR Broker-Attribut (`transmit`, `whatIf`, `ocaGroup`, etc.).
  - SQLite WAL Model / Dataclass-Feld (`OrderRow`, `ExecutionRow`, `CashLedgerRow`, etc.).
  - Sicherheitswächter (`python-security`: Fail-Closed, Reauth-Timeout, Margin-Cushion, `Decimal`-Validierung).
  - Handhabung: Falls von Vulture gemeldet, in `vulture_whitelist.py` aufnehmen.

### Ergebnis-Bericht:
Erstelle eine Tabelle mit Spalten:
`[Symbol | Datei:Zeile | Fundquelle | Kategorie (A/B/C) | Sicherheits-Bewertung | Geplante Maßnahme]`
```

---

## 3. Phasen des Workflows

```
┌────────────────────────────────────────────────────────┐
│ Phase 1: Automatisierter Multi-Tool-Scan               │
│ scan_dead_code.py + ruff + vulture + bandit            │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 2: Python-Security & Invarianten-Filter          │
│ Fail-Closed, Cushion, 2FA-Reauth, Decimal-Wächter      │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 3: Triage & Entscheidungsfindung (A / B / C)     │
│ Löschen (A) vs. Deprecate (B) vs. Whitelist (C)        │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 4: Umsetzung der Maßnahmen (Micro-Edits)         │
│ Kategorie A: Löschen | Kategorie B: @deprecated        │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 5: Verifikation über alle 5 Quality Gates        │
│ run_quality_gates.py (Ruff, Pytest, Vulture, Sync)     │
└────────────────────────────────────────────────────────┘
```

---

## 4. Phase-für-Phase Anleitung

### Phase 1: Automatisierter Multi-Tool-Scan
Führe die Scan-Werkzeuge aus:
```bash
# 1. AST Cross-Reference & Kategorisierung
.venv/bin/python .agents/skills/python-auditor/scripts/scan_dead_code.py

# 2. Lokale syntaktische Prüfung
.venv/bin/ruff check app/

# 3. Globale Heuristik
.venv/bin/vulture app/ vulture_whitelist.py

# 4. Sicherheits-Scan
.venv/bin/bandit -ll -x tests -r app
```

### Phase 2: Python-Security Triage
Prüfe jedes Verdachtsmoment gegen die 4 Kernprinzipien von `python-security`:
1. **Keine Beseitigung von Fail-Closed-Verhalten**: Exception-Handler, die bei Datenbank- oder Socket-Ausfall den Handel stoppen, dürfen niemals als "toter Code" entfernt werden, selbst wenn sie in Standardtests selten anschlagen.
2. **Finanzielle Integrität**: Konvertierungen und Validierungen (`Decimal`, `parse_positive_decimal`) müssen erhalten bleiben.
3. **Sicherheits-Gateways**: Telegram-Chat-ID-Whitelists, Docker-Socket-Debounce-Timer und Reauthorization-Probes sind geschützt.
4. **Idempotenz**: Ledger-Referenzen (`external_reference_id`) und Status-Prüfungen sind essenziell.

### Phase 3: Klassifizierung
Wende die Entscheidungsmatrix aus Abschnitt 2 an. 
- Falls ein Element **Kategorie A** ist: Plane die Entfernung.
- Falls ein Element **Kategorie B** ist: Plane die Deprecation gemäß Abschnitt 5.
- Falls ein Element **Kategorie C** ist: Prüfe, ob es in `vulture_whitelist.py` hinterlegt ist.

### Phase 4: Deprecation-Muster (Kategorie B)
Für öffentliche APIs, die abgelöst werden sollen, wird der offizielle Standard-Mechanismus von Python 3.12 verwendet (`warnings.deprecated` aus PEP 702):

```python
from warnings import deprecated


@deprecated(
    "get_latest_account_metrics ist veraltet und wird in v1.5 entfernt. "
    "Nutze stattdessen sync_and_save_account_metrics."
)
async def get_latest_account_metrics(
    database: aiosqlite.Connection,
) -> AccountMetricsSnapshot | None:
    """Queries the latest account metrics snapshot from SQLite.

    .. deprecated:: 1.4
       Scheduled for removal in v1.5. Use :func:`sync_and_save_account_metrics` instead.
    """
    ...
```

### Phase 5: Verifikation & Quality Gates
Nach jeder Bereinigung oder Deprecation MÜSSEN alle 5 Tore fehlerfrei bestehen:
```bash
python .agents/skills/python-craftsman/scripts/run_quality_gates.py
```
Insbesondere:
- **Pytest**: Alle 686+ Tests müssen bestehen (`pytest tests/`).
- **Architektur-Sync**: Falls ein öffentliches Element gelöscht wurde, muss [architecture.md](../../architecture.md) synchron aktualisiert werden, da sonst Gate 5 (`check_sync.py`) fehlschlägt!
- **Vulture**: Keine neuen unberechtigten Vulture-Fehler.
