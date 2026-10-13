# Bitget V4 — separate manual test setup

Upload these files to the ROOT of `Crypto-signal-bot-v2`, keeping `.github/workflows/bitget-v4.yml` in its shown folder.

- `bitget_signal_bot_v4.py`: closed-candle scanner (WATCH BUY/SELL and CONFIRMED BUY/SELL), no real orders.
- `requirements-v4.txt`: Python dependency.
- `.github/workflows/bitget-v4.yml`: separate GitHub Actions manual test.

## First run
1. Check that repository secret `DISCORD_WEBHOOK_URL` is configured (Settings → Secrets and variables → Actions).
2. Open Actions → **Bitget V4 Signal Bot (Manual Test)** → **Run workflow**.
3. Read the workflow logs and Discord messages. `NO TRADE` is printed in logs only.

## Notes
- The existing workflow, scheduler and old bot are not modified.
- Scheduling is disabled in the V4 workflow. To enable it later, uncomment the `schedule` lines in the workflow; scheduled runs may be delayed by GitHub.
- The bot saves `v4_signal_state.json` on its runner. GitHub-hosted runners are ephemeral, so this state is **not persistent across workflow runs**. Repeated WATCH/CONFIRMED alerts can occur on separate runs. Persistent signal-state and trade-state tracking require additional work.
- This is an unverified strategy; do not treat a score as a win probability. Test with demo trading first.
