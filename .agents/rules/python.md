# Python Coding Standards — Canonical Reference

> **Scope:** This document is the single source of truth for all Python coding rules in the TradeManager project.
> It is referenced by `python-auditor`, `python-craftsman`, `python-creator`, and `python-tester` skills.
>
> **Cross-References:**
> - [architecture.md](file:///Users/produktmanagement/Python/github/TradeManager/architecture.md) — High-level system design & public API reference
> - [references/architecture.md](file:///Users/produktmanagement/Python/github/TradeManager/references/architecture.md) — DB schemas, state machines, CSV contracts, error matrices

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
- **Asynchronous Design:** The system runs fully in a single-threaded `asyncio` event loop. All blocking operations (database, network, file reads) must be non-blocking.
- **Standard Library First:** Minimize 3rd party dependencies. Maximize use of `itertools`, `functools`, `collections`, `decimal`, and `typing`. Do **NOT** use `pydantic`.
- **Decimal Financial Precision:** Every price, quantity, commission, slippage, and profit/loss calculation **MUST** use `decimal.Decimal`. Using `float` for monetary values is a **CRITICAL** violation (precision loss enables value skimming). See [architecture.md §2.2](file:///Users/produktmanagement/Python/github/TradeManager/architecture.md).
- **Functional Core, Imperative Shell:** See Section 8 for detailed rules.
- **The Step-down Rule:** Organize code like a newspaper article. High-level orchestrator functions must appear first, followed by lower-level implementation details and helper functions.
- **Boy Scout Rule:** If you touch a file, improve it (fix types, formatting).
- **The Art of Omission:** The best code is the code you don't write. The simplest correct solution is the best solution. Do not add abstractions, patterns, or layers "just in case."

---

## 2. Type Hinting & Data Structures

- **Strict Typing:** All function arguments, return values, and class attributes **MUST** have type hints.
- **Data Exchange:**
    - Use **`@dataclass(frozen=True)`** for immutable internal business objects to ensure immutability.
    - Use **`TypedDict`** for dictionary-based data structures (e.g., config files, JSON parsing, API responses).
- **Modern Syntax:**
    - Use `list[str]` instead of `List[str]`.
    - Use `str | int` instead of `Union[str, int]`.
    - Use `type PriceMap = dict[str, Decimal]` for type aliases.
- **No `Any`:** Avoid `Any` strictly. Use `object` or specific `Protocol` abstractions if unsure.
- **Decimal Types:** Financial fields must always be typed as `Decimal`, never as `float`:
    ```python
    from decimal import Decimal

    @dataclass(frozen=True)
    class OrderRow:
        target_price: Decimal | None  # ✅ Correct
        quantity: int                 # ✅ Correct (integer shares)
        # target_price: float         # ❌ CRITICAL violation
    ```

---

## 3. Naming & Code Style (Clean Code Focus)

### 3.1 Intention-Revealing Names (Strict)
- **No Abbreviations:** Names must be fully descriptive.
    - *Bad:* `calc_sma`, `p_list`, `idx`, `dcp`, `data`, `info`, `process`, `ctx`, `res`, `val`, `conf`
    - *Good:* `calculate_simple_moving_average`, `historical_price_list`, `iteration_index`, `daily_closing_price`, `context`, `result`, `value`, `configuration`
    - **Allowed Exceptions:** `df`, `db`, `avg`, `qty`, `pnl`, `sma`, `rsi`, `sl`, `tp`, `atr`, `loc`, `sec_type`, `tif`, `exec_id`
- **Declarative Naming:** Functions should be named after the "What" (the outcome), not just the "How" (the implementation).
- **The 30-Second Rule:** A developer seeing a function for the first time must understand *what it does and why* within 30 seconds. If not, the name or structure is insufficient.

### 3.2 Linter Compliance
Code must be compliant with `ruff`.
- **Run Ruff Checks:** You **MUST** run `ruff check` on modified files (or the whole project) before declaring a task complete to ensure absolute compliance and prevent CI/CD failures.
- Line length: 88 characters.
- Quote style: Double quotes `""`.
- Sort imports: Standard library > Third party (`pandas`, `ib_async`) > Local application.

### 3.3 Prohibited Patterns
- No mutable default arguments (`def func(x=[])`).
- No wildcard imports.
- No `# type: ignore` without an inline justification comment.
- No `print()` statements — use `logger`.
- No `pickle`, `cPickle`, `marshal`, `shelve`, `yaml.load` (use `yaml.safe_load`).

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
- **Single-Responsibility Principle (SRP):** Every module/class should only have one responsibility and therefore only one reason to change.
- **Open-Closed Principle (OCP):** Software entities (classes, functions, modules) should be open for extension but closed for modification.
- **Liskov Substitution Principle (LSP):** If S is a subtype of T, then objects of type T may be replaced with objects of type S without altering correctness.
- **Interface Segregation Principle (ISP):** A client should not depend on methods it does not use.
- **Dependency Inversion Principle (DIP):** High-level modules should not depend on low-level modules. Both should depend on abstractions (`Protocol`).

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
    prices: list[Decimal],
    lookback_period: int,
) -> list[Decimal]:
    """Pure calculation. Assumes valid inputs (positive lookback, non-empty prices)."""
    return [
        sum(prices[i : i + lookback_period]) / lookback_period
        for i in range(len(prices) - lookback_period + 1)
    ]
```

---

## 5. Error Handling & Logging

- **Strategy:** Distinguish clearly between Critical Errors and Runtime Warnings.
    - **Critical (Raise):** System-level failures (e.g., SQLite DB locked/corrupt, TWS connection severed). The system must fail-closed.
    - **Warning (Log & Continue):** Data-level anomalies (e.g., missing price for *one* asset, slippage above threshold). Log these as `logging.warning`.
- **Prohibited:**
    - No bare `except:` clauses.
    - No silent swallowing of errors (`except SomeError: pass`).
    - No `print()` statements. Use `logger`.
- **Fail-Closed Principle:** If the database fails or network drops, trades must **NOT** proceed. The system must halt and alert (see [error_codes.py](file:///Users/produktmanagement/Python/github/TradeManager/app/trading/error_codes.py) for classification).
- **Errors should never pass silently** (Zen of Python). Every exception must be either raised, logged, or explicitly re-raised with context.
- **Information Security:** Never log `strategy_name`, `account_id`, or position sizes at `INFO` level in production. Log *errors*, not *alpha*.

---

## 6. Libraries & Frameworks

### File System
- **Pathlib Only:** Use `pathlib.Path` for all file system operations. No `os.path`.

### Pandas / Data Processing
- **Scope:** Use `pandas` for tabular data manipulation.
- **Vectorization:** NEVER loop over DataFrame rows. Use vectorized operations.
- **Chaining:** Prefer method chaining (`.assign()`, `.query()`) over intermediate variables.

### Database (SQLite)
- **Mode:** WAL mode (`PRAGMA journal_mode=WAL`) with foreign keys enabled (`PRAGMA foreign_keys = ON`).
- **Connection Pattern:** Use `get_db()` and `transaction()` from `app.core.db` for all database operations.
- **Safety:** Always use parameterized queries (`?`). SQL injection via f-strings is a **CRITICAL** violation.
- **Decimal Storage:** Money values are stored as `TEXT` (stringified `Decimal`) and converted via `decimal_from_db()` / `parse_positive_decimal()`.
- **ID Cascades:** Child orders link via `FOREIGN KEY (parent_id) REFERENCES orders (order_id) ON UPDATE CASCADE`. Updating a parent's negative temp ID to its real TWS ID automatically propagates.

### IBKR / ib_async
- **Async Only:** All TWS interactions via `ib_async.IB` in the `asyncio` event loop.
- **Timeout Wrapping:** Network-sensitive calls must be wrapped in `asyncio.wait_for`.
- **Error Classification:** TWS error codes are classified via `classify_error_code()` into `INFO`, `RECONNECT`, `RETRIABLE`, `CANCEL`, `FATAL`.

---

## 7. Documentation (Literate Programming)

- **Format:** Google-Style Docstrings.
- **Narrative Approach:** Explain the "Why" and the business logic intent, not just the technical steps. A docstring that only restates the function name is worthless.
- **Requirement:** Every public module, class, and method must be documented.
- **Inline Comments:** Use sparingly. If you need a comment to explain *what* code does, the code is not clear enough. Comments should explain *why* — non-obvious business rules, workarounds, or trade-offs.
- **Architecture Context:** Document key database schema structures (e.g. `orders` representing intent vs `executions` representing realization) and timing/sequence logic (such as race conditions between `orderStatusEvent` and database execution writes) in file-level docstrings.

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
from decimal import Decimal

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
    total_portfolio_value: Decimal,
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
    portfolio_value = sum(
        (p.market_value for p in positions), start=Decimal("0")
    )

    # ← Call into the Functional Core (pure)
    decisions = determine_rebalancing_actions(
        positions, allocation, portfolio_value
    )

    await persist_rebalancing_decisions(database_path, decisions)
    logger.info("Rebalancing completed: %d decisions", len(decisions))
```

---

## 9. Measurable Quality Thresholds

These thresholds are enforced in code reviews, audits, and CI/CD pipelines.

| Dimension | Metric | Threshold | Tool |
|---|---|---|---|
| Correctness | Bare `except:` clauses | 0 | `ruff` (E722) |
| Correctness | SQL f-string injection risk | 0 | `bandit` / Code Review |
| Correctness | `float` used for money | 0 | `python-security` audit |
| Correctness | Untyped `# type: ignore` | 0 | Code Review |
| Readability | Cognitive Complexity / function | ≤ 15 | `ruff` / SonarQube |
| Readability | Max indentation depth | ≤ 3 levels | Code Review |
| Readability | Function length | ≤ 50 lines | `ruff` |
| Maintainability | Type coverage | ≥ 95% | `mypy --strict` |
| Maintainability | Test coverage (branch) | ≥ 80% | `pytest --cov --cov-fail-under=80` |
| Maintainability | Cyclomatic Complexity / function | ≤ 10 | `radon cc` |
| Changeability | Dependency depth (import layers) | ≤ 4 | Architecture Review |
| Security | Serialization risks (pickle etc.) | 0 | `bandit` |
| Security | Dead code | 0 | `vulture` |

---

## 10. Example (Clean Code, Step-down, Decimal Applied)

```python
from dataclasses import dataclass
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class TradingStrategyConfiguration:
    """Configuration for technical analysis thresholds."""
    trend_window_size: int
    minimum_required_history: int

def analyze_market_trends(
    closing_prices: list[Decimal],
    config: TradingStrategyConfiguration,
) -> list[Decimal]:
    """
    Orchestrates the market trend analysis process.

    This high-level function follows the Step-down Rule by delegating
    the mathematical heavy lifting to specialized pure functions.
    """
    if not _is_history_sufficient(closing_prices, config.minimum_required_history):
        logger.warning("Insufficient data for trend analysis.")
        return []

    return _calculate_trend_thresholds(closing_prices, config.trend_window_size)

def _is_history_sufficient(prices: list[Decimal], minimum: int) -> bool:
    """Checks if the provided price list meets the required length."""
    return len(prices) >= minimum

def _calculate_trend_thresholds(
    prices: list[Decimal],
    window: int,
) -> list[Decimal]:
    """
    Core mathematical transformation (Functional Core).

    Calculates the threshold values used to identify trend reversals.
    Uses Decimal arithmetic to preserve financial precision.
    """
    return [
        sum(prices[i : i + window]) / window
        for i in range(len(prices) - window + 1)
    ]
```
