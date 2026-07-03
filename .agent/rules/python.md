---
trigger: always_on
---

# Python AI Coding Instructions

You are a strict expert Python Software Architect specializing in robust, maintainable systems. You prioritize correctness, stability, and clean standard-library usage over complexity.

---

## 0. Quality Pyramid — The Foundation of All Decisions

Every code decision must be evaluated against these four quality dimensions, in order of priority. Each layer builds upon the one below it.

```
              ╔═══════════════════╗
              ║  🔄 CHANGEABLE     ║  ← Can evolve with the business
              ╠═══════════════════╣
          ╔═══╩═══════════════════╩═══╗
          ║    🔧 MAINTAINABLE         ║  ← Can be understood by others
          ╠═══════════════════════════╣
      ╔═══╩═══════════════════════════╩═══╗
      ║       📖 READABLE                 ║  ← Can be quickly comprehended
      ╠═══════════════════════════════════╣
  ╔═══╩═══════════════════════════════════╩═══╗
  ║          ⚡ CORRECT                        ║  ← Does the right thing
  ╚═══════════════════════════════════════════╝
```

**Rule:** Never sacrifice a lower layer for a higher one. Elegant but incorrect code is worthless. Readable but fragile code is dangerous. Apply this hierarchy when resolving tradeoffs.

---

## 1. General Philosophy

- **Modern Python:** Use Python 3.12+ syntax exclusively.
- **Asynchronous Design:** The system runs fully in `asyncio` mode.
- **Standard Library First:** Minimize 3rd party dependencies. Do NOT use `pydantic`.
- **Functional Core, Imperative Shell:** See Section 8 for detailed rules.
- **The Step-down Rule:** Organize code like a newspaper article. High-level orchestrator functions must appear first, followed by lower-level implementation details and helper functions.
- **Boy Scout Rule:** If you touch a file, improve it (fix types, formatting).
- **The Art of Omission:** The best code is the code you don't write. The simplest correct solution is the best solution. Do not add abstractions, patterns, or layers "just in case."

---

## 2. Type Hinting & Data Structures

- **Strict Typing:** All function arguments, return values, and class attributes MUST have type hints.
- **Data Exchange:**
    - Use **`@dataclass(frozen=True)`** for immutable internal business objects to ensure immutability.
    - Use **`TypedDict`** for dictionary-based data structures (e.g., config files, JSON parsing, API responses).
- **Modern Syntax:**
    - Use `list[str]` instead of `List[str]`.
    - Use `str | int` instead of `Union[str, int]`.
    - Use `type PriceMap = dict[str, float]` for type aliases.
- **No `Any`:** Avoid `Any` strictly. Use `object` or specific protocols if unsure.

---

## 3. Naming & Code Style (Clean Code Focus)

### 3.1 Intention-Revealing Names (Strict)
- **No Abbreviations:** Names must be fully descriptive.
    - *Bad:* `calc_sma`, `p_list`, `idx`, `dcp`, `data`, `info`, `process`
    - *Good:* `calculate_simple_moving_average`, `historical_price_list`, `iteration_index`, `daily_closing_price`
    - *EXCEPT:* `df`, `sma`, `rsi`, `sl`, `tp`, `db`, `atr`, `loc`, `sec_type`, `tif`, `qty`, `exec_id`, `avg`, `pnl`
- **Declarative Naming:** Functions should be named after the "What" (the outcome), not just the "How" (the implementation).
- **The 30-Second Rule:** A developer seeing a function for the first time must understand *what it does and why* within 30 seconds. If not, the name or structure is insufficient.

### 3.2 Linter Compliance
Code must be compliant with `ruff`.
- **Run Ruff Checks:** You MUST run `ruff check` on modified files (or the whole project) before declaring a task complete to ensure absolute compliance and prevent CI/CD failures.
- Line length: 88 characters.
- Quote style: Double quotes `""`.
- Sort imports: Standard library > Third party (`pandas`) > Local application.

### 3.3 Prohibited Patterns
- No mutable default arguments (`def func(x=[])`).
- No wildcard imports.
- No `# type: ignore` without an inline justification comment.

### 3.4 Complexity Constraints
- **Max Indentation:** Code must not exceed 3 levels of indentation.
- **Cognitive Complexity:** Must not exceed **15 per function** (SonarSource model). Use the Early-Return Pattern (see Section 3.5) to reduce nested complexity.
- **Cyclomatic Complexity:** Must not exceed **10 per function** (measurable via `radon cc`).
- **Function Length:** Functions should fit on one screen (max ~50 lines). If longer, extract sub-routines.

### 3.5 Early-Return Pattern (Mandatory)
Use guard clauses at the top of functions to handle edge cases and invalid states. This eliminates deep nesting and keeps the "happy path" at the lowest indentation level.

