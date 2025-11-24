# Operator Guide

This guide is for the person operating WobbleBot in day‑to‑day use.  It describes the mental model, configuration basics, typical workflows, and emergency procedures.  It supplements the `deployment-guide.md` and `operations.md` with a focus on the operator’s perspective.

## Mental Model

WobbleBot consists of three primary subsystems:

1. **Trader (Bot Core)** – Implements the deterministic micro‑grid trading strategy.  It places limit orders and tracks positions and P&L.  Controls: off | paper | live.
2. **Advisor (LLM)** – Generates strategy suggestions in JSON format.  It cannot execute trades or move money.  Controls: off | passive (record suggestions) | active (auto‑apply within safe bounds).
3. **Harvester** – Manages balances between Kraken and a bank account.  In passive mode it only proposes transfers; in active mode it executes withdrawals (and optionally deposits) according to configured thresholds.  Controls: off | passive | active (withdrawals only in v1.0).

Each subsystem can be independently turned on, put into passive mode, or fully disabled.  Mode changes should be deliberate and recorded (see below).

## Configuration Basics

All runtime behavior is driven by configuration files, primarily `config/wobblebot.yml`.  Key sections include:

- `assets` – List of coins to trade and their grid parameters.  Example:

  ```yaml
  assets:
    DOGE:
      enabled: true
      grid:
        min_price: 0.10
        max_price: 0.20
        step: 0.002
      max_funds: 50.0
  ```

- `safety` – Global exposure caps and daily spend caps.  These are absolute hard limits for all trading activity.

- `advisor` – Controls for the Strategy Advisor: whether it’s enabled, polling interval, auto‑apply flag, and LLM endpoint.

- `harvester` – Controls for the Harvester: mode, min and max balances, transfer limits, and whether deposits are allowed.

- `logging` – Configure log levels and destinations.

After editing the config, restart WobbleBot to apply changes.  Keep old configurations under version control or backup so you can roll back if needed.

## Typical Workflows

### Safe Ramp‑Up

1. **Initialize** – Start with all subsystems disabled except Trader in paper mode.  This means no real trades, no advisor calls, no transfers.
2. **Observe** – Let the bot run for several days.  Check logs, DB entries, and metrics.  Tune grid parameters and safety caps based on observed behavior.
3. **Enable Advisor (Passive)** – Turn on the Advisor in passive mode.  It will produce JSON suggestions but not apply them.  Review the suggestions to gauge their usefulness and sanity.
4. **Enable Trader (Tiny Live)** – Switch Trader to live mode with tiny order sizes (e.g., $5 per order).  Continue monitoring.  Ensure safety caps are working.
5. **Enable Harvester (Passive)** – Let the Harvester propose transfers when the Kraken balance exceeds your target band.  Verify proposals match your risk appetite.
6. **Enable Harvester (Active)** – If comfortable, enable active withdrawals.  The Harvester will automatically transfer excess funds from Kraken to your bank within configured limits.  Deposits remain manual unless explicitly enabled later.

### Normal Operation

Once ramped up, normal operations involve:

- Monitoring the dashboard or logs for ongoing performance and health.
- Adjusting grid parameters when market volatility changes.  For example, widen grids if volatility increases to avoid too frequent trading; tighten grids when markets are flat.
- Occasionally running the Advisor on demand using `wobblebot advise` to see new suggestions.
- Reviewing Harvester actions and proposals.  Ensure it’s not pulling too much or leaving too little on the exchange.
- Updating the config as your comfort with risk grows or shrinks.

### Pausing or Halting

If you need to pause trading or halt all actions:

1. Edit the config to set all subsystems to `off` or desired safe modes (e.g., Trader → paper, Advisor → off, Harvester → passive).
2. Alternatively, use the CLI if such commands are implemented (e.g., `wobblebot run --paper`).
3. Restart WobbleBot.
4. Confirm via logs and DB that no new orders or transfers are being created.

## Reading the System

To understand what WobbleBot is doing at any given time, you can:

- **Use the CLI** – `wobblebot status` (once implemented) will show active coins, positions, open orders, P&L, and current modes.
- **Inspect the DB** – Query the SQLite DB to see recent trades, advisor suggestions, and harvester logs.  You can use a tool like `sqlite3` or a GUI.
- **Check Logs** – Logs provide a detailed timeline of decisions and actions.  Use them to reconstruct events and audit auto‑applied changes.
- **Dashboard** – If you’ve configured Grafana, check dashboards for P&L curves, exposure, cycle counts, etc.

## Emergency Procedures (Panic Buttons)

Sometimes you need to act fast:

- **Stop all containers** – Run `docker compose down` to instantly halt the system.
- **Switch to paper mode** – Edit the config (or use a CLI flag) to set Trader to paper, Advisor to off, and Harvester to off, then restart.
- **Disable withdrawals** – Remove or disable the Harvester key in the environment.  Without a valid key, withdrawals cannot occur.

After any emergency action, perform a detailed review before resuming normal operations.

## When to Revisit the Docs

Return to this operator guide and the rest of the documentation when:

- A new version of WobbleBot is released or installed.
- You enable a new mode (e.g., turning on the Advisor’s auto‑apply or enabling withdrawals).
- Market conditions change significantly and require retuning of grids or safety caps.
- You observe unexpected behavior or anomalies in logs or P&L.

Documentation is part of the system.  Keeping docs accurate and up to date helps ensure you and future operators always understand what the bot is doing and why.