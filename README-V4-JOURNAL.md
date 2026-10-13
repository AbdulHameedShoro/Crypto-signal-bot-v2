# Bitget V4 — Discord alerts and trade journal

1. Upload `bitget_signal_bot_v4.py`, `requirements-v4.txt` and `README-V4-JOURNAL.md` to the repository root.
2. Upload `.github/workflows/bitget-v4-journal.yml` to the matching workflow folder.
3. Upload `trade_journal/my_trades.csv` to that folder. Do not overwrite your own trade entries.
4. Ensure GitHub Actions setting **Workflow permissions** permits **Read and write permissions**. `DISCORD_WEBHOOK_URL` must exist as repository secret.
5. **Disable the older V4 workflow** (`bitget-v4.yml`) before using the new schedule to avoid duplicate alerts. Leave the old non-V4 bot unchanged.
6. Run the new workflow manually once. The scheduled runs use UTC and may be delayed by GitHub.

## Files and logging

- `trade_journal/signals.csv`: automatically written WATCH and CONFIRMED alerts, including unique signal ID and closed-candle UTC time. No NO TRADE rows.
- `trade_journal/my_trades.csv`: manually record exchange fills; copy the Signal ID from the Discord message to match them. Enter UTC time, actual entry, size, stop and exit. This file is not automatically edited by the workflow.
- `v4_signal_state.json`: remembers last delivered signal by symbol across scheduled runs, committed to the repository.

The bot **does not connect to private exchange accounts or place trades**. Trade P&L must be recorded from your exchange. A WATCH alert is not an entry. A CONFIRMED alert is not a guarantee. GitHub schedule can be late and write access can fail if repository rules prevent bot commits; inspect the workflow logs. Public repositories expose journal files to everyone: **do not put account IDs, balances, secrets, or private trade details in a public repository**. If your repo is public, keep `my_trades.csv` locally/private rather than uploading it.