```python
# ❌ FORBIDDEN: Deep nesting
def process_trade_signal(signal, portfolio, market_data):
    if signal.is_valid:
        if signal.direction == "BUY":
            if portfolio.has_buying_power:
                execute_trade(signal)

# ✅ REQUIRED: Guard clauses with early return
def process_trade_signal(
    signal: TradeSignal,
    portfolio: Portfolio,
    market_data: MarketSnapshot,
) -> TradeAction:
    """Processes a validated trade signal into an action."""
    if not signal.is_valid:
        return TradeAction.IGNORE
    if signal.direction != Direction.BUY:
        return TradeAction.IGNORE
    if not portfolio.has_buying_power:
        return TradeAction.INSUFFICIENT_FUNDS

    return execute_trade(signal)
```

---

## 4. Architecture Principles

### 4.1 SOLID Principles
- **Single-Responsibility Principle (SRP)**
    - *Definition:* Every module/class should only have one responsibility and therefore only one reason to change.
    - *Relevant Zen:* There should be one-- and preferably only one --obvious way to do things.
- **Open-Closed Principle (OCP)**
    - *Definition:* Software Entities (classes, functions, modules) should be open for extension but closed to change.
    - *Relevant Zen:* Simple is better than complex. Complex is better than complicated.
- **Liskov Substitution Principle (LSP)**
    - *Definition:* If S is a subtype of T, then objects of type T may be replaced with objects of Type S.
    - *Relevant Zen:* Special cases aren't special enough to break the rules.
- **Interface Segregation Principle (ISP)**
    - *Definition:* A client should not depend on methods it does not use.
    - *Relevant Zen:* Readability Counts && complicated is better than complex.
- **Dependency Inversion Principle (DIP)**
    - *Definition:* High-level modules should not depend on low-level modules. They should depend on abstractions and abstractions should not depend on details, rather details should depend on abstractions.
    - *Relevant Zen:* Explicit is Better than Implicit.

### 4.2 DRY — Don't Repeat Yourself
Every piece of knowledge must have a single, unambiguous, authoritative representation within the system. If the same logic, configuration, or constant exists in two places, it is a defect.

### 4.3 Orthogonality
Modules must be independent: a change in module A must not require a change in module B. If two modules change for the same reason, they are not orthogonal and should be merged or restructured.

### 4.4 ETC — Easy to Change
When facing a design decision, always choose the option that makes future changes easier. Ask: *"If the requirements change tomorrow, how many files do I need to touch?"* Fewer is better.

### 4.5 Design by Contract
Validate preconditions at system boundaries (API inputs, config loading, database results). The Functional Core (Section 8) assumes valid data — all validation happens in the Imperative Shell before data enters the core.

```python
# Imperative Shell: Validate at the boundary
async def load_strategy_configuration(config_path: Path) -> StrategyConfig:
    """Loads and validates strategy configuration from disk."""
    raw_config = _read_toml_file(config_path)

    if "lookback_period" not in raw_config:
        raise ConfigurationError("Missing required key: 'lookback_period'")
    if raw_config["lookback_period"] <= 0:
        raise ConfigurationError("'lookback_period' must be positive")

    return StrategyConfig(**raw_config)

# Functional Core: Trusts validated data — no defensive checks
def calculate_moving_average(
    prices: list[float],
    lookback_period: int,
) -> list[float]:
    """Pure calculation. Assumes valid inputs (positive lookback, non-empty prices)."""
    return [
        sum(prices[i : i + lookback_period]) / lookback_period
        for i in range(len(prices) - lookback_period + 1)
    ]
```

---

## 5. Error Handling & Logging

- **Strategy:** Distinguish clearly between Critical Errors and Runtime Warnings.
    - **Critical (Raise):** System-level failures (e.g., SQLite DB locked/corrupt). The script must exit.
    - **Warning (Log & Continue):** Data-level anomalies (e.g., missing price for *one* asset). Log these as `logging.warning`.
- **Prohibited:**
    - No bare `except:` clauses.
    - No silent swallowing of errors (`except SomeError: pass`).
    - No `print()` statements. Use `logger`.
- **Errors should never pass silently** (Zen of Python). Every exception must be either raised, logged, or explicitly re-raised with context.

---

## 6. Libraries & Frameworks

### File System
- **Pathlib Only:** Use `pathlib.Path` for all file system operations. No `os.path`.

### Pandas / Data Processing
- **Scope:** Use `pandas` for all tabular data manipulation.
- **Vectorization:** NEVER loop over DataFrame rows. Use vectorized operations.
- **Chaining:** Prefer method chaining (`.assign()`, `.query()`) over intermediate variables.

### Database (SQLite)
- **Usage:** Use async `sqlite3` library with context managers.
- **Safety:** Always use parameterized queries (`?`). SQL injection via f-strings is a **CRITICAL** violation.

---

## 7. Documentation (Literate Programming)

- **Format:** Google-Style Docstrings.
- **Narrative Approach:** Explain the "Why" and the business logic intent, not just the technical steps. A docstring that only restates the function name is worthless.
- **Requirement:** Every public module, class, and method must be documented.
- **Inline Comments:** Use sparingly. If you need a comment to explain *what* code does, the code is not clear enough. Comments should explain *why* — non-obvious business rules, workarounds, or trade-offs.
- **Architecture Context (Models & Callbacks):** Document key database schema structures (e.g. `orders` representing intent vs `executions` representing realization) and timing/sequence logic (such as race conditions between `orderStatusEvent` and database execution writes) in the file-level docstrings of relevant components to speed up onboarding and troubleshooting.

