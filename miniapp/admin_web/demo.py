"""Local pilot only: a demo data set built through the REAL classes (Store, ForecastHistory,
the frozen model's score matrix), plus clearly labelled demo telemetry.

Nothing here touches a real database: `seed` refuses a directory that already holds data,
and every demo user id lies in DEMO_ID_BASE.. so it can never be mistaken for a real one.
Demo telemetry is generated on the fly and is served only when the pilot was started in
pilot mode AND the viewer explicitly asked for it; every response carries source="demo".
"""
from __future__ import annotations

import hashlib
import json
import random
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from miniapp.forecast.history import ForecastHistory, ForecastRecord
from miniapp.forecast.markets import display_percentages, probabilities
from miniapp.forecast.scores import score_matrix
from miniapp.forecast.version import MODEL_VERSION
from miniapp.store import BurnWindow, Store
from miniapp.telegram_auth import TelegramUser

DEMO_ID_BASE = 990_000_000

_FIRST = ["Али", "Лейла", "Тимур", "Айгерим", "Мурат", "Elvin", "Nigar", "Emre", "Zeynep", "Rustam", "Dilnoza",
          "Kamran", "Aysel", "Sanjar", "Madina", "Orhan", "Farid", "Gulnara", "Ilkin", "Asel"]
_LAST = ["Алиев", "Мамедова", "Каримов", "Нурланова", "Yilmaz", "Kaya", "Huseynov", None, None, "Rashidova"]
_LANGS = ["ru", "ru", "ru", "az", "az", "en", "tr", "uz"]

_TEAMS = {
    "fd:PL": ["Манчестер Сити", "Фулхэм", "Арсенал", "Ливерпуль", "Челси", "Брентфорд", "Эвертон", "Тоттенхэм"],
    "fd:PD": ["Реал Мадрид", "Барселона", "Атлетико", "Севилья", "Бетис", "Вильярреал"],
    "fd:SA": ["Интер", "Милан", "Ювентус", "Наполи", "Рома", "Аталанта"],
    "fd:BL1": ["Бавария", "Боруссия Д", "Лейпциг", "Байер", "Штутгарт", "Вольфсбург"],
    "fd:FL1": ["ПСЖ", "Марсель", "Монако", "Лион", "Лилль", "Ланс"],
}


