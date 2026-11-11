"""AI quality check on match days (20-22 Nov 2026): PL, PD, BL1, SA — at most 20 paid calls, at most $10.

Runs LOCALLY on the release code (b6cf031) with real football-data and Anthropic keys, its own forecast
history file and its own state; never touches the stand or production, never places bets.

    PYTHONPATH=. python deploy/miniapp/ai_matchday_check.py <state dir> select [YYYY-MM-DD] [--per-league N]  # free: picks the day's matches
    PYTHONPATH=. python deploy/miniapp/ai_matchday_check.py <state dir> run                   # paid: analyses the selection
    PYTHONPATH=. python deploy/miniapp/ai_matchday_check.py <state dir> settle                # free: results after the matches
    PYTHONPATH=. python deploy/miniapp/ai_matchday_check.py <state dir> report                # criteria + per-match table

Keys come from the environment (MINIAPP_FOOTBALL_DATA_KEY, MINIAPP_ANTHROPIC_API_KEY), or else from
<state dir>/football_key and <state dir>/anthropic_key (0600); never printed.
Every paid request is RESERVED in ledger.json before it is sent (a crash or timeout still counts against
the 20 calls / $10 with its worst-case cost) and reconciled with the reported usage afterwards.
Only matches kicking off on the chosen UTC day and at least LEAD_MIN minutes ahead are analysed, so no
data from after kickoff can enter; the report re-checks it for every row.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
from datetime import UTC, date, datetime
from pathlib import Path

from miniapp.ai import analysis as A
from miniapp.forecast.history import ForecastHistory
from miniapp.settings import _competitions
from miniapp.sports.service import build_service, parse_instant

LEAGUES = ("PL", "PD", "BL1", "SA")
PER_LEAGUE = 5
MAX_CALLS = 20
CAP_USD = 10.0
PRICE_IN, PRICE_OUT = 4.0 / 1e6, 20.0 / 1e6  # claude-opus-5-5; thinking is billed as output
WORST_CALL_USD = 12000 * PRICE_IN + 8000 * PRICE_OUT  # generous input + max_tokens output
LEAD_MIN = 60

STATE = Path(sys.argv[1])
MODE = sys.argv[2]
STATE.mkdir(parents=True, exist_ok=True)
LEDGER = STATE / "ledger.json"  # every analysed match across the three days


def load(name: str, default):
    p = STATE / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name: str, data) -> None:
    (STATE / name).write_text(json.dumps(data, ensure_ascii=False, indent=1))


def secret(env: str, file: str) -> str:
    return (os.environ.get(env) or (STATE / file).read_text()).strip()


def settings():
    return types.SimpleNamespace(
        football_data_key=secret("MINIAPP_FOOTBALL_DATA_KEY", "football_key"), football_data_per_minute=40,
        football_data_per_day=2000, football_data_competitions=_competitions("all"),
        sports_unknown_lookups_per_hour=60, football_data_live_plan=True,
        anthropic_api_key=secret("MINIAPP_ANTHROPIC_API_KEY", "anthropic_key"), ai_model="claude-opus-5-5",
        ai_per_user_daily=1000, ai_daily_budget=1000)


def category(home: int | None, away: int | None, teams: int) -> str:
    if home is None or away is None:
        return "no_table"
    top, bottom, d = teams // 4, teams - teams // 4, away - home
    if home <= top and away <= top:
        return "top_vs_top"
    if home > bottom and away > bottom:
        return "bottom_vs_bottom"
    if d >= teams // 2:
        return "home_favourite"
    if -d >= teams // 2:
        return "away_favourite"
    return "balanced"


async def select(svc, day: date, today_max: int = PER_LEAGUE) -> list[dict]:
    done = load("ledger.json", [])
    per_league = {code: sum(1 for r in done if r["league"] == code) for code in LEAGUES}
    chosen = []
    for code in LEAGUES:
        room = min(PER_LEAGUE - per_league[code], today_max)  # spread the 5 per league over the three days
        if room <= 0:
            continue
        cal = await svc.calendar("football", f"fd:{code}")
        table = await svc.standings(f"fd:{code}")
        rows = []
        for e in cal.events:
            kick = parse_instant(e.starts_at)
            if e.status != "upcoming" or kick.date() != day or kick.timestamp() - LEAD_MIN * 60 < time.time():
                continue
            h, a = table.get(e.home.id), table.get(e.away.id)
            rows.append({"event": e.id, "league": code, "home": e.home.name, "away": e.away.name, "kickoff": e.starts_at,
                         "homePos": h.position if h else None, "awayPos": a.position if a else None,
                         "category": category(h.position if h else None, a.position if a else None, len(table))})
        picked, cats = [], {r["category"] for r in done if r["league"] == code}
        for r in sorted(rows, key=lambda r: (r["category"] in cats, r["kickoff"])):  # new kinds of match first
            if len(picked) < room:
                picked.append(r); cats.add(r["category"])
        chosen += picked
    return chosen


async def run(svc, ai, client) -> None:
    ledger = load("ledger.json", [])
    usage: list = []  # paid responses of the current analysis (expected: one)
    real = client._client.beta.messages.create

    async def create(**kw):
        # Durable reservation BEFORE the request leaves: counted even if it then fails or the process dies.
        entry["paidCalls"] = entry.get("paidCalls", 0) + 1
        entry["costUsd"] = round(entry.get("costUsd", 0) + WORST_CALL_USD, 5)
        entry["reserved"] = True
        save("ledger.json", ledger)
        resp = await real(**kw)
        usage.append(resp.usage)
        return resp

    client._client.beta.messages.create = create
    for r in load("selection.json", []):
        spent = sum(x.get("costUsd", 0) for x in ledger)
        calls = sum(x.get("paidCalls", 0) for x in ledger)
        # Before each analysis: room for its worst case in both limits (one request = at most one paid call;
        # a failed analysis is repeated at most once, by a later `run`, and that repeat is checked here too).
        if calls + 1 > MAX_CALLS or spent + WORST_CALL_USD > CAP_USD:
            print(f"STOP: limits (calls {calls}/{MAX_CALLS}, spent ${spent:.2f}/${CAP_USD})"); break
        tries = [x for x in ledger if x["event"] == r["event"]]
        if any(x.get("status") in ("ok", "skipped_started") for x in tries) or len(tries) >= 2:
            continue  # done, or already retried once: a failed analysis is repeated at most once (next `run`)
        if parse_instant(r["kickoff"]).timestamp() - A.KICKOFF_MARGIN_S - 60 <= time.time():
            ledger.append({**r, "status": "skipped_started"}); continue
        usage.clear()
        t0 = time.monotonic()
        entry = {**r, "requestedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "in_flight"}
        ledger.append(entry)
        try:
            res = await ai.analyse(r["event"], 1, create=True)
            entry.update(status="ok", result=res.to_json() if res else None)
        except A.AiError as exc:
            entry.update(status="error", error=exc.code)
        except Exception as exc:  # noqa: BLE001 — recorded, the run goes on
            entry.update(status="error", error=type(exc).__name__)
        entry["seconds"] = round(time.monotonic() - t0, 2)
        if len(usage) == entry.get("paidCalls", 0):  # every sent request answered: real cost replaces the reserve
            ins = sum((u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
                      + (getattr(u, "cache_read_input_tokens", 0) or 0) for u in usage)
            out = sum(u.output_tokens or 0 for u in usage)
            entry.update(inputTokens=ins, outputTokens=out, costUsd=round(ins * PRICE_IN + out * PRICE_OUT, 5),
                         reserved=False)
            if ins > 12000:
                save("ledger.json", ledger)
                print("STOP: a prompt exceeded the 12 000-token bound the budget assumes"); break
        save("ledger.json", ledger)
        res = entry.get("result") or {}
        print(f"{r['league']:4} {r['home'][:20]:20} - {r['away'][:20]:20} {entry['status']:5} "
              f"{entry.get('error') or (res.get('pick') or {}).get('market')} {entry['seconds']}s ${entry.get('costUsd', 0):.4f}")


async def settle(svc, history: ForecastHistory) -> None:
    for code in LEAGUES:
        cal = await svc.calendar("football", f"fd:{code}")
        print(code, "settled rows:", await asyncio.to_thread(history.settle_events, cal.events))
    # Independent reference for the report: the provider's match record (one request per analysed match).
    import urllib.request
    key = settings().football_data_key
    source = load("source_results.json", {})
    for x in load("ledger.json", []):
        if x.get("status") != "ok" or x["event"] in source and source[x["event"]]["status"] == "FINISHED":
            continue
        req = urllib.request.Request(f"https://api.football-data.org/v4/matches/{x['event'].split(':')[1]}",
                                     headers={"X-Auth-Token": key})
        m = json.load(urllib.request.urlopen(req, timeout=15))
        sc = m.get("score") or {}
        ft = (sc.get("fullTime") if sc.get("duration") == "REGULAR" else sc.get("regularTime")) or {}
        source[x["event"]] = {"status": m.get("status"), "score": [ft.get("home"), ft.get("away")]}
        time.sleep(1)
    save("source_results.json", source)


def report(history: ForecastHistory) -> bool:
    import sqlite3
    ledger = load("ledger.json", [])
    rows = {}
    if history.path.exists():
        c = sqlite3.connect(history.path)
        for ev, st, fh, fa in c.execute("SELECT event_id, settled_status, final_home, final_away FROM ai_forecasts"):
            rows[ev] = (st, fh, fa)
    ok_rows = [x for x in ledger if x.get("status") == "ok"]
    ok_events = {x["event"] for x in ok_rows}
    errors = [x for x in ledger if x.get("status") == "error"]
    unrecovered = sorted({x["event"] for x in errors} - ok_events)
    secs = sorted(x["seconds"] for x in ledger if "seconds" in x)
    p95 = secs[max(0, int(len(secs) * 0.95) - 1)] if secs else None
    spent = sum(x.get("costUsd", 0) for x in ledger)
    calls = sum(x.get("paidCalls", 0) for x in ledger)
    integrity = []
    for x in ok_rows:
        r = x["result"]
        f = r.get("forecast") or {}
        p = r.get("probabilities") or {}
        integrity.append(all([
            f.get("dataUntil", "") < r["generatedAt"] < x["kickoff"],
            99 <= sum(p.values()) <= 101,
            (r.get("pick") or {}).get("market") == "NO_BET" or f.get("noBetReason") is None,
            f.get("noBetReason") != "early_forecast",  # match day: never "too early"
        ]))
    finished = [x for x in ok_rows if x["kickoff"] < datetime.fromtimestamp(time.time() - 3 * 3600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")]
    source = load("source_results.json", {})  # written by `settle`: football-data's own 90-minute score
    def settled_right(x: dict) -> bool:
        st = rows.get(x["event"])
        src = source.get(x["event"])
        if src is None or st is None:
            return False
        if src["status"] == "FINISHED":
            return st[0] == "settled" and [st[1], st[2]] == src["score"]
        return st[0] in ("pending", "void")  # postponed / cancelled: never a win or a loss
    settled_ok = all(settled_right(x) for x in finished)
    criteria = {
        "1_delivery_19_of_20_and_errors_recovered": len(ok_events) >= 19 and not unrecovered,
        "2_p95_le_20s_and_every_timeout_recovered": p95 is not None and p95 <= 20 and all(
            x["event"] in ok_events for x in errors if x.get("error") == "timeout"),
        "3_calls_le_20_and_cost_le_10usd": calls <= MAX_CALLS and spent <= CAP_USD,
        "4_5_integrity_and_match_day_logic": all(integrity) and len(integrity) == len(ok_rows),
        "6_settlement_of_finished_matches": settled_ok,
    }
    print(json.dumps({
        "analysed": len(ok_rows), "errors": [(x["event"], x.get("error")) for x in errors],
        "unrecoveredErrors": unrecovered, "paidCalls": calls, "spentUsd": round(spent, 4),
        "reservedNotReconciled": sum(1 for x in ledger if x.get("reserved")), "p95Seconds": p95,
        "finishedMatches": len(finished), "criteria": criteria, "PASS": all(criteria.values()),
        "note": "20 matches: descriptive only, no statistical conclusions on accuracy",
    }, ensure_ascii=False, indent=1))
    print("| League | Match (table places) | Kickoff UTC | Kind | 1/X/2 | Pick | Reason | Result (90') | Time s | Tokens in/out | $ |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for x in ledger:
        r = x.get("result") or {}
        p = r.get("probabilities") or {}
        f = r.get("forecast") or {}
        st = rows.get(x["event"])
        res = f"{st[1]}-{st[2]}" if st and st[0] == "settled" else (st[0] if st else "—")
        print(f"| {x['league']} | {x['home']} ({x['homePos']}) – {x['away']} ({x['awayPos']}) | {x['kickoff'][:16]} | "
              f"{x.get('status') if x.get('status') != 'ok' else r.get('kind')} {x.get('error', '')} | "
              f"{p.get('home', '')}/{p.get('draw', '')}/{p.get('away', '')} | {(r.get('pick') or {}).get('market', '')} | "
              f"{f.get('noBetReason') or ''} | {res} | {x.get('seconds', '')} | {x.get('inputTokens', '')}/{x.get('outputTokens', '')} | "
              f"{x.get('costUsd', 0):.4f} |")
    return all(criteria.values())


async def main() -> None:
    s = settings()
    svc = build_service(s, warm_emblems=False)
    history = ForecastHistory(STATE / "forecasts.db")
    history.init_schema()
    if MODE == "report":
        sys.exit(0 if report(history) else 1)
    if MODE == "select":
        args = sys.argv[3:]
        today_max = int(args[args.index("--per-league") + 1]) if "--per-league" in args else PER_LEAGUE
        day = date.fromisoformat(args[0]) if args and not args[0].startswith("--") else datetime.now(UTC).date()
        chosen = await select(svc, day, today_max)
        save("selection.json", chosen)
        for r in chosen:
            print(r["league"], r["kickoff"][:16], f"{r['home']} ({r['homePos']}) - {r['away']} ({r['awayPos']})", r["category"])
        return
    if MODE == "settle":
        await settle(svc, history); return
    ai = A.build_ai_service(s, svc, ledger=A.MemoryLedger(), history=history)
    await run(svc, ai, ai.client)


asyncio.run(main())
