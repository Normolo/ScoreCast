#!/usr/bin/env python3
"""
wc_scores.py — Polymarket World Cup exact-score market web dashboard.

Runs a local HTTP server. Open http://<host>:8080 in a browser.

Usage:
  python3 wc_scores.py [--port 8080] [--tag-id 102232] [--hours 24] [--interval 60]
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("Missing dependency: pip install requests")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://gamma-api.polymarket.com"

SCORE_RE = re.compile(r"\b\d{1,2}[-–]\d{1,2}\b")
EXACT_KW_RE = re.compile(r"exact.?score|correct.?score", re.I)
SUFFIX_RE = re.compile(
    r"\s*[-–]\s*(exact score|halftime result|more markets|halftime).*$", re.I
)
VS_RE = re.compile(r"\s+vs\.?\s+", re.I)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ScoreMarket:
    scoreline: str
    probability: float
    best_bid: Optional[float]
    best_ask: Optional[float]
    last_trade: Optional[float]
    market_id: str


@dataclass
class MatchInfo:
    event_id: str
    team_home: str
    team_away: str
    kickoff_utc: datetime
    has_exact_score: bool
    score_markets: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def build_session(max_retries: int = 3, backoff_factor: float = 2.0) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {"Accept": "application/json", "User-Agent": "wc-scores-dashboard/1.0"}
    )
    return session


def fetch_page(session, tag_id: int, limit: int, offset: int) -> list:
    url = f"{BASE_URL}/events"
    params = {"tag_id": tag_id, "closed": "false", "limit": limit, "offset": offset}
    resp = session.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def fetch_all_events(session, tag_id: int, page_size: int = 100) -> list:
    results = []
    offset = 0
    while True:
        page = fetch_page(session, tag_id, page_size, offset)
        results.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return results


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_prices(raw) -> list:
    if isinstance(raw, str):
        parsed = json.loads(raw)
    else:
        parsed = raw
    return [float(p) for p in parsed]


def parse_team_names(title: str) -> tuple:
    clean = SUFFIX_RE.sub("", title).strip()
    if ":" in clean:
        clean = clean.split(":", 1)[1].strip()
    parts = VS_RE.split(clean, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "Unknown", "Unknown"


def is_exact_score_event(event: dict) -> bool:
    return bool(EXACT_KW_RE.search(event.get("title", "")))


def is_exact_score_market(market: dict) -> bool:
    question = market.get("question", "")
    group_title = market.get("groupItemTitle", "")
    if EXACT_KW_RE.search(question):
        return True
    if SCORE_RE.search(group_title):
        return True
    if SCORE_RE.search(question):
        return True
    return False


def parse_score_markets(raw_markets: list) -> list:
    results = []
    for m in raw_markets:
        if not is_exact_score_market(m):
            continue
        try:
            prices = _parse_prices(m.get("outcomePrices", "[]"))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not prices:
            continue
        prob = prices[0]

        group_title = m.get("groupItemTitle", "")
        question = m.get("question", "")
        if SCORE_RE.search(group_title) or EXACT_KW_RE.search(group_title):
            scoreline = group_title
        else:
            match = SCORE_RE.search(question)
            scoreline = match.group(0) if match else group_title or question[:40]

        results.append(
            ScoreMarket(
                scoreline=scoreline,
                probability=prob,
                best_bid=m.get("bestBid"),
                best_ask=m.get("bestAsk"),
                last_trade=m.get("lastTradePrice"),
                market_id=str(m.get("id", "")),
            )
        )
    results.sort(key=lambda x: x.probability, reverse=True)
    return results


def build_match_infos(events: list, now_utc: datetime, window_hours: float) -> list:
    deadline = now_utc + timedelta(hours=window_hours)

    exact_score_map: dict = {}
    plain_match_map: dict = {}

    for event in events:
        end_dt = _parse_dt(event.get("endDate", ""))
        if end_dt is None:
            continue
        if not (now_utc <= end_dt <= deadline):
            continue

        title = event.get("title", "")
        is_exact = is_exact_score_event(event)

        clean_title = title.split(":", 1)[1].strip() if ":" in title else title
        has_vs = bool(VS_RE.search(clean_title))
        has_aux_suffix = bool(re.search(r"\s[-–]\s", clean_title))
        is_question = "?" in clean_title

        if is_exact:
            pass
        elif has_vs and not has_aux_suffix and not is_question:
            parts = VS_RE.split(clean_title, maxsplit=1)
            if len(parts) == 2 and len(parts[0].split()) <= 5 and len(parts[1].split()) <= 5:
                pass
            else:
                continue
        else:
            continue

        home, away = parse_team_names(title)
        key = frozenset({home, away})
        markets = event.get("markets", [])

        if is_exact:
            score_markets = parse_score_markets(markets)
            info = MatchInfo(
                event_id=str(event.get("id", "")),
                team_home=home,
                team_away=away,
                kickoff_utc=end_dt,
                has_exact_score=True,
                score_markets=score_markets,
            )
            if key not in exact_score_map or len(score_markets) > len(
                exact_score_map[key].score_markets
            ):
                exact_score_map[key] = info
        else:
            if key not in plain_match_map:
                plain_match_map[key] = MatchInfo(
                    event_id=str(event.get("id", "")),
                    team_home=home,
                    team_away=away,
                    kickoff_utc=end_dt,
                    has_exact_score=False,
                    score_markets=[],
                )

    combined: dict = dict(exact_score_map)
    for key, info in plain_match_map.items():
        if key not in combined:
            combined[key] = info

    return sorted(combined.values(), key=lambda m: m.kickoff_utc)


# ---------------------------------------------------------------------------
# Shared state (updated by background thread, read by HTTP handler)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_state = {
    "matches": [],
    "last_fetch_iso": None,
    "error": None,
    "fetching": False,
}


def _matches_to_json(matches: list) -> list:
    out = []
    for m in matches:
        out.append(
            {
                "home": m.team_home,
                "away": m.team_away,
                "kickoff_iso": m.kickoff_utc.isoformat(),
                "has_exact_score": m.has_exact_score,
                "markets": [
                    {
                        "scoreline": s.scoreline,
                        "probability": s.probability,
                        "best_bid": s.best_bid,
                        "best_ask": s.best_ask,
                        "last_trade": s.last_trade,
                    }
                    for s in m.score_markets
                ],
            }
        )
    return out


def fetch_loop(args: argparse.Namespace) -> None:
    session = build_session()
    while True:
        with _state_lock:
            _state["fetching"] = True
        try:
            now_utc = datetime.now(timezone.utc)
            events = fetch_all_events(session, args.tag_id, args.page_size)
            matches = build_match_infos(events, now_utc, args.hours)
            with _state_lock:
                _state["matches"] = _matches_to_json(matches)
                _state["last_fetch_iso"] = now_utc.isoformat()
                _state["error"] = None
        except Exception as exc:
            with _state_lock:
                _state["error"] = str(exc)
        finally:
            with _state_lock:
                _state["fetching"] = False
        time.sleep(args.interval)


# ---------------------------------------------------------------------------
# HTML template (self-contained, no CDN)
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>World Cup Exact Scores · Polymarket</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3350;
    --text: #e8eaf0;
    --dim: #6b7280;
    --accent: #3b82f6;
    --green: #22c55e;
    --yellow: #f59e0b;
    --red: #ef4444;
    --bar-bg: #1e293b;
    --bar-fill: #3b82f6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.5;
  }
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 10;
  }
  header h1 { font-size: 16px; font-weight: 600; letter-spacing: 0.02em; }
  header h1 span { color: var(--accent); }
  #status { font-size: 12px; color: var(--dim); text-align: right; }
  #status .dot {
    display: inline-block; width: 7px; height: 7px;
    border-radius: 50%; background: var(--green);
    margin-right: 5px; vertical-align: middle;
  }
  #status .dot.loading { background: var(--yellow); animation: pulse 1s infinite; }
  #status .dot.error   { background: var(--red); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

  main { max-width: 900px; margin: 0 auto; padding: 24px 16px; }

  .empty {
    text-align: center; padding: 60px 20px; color: var(--dim);
    border: 1px dashed var(--border); border-radius: 8px;
  }
  .empty h2 { margin-bottom: 8px; font-size: 18px; }

  .match-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 20px;
    overflow: hidden;
  }
  .match-header {
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
  }
  .teams {
    font-size: 17px;
    font-weight: 700;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .vs { font-size: 12px; color: var(--dim); font-weight: 400; }
  .kickoff {
    font-size: 12px;
    color: var(--dim);
    text-align: right;
    white-space: nowrap;
  }
  .kickoff .countdown {
    color: var(--yellow);
    font-weight: 600;
  }
  .kickoff .soon { color: var(--red); }

  .no-market {
    padding: 16px 20px;
    color: var(--dim);
    font-style: italic;
  }

  table {
    width: 100%;
    border-collapse: collapse;
  }
  thead th {
    padding: 8px 12px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--dim);
    text-align: left;
    border-bottom: 1px solid var(--border);
    background: var(--surface2);
  }
  thead th.num { text-align: right; }
  tbody tr { border-bottom: 1px solid var(--border); }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: var(--surface2); }
  td {
    padding: 9px 12px;
    vertical-align: middle;
  }
  td.rank { color: var(--dim); font-size: 12px; width: 28px; }
  td.scoreline { font-weight: 500; }
  td.scoreline.any-other { color: var(--dim); font-style: italic; font-weight: 400; }
  td.prob { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; width: 60px; }
  td.prob.high { color: var(--green); }
  td.prob.mid  { color: var(--text); }
  td.prob.low  { color: var(--dim); }
  td.bar { width: 140px; padding-right: 16px; }
  .bar-track {
    height: 6px;
    background: var(--bar-bg);
    border-radius: 3px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    border-radius: 3px;
    background: var(--bar-fill);
    transition: width 0.4s ease;
  }
  .bar-fill.high { background: var(--green); }
  .bar-fill.mid  { background: var(--accent); }
  .bar-fill.low  { background: var(--dim); }
  td.price { text-align: right; font-variant-numeric: tabular-nums; color: var(--dim); font-size: 12px; width: 58px; }

  footer {
    text-align: center;
    color: var(--dim);
    font-size: 12px;
    padding: 24px 16px;
    border-top: 1px solid var(--border);
    margin-top: 8px;
  }
</style>
</head>
<body>
<header>
  <h1>World Cup · <span>Exact Scores</span> · Polymarket</h1>
  <div id="status"><span class="dot loading" id="dot"></span><span id="status-text">Loading…</span></div>
</header>
<main id="main"></main>
<footer id="footer">Refreshes automatically every <span id="interval-display">—</span>s · data from Polymarket Gamma API</footer>

<script>
const API = '/api/data';
let refreshInterval = 60;
let countdown = refreshInterval;
let timer = null;

function fmtPrice(v) {
  if (v == null) return '—';
  return v.toFixed(3);
}

function probClass(p) {
  if (p >= 0.10) return 'high';
  if (p >= 0.05) return 'mid';
  return 'low';
}

function fmtKickoff(isoStr) {
  const d = new Date(isoStr);
  const now = new Date();
  const diffMs = d - now;
  const diffH = diffMs / 3600000;

  const dateStr = d.toLocaleString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZoneName: 'short'
  });

  let countdown = '';
  if (diffMs < 0) {
    countdown = '<span class="countdown soon">In progress</span>';
  } else if (diffH < 1) {
    const m = Math.floor(diffMs / 60000);
    countdown = `<span class="countdown soon">in ${m}m</span>`;
  } else if (diffH < 2) {
    const h = Math.floor(diffH);
    const m = Math.floor((diffMs % 3600000) / 60000);
    countdown = `<span class="countdown soon">in ${h}h ${m}m</span>`;
  } else {
    const h = Math.floor(diffH);
    const m = Math.floor((diffMs % 3600000) / 60000);
    countdown = `<span class="countdown">in ${h}h ${m}m</span>`;
  }

  return `${dateStr} &nbsp; ${countdown}`;
}

function isAnyOther(scoreline) {
  return /any other/i.test(scoreline);
}

function renderMatch(m) {
  const hdr = `
    <div class="match-header">
      <div class="teams">
        ${m.home} <span class="vs">vs</span> ${m.away}
      </div>
      <div class="kickoff">${fmtKickoff(m.kickoff_iso)}</div>
    </div>`;

  if (!m.has_exact_score) {
    return `<div class="match-card">${hdr}<div class="no-market">No exact-score market available</div></div>`;
  }
  if (!m.markets.length) {
    return `<div class="match-card">${hdr}<div class="no-market">Market exists but no active lines yet</div></div>`;
  }

  const maxProb = m.markets[0].probability;

  const rows = m.markets.map((s, i) => {
    const pc = probClass(s.probability);
    const pct = (s.probability * 100).toFixed(1) + '%';
    const barW = maxProb > 0 ? ((s.probability / maxProb) * 100).toFixed(1) : 0;
    const slClass = isAnyOther(s.scoreline) ? 'scoreline any-other' : 'scoreline';
    return `<tr>
      <td class="rank">${i + 1}</td>
      <td class="${slClass}">${s.scoreline}</td>
      <td class="prob ${pc}">${pct}</td>
      <td class="bar"><div class="bar-track"><div class="bar-fill ${pc}" style="width:${barW}%"></div></div></td>
      <td class="price">${fmtPrice(s.best_bid)}</td>
      <td class="price">${fmtPrice(s.best_ask)}</td>
      <td class="price">${fmtPrice(s.last_trade)}</td>
    </tr>`;
  }).join('');

  const tbl = `<table>
    <thead><tr>
      <th>#</th><th>Scoreline</th>
      <th class="num">Prob</th><th></th>
      <th class="num">Bid</th><th class="num">Ask</th><th class="num">Last</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  return `<div class="match-card">${hdr}${tbl}</div>`;
}

function render(data) {
  const main = document.getElementById('main');

  if (data.error) {
    main.innerHTML = `<div class="empty"><h2>Fetch error</h2><p>${data.error}</p></div>`;
    return;
  }

  refreshInterval = data.interval;
  document.getElementById('interval-display').textContent = refreshInterval;

  if (!data.matches.length) {
    main.innerHTML = `<div class="empty">
      <h2>No upcoming matches</h2>
      <p>No World Cup matches found kicking off in the next ${data.hours}h.<br>
      Polymarket will add markets as matches approach.</p>
    </div>`;
    return;
  }

  main.innerHTML = data.matches.map(renderMatch).join('');
}

function setStatus(state, text) {
  document.getElementById('dot').className = 'dot ' + state;
  document.getElementById('status-text').textContent = text;
}

async function fetchData() {
  setStatus('loading', 'Fetching…');
  try {
    const r = await fetch(API);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    render(data);

    const ts = data.last_fetch ? new Date(data.last_fetch).toLocaleTimeString() : '—';
    setStatus('', `Updated ${ts}`);
    document.getElementById('dot').className = 'dot';
  } catch (e) {
    setStatus('error', 'Error: ' + e.message);
  }
}

function startCountdown() {
  if (timer) clearInterval(timer);
  countdown = refreshInterval;
  timer = setInterval(() => {
    countdown--;
    if (countdown <= 0) {
      clearInterval(timer);
      fetchData().then(startCountdown);
    } else {
      const ts = document.getElementById('status-text').textContent;
      // Only update the countdown portion, leave the "Updated HH:MM" text alone
      if (!ts.startsWith('Fetching')) {
        const base = ts.replace(/ · next in \d+s$/, '');
        setStatus('', `${base} · next in ${countdown}s`);
      }
    }
  }, 1000);
}

fetchData().then(startCountdown);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    args: argparse.Namespace  # injected before server starts

    def log_message(self, fmt, *a):
        pass  # suppress access logs

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send(200, "text/html; charset=utf-8", HTML.encode())
        elif self.path == "/api/data":
            with _state_lock:
                payload = {
                    "matches": _state["matches"],
                    "last_fetch": _state["last_fetch_iso"],
                    "error": _state["error"],
                    "fetching": _state["fetching"],
                    "interval": self.args.interval,
                    "hours": self.args.hours,
                }
            self._send(200, "application/json", json.dumps(payload).encode())
        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Polymarket World Cup exact-score web dashboard.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port", type=int, default=8081, metavar="N",
                   help="HTTP port to listen on")
    p.add_argument("--host", default="0.0.0.0", metavar="ADDR",
                   help="Address to bind (0.0.0.0 = all interfaces, reachable via Tailscale)")
    p.add_argument("--tag-id", type=int, default=102232, metavar="N",
                   help="Polymarket tag ID")
    p.add_argument("--hours", type=float, default=24.0, metavar="N",
                   help="Kickoff window in hours from now")
    p.add_argument("--interval", type=int, default=60, metavar="N",
                   help="Data refresh interval in seconds")
    p.add_argument("--page-size", type=int, default=100, metavar="N",
                   help="API page size")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Inject args into handler class
    Handler.args = args

    # Start background data fetcher
    t = threading.Thread(target=fetch_loop, args=(args,), daemon=True)
    t.start()

    server = HTTPServer((args.host, args.port), Handler)
    print(f"Dashboard running at http://{args.host}:{args.port}/")
    print(f"  tag_id={args.tag_id}  window={args.hours}h  refresh={args.interval}s")
    print("  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