def _team_id(name: str) -> str:
    return f"fd:team:{int(hashlib.sha256(name.encode()).hexdigest()[:6], 16) % 9000 + 1}"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(data_dir: Path, now: datetime | None = None) -> dict:
    """Creates miniapp.db, miniapp.forecasts.db and the labels file of the demo set."""
    now = now or datetime.now(UTC).replace(microsecond=0)
    db = data_dir / "miniapp.db"
    if db.exists() or (data_dir / "miniapp.forecasts.db").exists():
        raise SystemExit(f"refused: {data_dir} already holds data (delete the pilot directory to re-seed)")
    rng = random.Random(20261110)
    store = Store(db)
    store.init_schema()
    ts = int(now.timestamp())
    burn = BurnWindow.for_policy(ts, 300, 30)
    for i in range(64):
        uid = DEMO_ID_BASE + 1000 + i * 37
        first = rng.choice(_FIRST)
        created = ts - int(rng.uniform(0, 120) * 86400)
        user = TelegramUser(id=uid, first_name=first, last_name=rng.choice(_LAST),
                            username=None if rng.random() < 0.2 else f"demo_{first.lower()}_{i}"
                            if first.isascii() else f"demo_user_{i}", language_code=rng.choice(_LANGS))
        store.upsert_user(user, created)
        last_login = min(ts, created + int(rng.uniform(0, (ts - created) / 86400 + 1) * 86400))
        if rng.random() < 0.75:
            store.upsert_user(user, last_login)
        for _ in range(rng.choice([0, 1, 1, 2, 3])):
            start = last_login - int(rng.uniform(0, 3) * 86400)
            token = secrets.token_urlsafe(24)
            store.create_session(token, uid, secrets.token_hex(16), start, 7 * 86400)
            if rng.random() < 0.15:
                store.revoke_user_sessions(uid, start + 3600, burn)

    history = ForecastHistory(data_dir / "miniapp.forecasts.db")
    history.init_schema()
    labels: dict[str, str] = {}
    to_settle = []
    corrections = []
    event_no = 560600
    for days in list(range(-26, 0, 1)) + [1, 2, 3, 4, 6, 9, 11, 13]:
        for tournament in rng.sample(list(_TEAMS), k=1 if days > 0 else rng.choice([1, 1, 2])):
            home, away = rng.sample(_TEAMS[tournament], 2)
            event_no += 1
            event_id = f"fd:{event_no}"
            kickoff = (now + timedelta(days=days)).replace(hour=rng.choice([12, 15, 17, 19, 20]), minute=rng.choice(
                [0, 30]), second=0)
            created = kickoff - timedelta(hours=rng.choice([3, 6, 20, 30]))
            if created >= now:
                created = now - timedelta(hours=1)
            lam, mu = rng.uniform(0.8, 2.3), rng.uniform(0.6, 1.8)
            exact = probabilities(score_matrix(lam, mu, -0.05))
            pick = max(("1", "X", "2", "O2.5", "U2.5", "BTTS_Y"), key=lambda k: exact[k])
            no_bet = exact[pick] < 0.52 or rng.random() < 0.25
            digest = hashlib.sha256(f"{event_id}:{lam:.3f}".encode()).hexdigest()[:16]
            record = ForecastRecord(
                forecast_key=f"{MODEL_VERSION}:{event_id}:{digest}:demo", event_id=event_id, tournament_id=tournament,
                home_team_id=_team_id(home), away_team_id=_team_id(away),
                created_at=_iso(created), kickoff_at=_iso(kickoff), model_version=MODEL_VERSION, parameters="demo",
                data_until=_iso(created - timedelta(days=3)), sample=rng.randint(180, 380), inputs_digest=digest,
                probabilities={k: round(v, 6) for k, v in exact.items()},
                baseline={"1": 0.45, "X": 0.27, "2": 0.28}, gate_eligible=tournament != "fd:CL",
                display=display_percentages(exact), expected_goals=(round(lam, 3), round(mu, 3)),
                pick_market="NO_BET" if no_bet else pick, pick_probability=None if no_bet else round(exact[pick], 6),
                no_bet_reason="low_edge" if no_bet else None, alternatives=[], explanation={"factors": [], "reasons": []},
                llm_model="demo", source="demo")
            history.record(record)
            labels[event_id] = f"{home} — {away}"
            if kickoff < now - timedelta(hours=3):
                roll = rng.random()
                if roll < 0.06:
                    to_settle.append(SimpleNamespace(id=event_id, sport="football", status="cancelled"))
                elif roll < 0.1:
                    pass  # postponed: stays pending, like the real settlement does
                else:
                    score = (rng.choice([0, 1, 1, 2, 2, 3, 4]), rng.choice([0, 0, 1, 1, 2, 3]))
                    to_settle.append(SimpleNamespace(id=event_id, sport="football", status="finished",
                                                     awarded=False, regular_score=score))
                    if rng.random() < 0.08:
                        corrections.append(SimpleNamespace(id=event_id, sport="football", status="finished",
                                                           awarded=False, regular_score=(score[0], score[1] + 1)))
    history.settle_events(to_settle)
    history.settle_events(corrections)  # a score correction: revision 2 in the settlement journal

    (data_dir / "event_labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=1))
    meta = {"dataset": "demo", "createdAt": _iso(now), "users": 64, "forecasts": len(labels)}
    (data_dir / "pilot.json").write_text(json.dumps(meta, indent=1))
    return meta


# ---- demo telemetry (never stored; regenerated per request) ------------------------------------

def _day_rng(day: str, salt: str) -> random.Random:
    return random.Random(f"{salt}:{day}")


def telemetry(days: int, today: datetime) -> dict:
    """Daily series for the requested period, deterministic per calendar day."""
    series = []
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        r = _day_rng(day, "activity")
        dau = r.randint(18, 46)
        ai_created = r.randint(10, 40)
        series.append({
            "day": day, "dau": dau, "new": r.randint(0, 5), "aiCreated": ai_created,
            "aiOpened": ai_created + r.randint(5, 30), "quick": r.randint(5, 25), "coupons": r.randint(1, 9),
            "aiCostUsd": round(ai_created * r.uniform(0.011, 0.019), 3),
            "apiAttempts": r.randint(800, 1600), "api429": r.choice([0, 0, 0, 1, 2, 4]),
            "apiErrors": r.choice([0, 0, 1, 2, 3]),
        })
    return {"source": "demo", "series": series,
            "wau": min(64, sum(d["dau"] for d in series[-7:]) // 3), "mau": min(64, sum(d["dau"] for d in series) // 6)}


_ERRORS = ["upstream_timeout", "rate_limited", "invalid_answer", "refusal", "upstream_error"]


def ai_calls(days: int, today: datetime) -> list[dict]:
    calls = []
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        r = _day_rng(day.strftime("%Y-%m-%d"), "ai")
        for n in range(r.randint(6, 14)):
            at = day.replace(hour=r.randint(0, 23), minute=r.randint(0, 59), second=r.randint(0, 59))
            if at > today:
                continue
            failed = r.random() < 0.08
            unknown_cost = failed and r.random() < 0.5
            inp, out = r.randint(2500, 5200), r.randint(350, 900)
            calls.append({
                "id": f"demo-{day:%Y%m%d}-{n}", "at": int(at.timestamp()), "operation": r.choice(
                    ["AI-разбор", "AI-разбор", "AI-разбор", "Повтор разбора"]),
                "model": r.choice(["claude-sonnet-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]),
                "inputTokens": None if unknown_cost else inp, "outputTokens": None if unknown_cost else out,
                "costUsd": None if unknown_cost else round(inp * 3e-6 + out * 15e-6, 4),
                "latencyMs": r.randint(2400, 9800),
                "result": r.choice(_ERRORS) if failed else ("delivered" if r.random() > 0.05 else "not_delivered"),
                "userId": DEMO_ID_BASE + 1000 + r.randrange(64) * 37,
            })
    calls.sort(key=lambda c: c["at"], reverse=True)
    return calls


def api_providers(today: datetime) -> list[dict]:
    r = _day_rng(today.strftime("%Y-%m-%d"), "api")
    return [
        {"provider": "football-data.org", "dailyLimit": None, "perMinuteLimit": 10, "usedLastMinute": r.randint(0, 6),
         "attempts": r.randint(900, 1500), "rateLimited": r.choice([0, 1, 2]), "errors": r.choice([0, 1, 3]),
         "lastOkAt": int((today - timedelta(seconds=r.randint(5, 90))).timestamp()), "status": "ok"},
        {"provider": "Коэффициенты", "dailyLimit": None, "perMinuteLimit": None, "usedLastMinute": None,
         "attempts": 0, "rateLimited": 0, "errors": 0, "lastOkAt": None, "status": "disabled"},
    ]


def system_events(today: datetime) -> list[dict]:
    r = random.Random(today.strftime("%Y-%m-%d"))
    components = ["ai", "sports", "settlement", "auth", "quick"]
    codes = {"ai": ["upstream_timeout", "invalid_answer"], "sports": ["rate_limited", "upstream_error"],
             "settlement": ["upstream_error"], "auth": ["init_data_expired"], "quick": ["no_events"]}
    out = []
    for i in range(36):
        comp = r.choice(components)
        at = today - timedelta(minutes=r.randint(1, 60 * 24 * 3))
        out.append({"at": int(at.timestamp()), "component": comp, "level": "error" if r.random() < 0.4 else "warning",
                    "code": r.choice(codes[comp]), "count": r.randint(1, 6), "requestId": f"demo{r.getrandbits(48):012x}"})
    out.sort(key=lambda e: e["at"], reverse=True)
    return out


def tasks(today: datetime) -> list[dict]:
    t = int(today.timestamp())
    return [
        {"name": "Сверка результатов", "lastOkAt": t - 240, "lastError": None, "state": "ok"},
        {"name": "Прогрев таблиц", "lastOkAt": t - 1300, "lastError": None, "state": "ok"},
        {"name": "Индекс поиска", "lastOkAt": t - 5400, "lastError": "rate_limited", "state": "warning"},
    ]
