# ruff: noqa
# vulture_whitelist.py
# Whitelist for unused variables, attributes, and classes identified by Vulture


_.row_factory
transmitted_at
ExecutionRow
SettlementRow
net_pnl
settled_at
CSV_FILE_PATH
oca_type

# IBKR Order attributes and trading properties
_.totalQuantity
_.orderRef
_.lmtPrice
_.auxPrice
_.ocaGroup
_.ocaType
_.transmit
_.parentId
_.whatIf
_.outsideRth
_.goodAfterTime
_.conditionsIgnoreRth
_.conditionsCancelOrder
_.exch
_.isMore
_.triggerMethod
_.conjunction
_.conditions

# Simulated / Mocked TWS client methods
_.reqAccountSummary
_.cancelAccountSummary
_.reqOpenOrders
_.reqCompletedOrders

# Public entrypoints in app/trading
TwsCallbacksManager
register_all
run_recovery
handle_retriable_error
trigger_settlement
execution_worker
_.send_daily_summary
_.send_archived_error_alert

# Public entrypoints in app/services/account_metrics
get_latest_account_metrics
save_account_metrics
sync_and_save_account_metrics
AccountMetricsSnapshot
AccountMetricsReport