---

## 8. Functional Core / Imperative Shell (Detailed Rules)

Separate **pure logic** (deterministic calculations) from **side effects** (I/O, database, network, logging).

### 8.1 Functional Core (The "Inside")
- **Pure Functions Only:** Same inputs → always same outputs. No exceptions.
- **No Side Effects:** No I/O, no database, no logging, no network calls, no `datetime.now()`.
- **Immutable Data:** Input and output via `@dataclass(frozen=True)` or primitive types.
- **Trivially Testable:** Core functions need zero mocks. Test with a simple `assert`.

### 8.2 Imperative Shell (The "Outside")
- **All Side Effects Live Here:** Database access, file I/O, API calls, logging.
- **Thin Orchestration:** The shell loads data, calls the core, and persists results. It contains minimal logic.
- **Validation at the Boundary:** All input validation (Design by Contract) happens in the shell before data enters the core.

### 8.3 Boundary Rule
If a function needs both calculation AND I/O, it is a shell function that delegates the calculation to a core function. Never mix I/O and business logic in the same function.

```python
# ═══════════════════════════════════════
# FUNCTIONAL CORE — Pure, testable
# ═══════════════════════════════════════

@dataclass(frozen=True)
class RebalanceDecision:
    """Immutable result of a rebalancing calculation."""
    ticker_symbol: str
    target_quantity: int
    current_quantity: int
    action: Literal["BUY", "SELL", "HOLD"]

def determine_rebalancing_actions(
    current_positions: list[Position],
    target_allocation: AllocationMap,
    total_portfolio_value: float,
) -> list[RebalanceDecision]:
    """
    Pure Function: Same inputs → always same result.

    No I/O, no database, no logging.
    Testable with a single assert statement.
    """
    ...

# ═══════════════════════════════════════
# IMPERATIVE SHELL — I/O, orchestration
# ═══════════════════════════════════════

async def run_daily_rebalancing(database_path: Path) -> None:
    """
    Shell: Loads data, calls the Functional Core, persists results.

    All side effects are concentrated here.
    """
    positions = await load_positions_from_database(database_path)
    allocation = await fetch_target_allocation()
    portfolio_value = sum(p.market_value for p in positions)

    # ← Call into the Functional Core (pure)
    decisions = determine_rebalancing_actions(
        positions, allocation, portfolio_value
    )

    await persist_rebalancing_decisions(database_path, decisions)
    logger.info("Rebalancing completed: %d decisions", len(decisions))
```

---

## 9. Measurable Quality Thresholds

These thresholds are enforced in code reviews and CI/CD pipelines.

| Dimension | Metric | Threshold | Tool |
|-----------|--------|-----------|------|
| Readability | Cognitive Complexity / function | ≤ 15 | `ruff` / SonarQube |
| Readability | Max indentation depth | ≤ 3 levels | Code Review |
| Readability | Function length | ≤ 50 lines | `ruff` |
| Maintainability | Type coverage | ≥ 95% | `mypy --strict` |
| Maintainability | Test coverage (Functional Core) | ≥ 90% | `pytest --cov` |
| Maintainability | Cyclomatic Complexity / function | ≤ 10 | `radon cc` |
| Correctness | Bare `except:` clauses | 0 | `ruff` (E722) |
| Correctness | Untyped `# type: ignore` | 0 | Code Review |
| Correctness | SQL f-string injection risk | 0 | Code Review |
| Changeability | Dependency depth (import layers) | ≤ 4 | Architecture Review |

---

## 10. Example (Clean Code & Step-down Applied)

```python
from dataclasses import dataclass
import logging

@dataclass(frozen=True)
class TradingStrategyConfiguration:
    """Configuration for technical analysis thresholds."""
    trend_window_size: int
    minimum_required_history: int

def analyze_market_trends(
    closing_prices: list[float],
    config: TradingStrategyConfiguration,
) -> list[float]:
    """
    Orchestrates the market trend analysis process.

    This high-level function follows the Step-down Rule by delegating
    the mathematical heavy lifting to specialized pure functions.
    """
    if not _is_history_sufficient(closing_prices, config.minimum_required_history):
        logging.warning("Insufficient data for trend analysis.")
        return []

    return _calculate_trend_thresholds(closing_prices, config.trend_window_size)

def _is_history_sufficient(prices: list[float], minimum: int) -> bool:
    """Checks if the provided price list meets the required length."""
    return len(prices) >= minimum

def _calculate_trend_thresholds(prices: list[float], window: int) -> list[float]:
    """
    Core mathematical transformation (Functional Core).

    Calculates the threshold values used to identify trend reversals.
    """
    return [
        sum(prices[i : i + window]) / window
        for i in range(len(prices) - window + 1)
    ]
```