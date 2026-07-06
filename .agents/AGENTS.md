# Custom Agent Rules for TradeManager

## Testing & Validation Rules
- **Dry-run Priority:** Whenever a test run, simulation, or verification of an order file is requested, start directly by executing the appropriate dry-run script (e.g., `scripts/run_dry_run_today.py` or other diagnostic/dry-run scripts under `scripts/`) to get precise diagnostic output for the current database and CSV state.
