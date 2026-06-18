# ScoreCast

**Live World Cup exact-score odds from Polymarket, served as a self-hosted web dashboard.**

ScoreCast polls the [Polymarket Gamma API](https://gamma-api.polymarket.com) every 60 seconds and renders a clean browser dashboard showing implied probabilities for every exact full-time scoreline across upcoming World Cup matches — including the catch-all "Any Other Score" bucket. No account, no API key, no fees.

---

## Features

- Exact-score market probabilities for all matches kicking off in the next 24 hours
- Bid / ask / last-trade prices alongside each scoreline
- Relative probability bars scaled per match
- Kickoff times in your browser's local timezone with a live countdown
- Auto-refreshes in place — no page reloads
- Binds to `0.0.0.0` so it's reachable over Tailscale (or any LAN) out of the box
- Survives reboots via systemd user service
- Single-file, ~350 lines of Python, zero frontend dependencies

---

## Requirements

- Python 3.9+
- `requests` library

```bash
pip install requests
```

---

## Usage

```bash
python3 wc_scores.py
```

Open `http://<your-ip>:8081/` in a browser.

### Options

| Flag | Default | Description |
|---|---|---|
| `--port` | `8081` | HTTP port |
| `--host` | `0.0.0.0` | Bind address |
| `--hours` | `24` | Kickoff window (hours from now) |
| `--interval` | `60` | Data refresh interval (seconds) |
| `--tag-id` | `102232` | Polymarket tag ID for World Cup events |
| `--page-size` | `100` | API page size (pagination) |

---

## Run on boot (systemd)

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/wc-scores.service << 'EOF'
[Unit]
Description=ScoreCast – Polymarket World Cup Dashboard
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/YOUR_USER/polyworldcup/wc_scores.py
WorkingDirectory=/home/YOUR_USER/polyworldcup
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user enable --now wc-scores
loginctl enable-linger YOUR_USER
```

Replace `YOUR_USER` with your username. `enable-linger` keeps the service alive after logout.

To check logs:
```bash
journalctl --user -u wc-scores -f
```

---

## How it works

The Polymarket Gamma API groups World Cup markets into separate top-level events per match type. Exact-score events have `"- Exact Score"` in their title and contain one market per scoreline (e.g. `"Spain 2 - 1 Brazil"`). Each market carries an `outcomePrices` field — a JSON-encoded array of implied probabilities for Yes/No — where index 0 is the "Yes" price, directly interpretable as a probability (no conversion needed).

ScoreCast:
1. Paginates through all events for the configured tag ID
2. Filters to matches whose `endDate` (actual kickoff time) falls within the configured window
3. Strips auxiliary markets (corners, player props, halftime, announcer props) using suffix and word-count heuristics
4. Deduplicates: if both a plain result event and an exact-score event exist for the same fixture, only the exact-score event is shown
5. Serves the parsed data as JSON at `/api/data`; the frontend polls this and re-renders without a page reload

---

## Tailscale setup

The server binds to `0.0.0.0` by default, so it's accessible on your Tailscale IP immediately. No extra configuration needed. If you run another web app on the same host, use `--port` to pick a free port.

---

## License

MIT
