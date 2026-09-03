---
description: "Standardablauf für sicheres, schrittweises Python-Refactoring ohne Verhaltensänderung (TDD Pinning, Golden Master, Micro-Edits)."
trigger: "/refactor"
---

# Refactoring Workflow (Automated Python Refactoring Engine)

Dieses Dokument definiert den verbindlichen Standardablauf für die sichere, schrittweise und automatisierte Umstrukturierung von bestehendem Python-Code in TradeManager zur Erreichung des Repository-Gold-Standards (strikte Typisierung, saubere Architektur, hohe Testabdeckung), ohne das bestehende Laufzeitverhalten zu verändern (*Behavioral Equivalence*).

> [!IMPORTANT]
> **Voraussetzung für die Ausführung:**
> Vor jeder Code-Analyse oder -Modifikation MÜSSEN die obligatorischen Schritte aus [.agents/AGENTS.md](.agents/AGENTS.md) eingehalten werden:
> 1. **Step 1:** Architektur-Inspektion von [architecture.md](architecture.md) & [references/architecture.md](references/architecture.md).
> 2. **Step 2:** Skill-Aktivierung gemäß der betroffenen Phasen.

---

## Phasen-Übersicht

```
┌────────────────────────────────────────────────────────┐
│ Phase 1: Baseline Verification (Status Quo sichern)    │
│ Skill: python-tester (Pinning Tests & Golden Master)   │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 2: Static Analysis, Security & Blast-Radius      │
│ Skills: python-auditor & python-security (Diagnose)    │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 3: Iterative Transformation (Refactoring)        │
│ Skill: python-craftsman (Micro-TDD-Schleife)           │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 4: Final Verification & Quality Gates            │
│ Skills: python-craftsman, tester, auditor, security    │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 5: Documentation & Architecture Sync             │
│ Skills: python-craftsman & architect-design            │
└────────────────────────────────────────────────────────┘
```

---

## Phase 1: Baseline Verification (Status Quo sichern)
**Verantwortlicher Skill:** `python-tester` ([.agents/skills/python-tester/SKILL.md](.agents/skills/python-tester/SKILL.md))

1. **Bestehende Test-Suite ausführen:**
   - Ausführung von `pytest tests/` für das Zielmodul und direkt gekoppelte Subsysteme.
   - *Quality Gate 1:* Alle bestehenden Tests MÜSSEN grün sein. Bei fehlschlagenden Tests bricht der Workflow sofort ab.
2. **Branch-Coverage & Varianten-Matrix:**
   - Abdeckung via `pytest --cov=app.<subsystem> --cov-branch --cov-report=term-missing` ermitteln.
   - Nicht nur auf pauschale Zeilen-Coverage blicken: Systematische Prüfung gegen eine **fachliche Varianten-Matrix** (Grenzwerte, Verzweigungen, `None`-Werte, optionale Parameter, alternative Order-Typen).
3. **Dauerhafte Characterization Tests (Pinning Tests):**
   - Für alle ungedeckten Pfade und Zweige MÜSSEN vorab Characterization-Tests in `tests/` erstellt werden.
   - *Wichtig:* Diese Tests sind **dauerhafter Regressionsschutz** und dürfen nach dem Refactoring **nicht** gelöscht werden.
4. **Golden-Master- / Snapshot-Sicherung (bei I/O, Exporten & Formatierungen):**
   - Erzeugt das Modul persistente Artefakte oder formatierte Ausgaben (z. B. CSV, SQLite-Records, Telegram-HTML-Nachrichten, Settlement-Berechnungen), wird vor dem Code-Eingriff ein Referenz-Snapshot (Golden Master) der Ausgabe erzeugt, um nach dem Refactoring absolute Inhalts- und Verhaltensgleichheit nachzuweisen.
   - *Quality Gate 2:* Branch-Coverage $\ge 90\,\%$, alle Varianten der Matrix erfasst, Snapshot gesichert.

---

## Phase 2: Static Analysis, Security & Blast-Radius Audit (Diagnose)
**Verantwortliche Skills:** `python-auditor` ([.agents/skills/python-auditor/SKILL.md](.agents/skills/python-auditor/SKILL.md)) & `python-security` ([.agents/skills/python-security/SKILL.md](.agents/skills/python-security/SKILL.md))

1. **Blast-Radius & Aufrufer-Analyse:**
   - Ermittlung aller externen Aufrufer und Konsumenten der öffentlichen Funktionen/Klassen via `grep_search`.
   - Sicherstellen, dass geplante Signaturänderungen rückwärtskompatibel sind oder alle Aufrufer synchron migriert werden.
2. **Qualitäts-Scan (`python-auditor`):**
   - Syntax-, Style- und Type-Checks via `ruff check .` und `mypy`.
   - Prüfung gegen die Qualitäts-Pyramide (Correctness > Readability > Maintainability > Changeability).
   - Identifizierung von Code Smells:
     * Zyklomatische Komplexität > 10 (`radon cc`)
     * Kognitive Komplexität > 15
     * Verschachtelungstiefe > 3 Ebenen
     * Mehr als 5 funktionale Parameter (Refactoring zu `@dataclass(frozen=True)` oder `TypedDict`)
     * `print()` statt `logger`
     * Fehlende Docstrings oder unklare Bezeichner (Verstoß gegen [.agents/rules/python.md §3](.agents/rules/python.md))
3. **Sicherheits- & Präzisions-Scan (`python-security`):**
   - **Zero-Float Invariante:** Auditierung auf Verwendung von binären `float`-Werten für Geld, Preise, PnL oder Slippage $\rightarrow$ strikte Umstellung auf `decimal.Decimal`.
   - **SQL-Injektion & WAL-Integrität:** Sicherstellen parametrisierter Queries (`?`), keine String-Interpolation, korrekte Foreign-Key-Cascades (`ON UPDATE CASCADE`).
   - **Fail-Closed Prinzip:** Keine `except: pass` Blöcke, saubere Klassifizierung von TWS-Fehlern via `classify_error_code`.
4. **Erstellung des Audit-Protokolls:**
   - Konsolidierung aller Befunde in einer priorisierten Mängelliste (Teil A: Code Quality, Teil B: Security & Architecture).

---

## Phase 3: Iterative Transformation (Refactoring)
**Verantwortlicher Skill:** `python-craftsman` ([.agents/skills/python-craftsman/SKILL.md](.agents/skills/python-craftsman/SKILL.md))

1. **Abarbeitung des Audit-Protokolls in Micro-Schritten:**
   - Schrittweise Behebung der im Protokoll definierten Punkte.
   - **Micro-Test-Schleife (TDD-Refactoring):** Nach *jedem einzelnen* Teilschritt (z. B. Extraktion einer Konstante, Umstellung eines Modells) wird die Test-Suite sofort ausgeführt (`Edit` $\rightarrow$ `Tests grün` $\rightarrow$ `nächster Edit`).
   - Schlägt ein Zwischenschritt fehl, wird sofort auf den letzten grünen Stand zurückgerollt (`git checkout -- <file>`).
2. **TradeManager Architektur- und Clean-Code-Regeln:**
   - **Standard-Bibliothek First:** Keine externen Validatoren wie Pydantic; Verwendung von `@dataclass(frozen=True)` für immutable Domain-Objekte oder `TypedDict` / `NamedTuple` für DTOs.
   - **Paradigma-Trennung (Functional Core vs. Imperative Shell):**
     * **Functional Core:** Rein synchrone, deterministische Berechnungsfunktionen ohne Nebeneffekte (kein `async def`, keine I/O, kein Logger, keine DB).
     * **Imperative Shell:** Asynchrone Orchestrierung (`async def` mit `asyncio`, `ib_async`, `aiosqlite`) für Netzwerk, Dateizugriff und DB-Transaktionen.
   - **Dependency Injection:** Übergabe von Konfigurationen und Verbindungen statt harter Kopplung an globale Singletons.
   - **Early-Return Pattern:** Guard Clauses am Funktionsanfang zur Reduktion der Verschachtelungstiefe (maximal 3 Ebenen).
3. **Inkrementelle Reihenfolge:**
   - **Schritt A:** Konstanten, Typen & DTOs definieren/extrahieren.
   - **Schritt B:** Interne Berechnungslogik und Algorithmen in Pure Functions entkoppeln.
   - **Schritt C:** I/O, Schnittstellen und asynchrone Shell-Aufrufer harmonisieren.

---

## Phase 4: Final Verification & Quality Gates (Validierung)
**Verantwortliche Skills:** `python-craftsman`, `python-tester`, `python-auditor`, `python-security`

1. **Regressionstest & Snapshot-Vergleich (`python-tester`):**
   - Erneutes Ausführen der gesamten Test-Suite via `pytest tests/`.
   - Bei I/O-Modulen: Byte-für-Byte- oder semantischer Diff-Vergleich mit dem Golden-Master-Snapshot aus Phase 1.
   - *Quality Gate 3:* Verhaltensäquivalenz zu 100 % gewahrt (0 Regressionen, Snapshot identisch).
2. **Automated 5-Gate Quality Pipeline (`python-craftsman`):**
   - Ausführung des zentralen Runners:
     ```bash
     python .agents/skills/python-craftsman/scripts/run_quality_gates.py
     ```
   - Dies verifiziert automatisch:
     * **Gate 1:** `ruff check .` & `ruff format --check .` (0 Linting-/Style-Fehler).
     * **Gate 2:** `pytest` mit Coverage $\ge 80\,\%$.
     * **Gate 3:** Architecture & Type Audit (`mypy`).
     * **Gate 4:** Security Scan (Zero-Float, Injection, etc.).
     * **Gate 5:** Architecture Sync Check (`python .agents/skills/architecture-sync/scripts/check_sync.py`).

---

## Phase 5: Documentation & Architecture Sync
**Verantwortliche Skills:** `python-craftsman` & `architect-design` ([.agents/skills/architect-design/SKILL.md](.agents/skills/architect-design/SKILL.md))

1. **Diff-Inspektion (`git diff`):**
   - Strikte Prüfung gegen Scope Leaks oder unbeabsichtigte Formatierungsänderungen außerhalb des Refactorings.
2. **Architecture Sync & Dokumentation:**
   - Wurden öffentliche Funktionen oder Klassen umbenannt, verschoben oder hinzugefügt, MUSS [architecture.md §4](architecture.md) aktualisiert werden.
   - Validierung durch:
     ```bash
     python .agents/skills/architecture-sync/scripts/check_sync.py
     ```
   - Alle neuen oder geänderten öffentlichen Symbole erhalten Google-Style Docstrings ("Why" statt "How").
3. **Abschlussbericht (Completion Report):**
   - Erstellung des standardisierten Berichts gemäß [.agents/AGENTS.md](.agents/AGENTS.md):
     * `Changed`: Umgesetzte Refactoring-Maßnahmen.
     * `Files`: Modifizierte Dateien.
     * `Validation`: Ausgeführte Test- und Gate-Befehle inklusive Status.
     * `Not validated`: Nicht ausführbare Prüfungen (mit Begründung).
     * `Assumptions`: Getroffene Annahmen.
     * `Out of scope`: Entdeckte Mängel außerhalb des Aufgabenbereichs.
