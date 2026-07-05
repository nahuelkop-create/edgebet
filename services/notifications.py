"""Automatic Telegram notifications, driven by a threading-based scheduler.

Background daemon threads (started from run_bot.py via start_schedulers()):

1. Pre-match (every 60 min): for each of today's fixtures kicking off within the
   next two hours, send the full Claude analysis to every user once (deduped in
   the DB so it never repeats).
2. Results (every 30 min): for each pending bet whose fixture has finished,
   grade every pick from the real API result/stats, send a detailed settlement
   message and update the balance. Bets without fixture_id are matched by name
   against fixtures from the bet date.
3. Data collectors: upcoming fixtures, finished match stats and odds snapshots
   are persisted into PostgreSQL when DATABASE_URL is configured.

Messages are sent with a plain HTTP call to the Telegram Bot API so the threads
stay completely independent of python-telegram-bot's asyncio event loop.
"""
import logging
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from collectors.fixtures_collector import collect_upcoming_fixtures
from collectors.injuries_collector import collect_injuries
from collectors.odds_collector import collect_odds
from collectors.stats_collector import collect_finished_match_stats
from models.corners_model import retrain_if_needed
from services.anthropic_client import analyze_match
from services.evaluation_engine import run_daily_evaluation
from services.football_data import (
    _get,
    get_fixtures_today,
    get_fixture_details,
    get_match_stats,
    PRE_MATCH_STATUSES,
)
from services.database import (
    get_all_users,
    get_user_chat_id,
    was_fixture_notified,
    mark_fixture_notified,
    get_all_pending_bets,
    resolve_bet,
    update_monthly_balance,
    mark_result_notified,
)

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

FINISHED_STATUSES = {"FT", "AET", "PEN"}
PRE_MATCH_WINDOW_SECONDS = 2 * 60 * 60  # notify when kickoff is <= 2h away
PRE_MATCH_INTERVAL = 60 * 60            # scheduler: pre-match check every 60 min
RESULTS_INTERVAL = 30 * 60              # scheduler: results check every 30 min
FIXTURES_COLLECTOR_INTERVAL = 6 * 60 * 60  # scheduler: upcoming fixtures every 6h
STATS_COLLECTOR_INTERVAL = 2 * 60 * 60     # scheduler: finished match stats every 2h
ODDS_COLLECTOR_INTERVAL = 2 * 60 * 60      # scheduler: odds snapshots every 2h
INJURIES_COLLECTOR_INTERVAL = 6 * 60 * 60  # scheduler: injuries every 6h
EVALUATION_ENGINE_INTERVAL = 24 * 60 * 60  # scheduler: settled prediction evaluation every 24h
MODEL_RETRAIN_INTERVAL = 7 * 24 * 60 * 60   # scheduler: model retrain every week


# --------------------------------------------------------------------------- #
# Telegram sending (plain HTTP, no event loop)
# --------------------------------------------------------------------------- #

def send_message(chat_id, text: str) -> bool:
    """Send a Telegram message via the Bot API. Returns True on success."""
    if not TELEGRAM_TOKEN:
        logging.warning("TELEGRAM_TOKEN no definido: no se pueden enviar notificaciones.")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        return resp.ok
    except Exception:
        logging.exception("Fallo enviando mensaje a Telegram (chat %s)", chat_id)
        return False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _norm(text) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().strip()


def _parse_kickoff(utc_date: str):
    if not utc_date:
        return None
    try:
        return datetime.fromisoformat(str(utc_date).replace("Z", "+00:00"))
    except ValueError:
        return None


def _profit_for(result: str, stake: float, odds: float) -> float:
    """Net profit: stake*(odds-1) if won, -stake if lost (mirrors handlers)."""
    if result == "ganada":
        return round(stake * (odds - 1), 2)
    return round(-stake, 2)


def _money(amount: float) -> str:
    """Signed money string: +$875 / -$1000 (drops the decimals when whole)."""
    sign = "+" if amount >= 0 else "-"
    value = abs(amount)
    body = f"{value:.0f}" if value == int(value) else f"{value:.2f}"
    return f"{sign}${body}"


def _first_number(text: str):
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    return float(m.group(1).replace(",", ".")) if m else None


def _numbers(text: str) -> list[float]:
    return [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", text or "")]


def _is_half_line(line) -> bool:
    """True for .5 lines (e.g. 2.5). Integer lines risk a push, so we skip them."""
    return line is not None and (int(round(line * 2)) % 2 == 1)


def _stat_total(stats: dict, *keys) -> float:
    """Sum a per-team stat across both teams (stats keyed by team name)."""
    total = 0.0
    found = False
    for team_stats in (stats or {}).values():
        for key in keys:
            value = team_stats.get(key)
            if value is None:
                continue
            try:
                total += float(str(value).replace("%", "").replace(",", ".").strip())
                found = True
            except (ValueError, TypeError):
                continue
    return total if found else None


def _split_picks(pick_text: str) -> list[str]:
    picks = [p.strip(" -•\t") for p in re.split(r"[;\n]+", pick_text or "") if p.strip(" -•\t")]
    return picks or ([pick_text.strip()] if pick_text else [])


def _fmt_total(value, unit: str, prefix: str = "") -> str:
    if value is None:
        return "sin dato"
    if float(value).is_integer():
        value_text = str(int(value))
    else:
        value_text = f"{value:.1f}"
    return f"{prefix}{value_text} {unit}".strip()


def _pick_label(ok: bool) -> str:
    return "✅" if ok else "❌"


def _message_detail(item: dict) -> str:
    if item.get("ok") is None:
        return item.get("detail", "no se pudo calcular automáticamente")
    if item.get("ok") and item.get("detail") != "CUMPLIDO":
        return item.get("detail")
    return "CUMPLIDO" if item.get("ok") else item.get("detail", "NO CUMPLIDO")


def _find_team_side(pick_text: str, home_name: str, away_name: str):
    p = _norm(pick_text)
    if home_name and _norm(home_name) in p:
        return "home"
    if away_name and _norm(away_name) in p:
        return "away"
    if "local" in p:
        return "home"
    if "visitante" in p:
        return "away"
    return None


def _fetch_fixture_player_stats(fixture_id: int) -> list[dict]:
    try:
        data = _get("/fixtures/players", params={"fixture": fixture_id})
    except Exception:
        return []

    players = []
    for team_data in data.get("response", []):
        team_name = (team_data.get("team") or {}).get("name")
        for item in team_data.get("players", []) or []:
            stat = (item.get("statistics") or [{}])[0]
            player = item.get("player", {}) or {}
            shots = stat.get("shots", {}) or {}
            fouls = stat.get("fouls", {}) or {}
            goals = stat.get("goals", {}) or {}
            cards = stat.get("cards", {}) or {}
            players.append({
                "name": player.get("name", "Unknown"),
                "team": team_name,
                "shots": int(shots.get("total") or 0),
                "shots_on_target": int(shots.get("on") or 0),
                "fouls": int(fouls.get("committed") or 0),
                "fouls_drawn": int(fouls.get("drawn") or 0),
                "saves": int(goals.get("saves") or 0),
                "yellow_cards": int(cards.get("yellow") or 0),
            })
    return players


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[\s\.]+", _norm(name)) if len(t) > 1}


def _match_player(pick_text: str, players: list[dict]):
    pick_tokens = _name_tokens(pick_text)
    best = None
    best_score = 0
    for player in players:
        tokens = _name_tokens(player.get("name"))
        if not tokens:
            continue
        score = len(tokens & pick_tokens)
        if tokens and tokens.issubset(pick_tokens):
            score += 3
        if score > best_score:
            best_score = score
            best = player
    return best if best_score > 0 else None


def _line_from_pick(text: str):
    nums = _numbers(text)
    if not nums:
        return None
    # Player props often include "+1.5"; result picks can include team names like
    # "1X2", so callers only use this after identifying a line-based market.
    return nums[-1]


def _over_under_result(text: str, raw_pick: str, line, total):
    if line is None or total is None:
        return None
    is_over = any(k in text for k in ("over", "mas de", "más de", "mas ", "más ")) or "+" in raw_pick
    is_under = any(k in text for k in ("under", "menos de", "menos ")) or ("-" in raw_pick and not is_over)
    if is_over:
        return total > line
    if is_under:
        return total < line
    return None


def _evaluate_pick(pick: str, context: dict) -> dict:
    p = _norm(pick)
    hg = context["home_goals"]
    ag = context["away_goals"]
    total_goals = hg + ag
    stats = context.get("stats") or {}

    if "corner" in p:
        line = _line_from_pick(p)
        total = _stat_total(stats, "corners")
        ok = _over_under_result(p, pick, line, total)
        if ok is not None:
            detail = _fmt_total(total, "corners", "solo " if not ok else "")
            return {"pick": pick, "ok": ok, "detail": detail}

    if any(k in p for k in ("remate", "shot", "disparo", "tiro", "falta", "ataj", "save")):
        player = _match_player(pick, context.get("players") or [])
        line = _line_from_pick(p)
        if player and line is not None:
            if any(k in p for k in ("remate", "shot", "disparo", "tiro")):
                total, unit = player.get("shots", 0), "remates"
            elif any(k in p for k in ("recib", "drawn")) and "falta" in p:
                total, unit = player.get("fouls_drawn", 0), "faltas recibidas"
            elif "falta" in p:
                total, unit = player.get("fouls", 0), "faltas"
            else:
                total, unit = player.get("saves", 0), "atajadas"
            ok = _over_under_result(p, pick, line, total)
            if ok is not None:
                return {"pick": pick, "ok": ok, "detail": _fmt_total(total, unit)}

    if "tarjeta" in p or "card" in p or "amarilla" in p:
        line = _line_from_pick(p)
        total = _stat_total(stats, "yellow_cards", "red_cards")
        ok = _over_under_result(p, pick, line, total)
        if ok is not None:
            return {"pick": pick, "ok": ok, "detail": _fmt_total(total, "tarjetas")}

    if "btts" in p or "ambos" in p or ("both" in p and "score" in p):
        both = hg > 0 and ag > 0
        negated = bool(re.search(r"\bno\b", p))
        ok = (not both) if negated else both
        return {"pick": pick, "ok": ok, "detail": "ambos anotaron" if both else "no anotaron ambos"}

    if "gol" in p or "goal" in p or "over" in p or "under" in p:
        line = _line_from_pick(p)
        ok = _over_under_result(p, pick, line, total_goals)
        if ok is not None:
            return {"pick": pick, "ok": ok, "detail": _fmt_total(total_goals, "goles")}

    winner = "home" if hg > ag else ("away" if ag > hg else "draw")
    if "empate" in p or re.search(r"\bdraw\b", p):
        return {"pick": pick, "ok": winner == "draw", "detail": "empate" if winner == "draw" else "no fue empate"}
    if any(k in p for k in ("gana", "ganador", "victoria", "win", "1x2")):
        picked = _find_team_side(pick, context.get("home_name"), context.get("away_name"))
        if picked:
            return {"pick": pick, "ok": winner == picked, "detail": "CUMPLIDO" if winner == picked else "no ganó"}

    return {"pick": pick, "ok": None, "detail": "no se pudo calcular automáticamente"}


def _evaluate_bet_picks(bet: dict, context: dict) -> list[dict]:
    picks = _split_picks(bet.get("pick", ""))
    if not picks:
        return [{"pick": "Pick sin detalle", "ok": None, "detail": "no hay texto de pick"}]
    return [_evaluate_pick(pick, context) for pick in picks]


def _ou_verdict(text: str, raw_pick: str, line, total) -> str:
    """Grade an over/under line given the realized total. Returns None if unsure."""
    if line is None or total is None or not _is_half_line(line):
        return None
    is_over = any(k in text for k in ("over", "mas de", "más de", "mas ", "más ")) or "+" in raw_pick
    is_under = any(k in text for k in ("under", "menos de", "menos ")) or ("-" in raw_pick and not is_over)
    if is_over:
        return "ganada" if total > line else "perdida"
    if is_under:
        return "ganada" if total < line else "perdida"
    return None


def grade_bet(pick: str, market: str, hg, ag, home_name, away_name, stats=None) -> str:
    """Best-effort automatic grading of a bet from the final result.

    Returns 'ganada' / 'perdida' when confident, or None when the pick can't be
    graded safely (free-text player props, exotic markets, integer lines, ...).
    """
    if hg is None or ag is None:
        return None
    p = _norm(pick) + " | " + _norm(market)
    total_goals = hg + ag

    # 1) Corners over/under (needs match statistics).
    if "corner" in p:
        line = _first_number(p)
        verdict = _ou_verdict(p, pick, line, _stat_total(stats, "corners"))
        if verdict:
            return verdict

    # 2) Cards / tarjetas over/under.
    if "tarjeta" in p or "card" in p or "amarilla" in p:
        line = _first_number(p)
        verdict = _ou_verdict(p, pick, line, _stat_total(stats, "yellow_cards", "red_cards"))
        if verdict:
            return verdict

    # 3) BTTS / ambos anotan (fully determined by the score).
    if "btts" in p or "ambos" in p or ("both" in p and "score" in p):
        both = hg > 0 and ag > 0
        negated = bool(re.search(r"\bno\b", p))
        win = (not both) if negated else both
        return "ganada" if win else "perdida"

    # 4) Goals over/under.
    if "gol" in p or "goal" in p or "over" in p or "under" in p:
        line = _first_number(p)
        verdict = _ou_verdict(p, pick, line, total_goals)
        if verdict:
            return verdict

    # 5) Match result (1X2 / winner / draw), only with an explicit result keyword.
    winner = "home" if hg > ag else ("away" if ag > hg else "draw")
    if "empate" in p or re.search(r"\bdraw\b", p):
        return "ganada" if winner == "draw" else "perdida"
    if any(k in p for k in ("gana", "ganador", "victoria", "win", "1x2")):
        picked = None
        if home_name and _norm(home_name) in p:
            picked = "home"
        elif away_name and _norm(away_name) in p:
            picked = "away"
        elif "local" in p:
            picked = "home"
        elif "visitante" in p:
            picked = "away"
        if picked:
            return "ganada" if winner == picked else "perdida"

    return None


# --------------------------------------------------------------------------- #
# Job 1: pre-match analysis (every 30 min)
# --------------------------------------------------------------------------- #

def check_prematch_notifications() -> int:
    """Send the full analysis for fixtures starting within 2h. Returns how many
    fixtures were notified (handy for logging/tests)."""
    users = get_all_users()
    if not users:
        return 0

    now = datetime.now(timezone.utc)
    sent = 0
    for match in get_fixtures_today():
        fixture_id = match.get("id")
        if str(match.get("status") or "").upper() not in PRE_MATCH_STATUSES:
            logging.debug(
                "[pre-partido] fixture %s ignorado por status=%s",
                fixture_id,
                match.get("status"),
            )
            continue
        kickoff = _parse_kickoff(match.get("utcDate"))
        if not kickoff:
            logging.debug("[pre-partido] fixture %s sin kickoff parseable", fixture_id)
            continue
        seconds_to_kickoff = (kickoff - now).total_seconds()
        if not (0 < seconds_to_kickoff <= PRE_MATCH_WINDOW_SECONDS):
            logging.debug(
                "[pre-partido] fixture %s fuera de ventana: %.0fs hasta kickoff",
                fixture_id,
                seconds_to_kickoff,
            )
            continue

        if fixture_id is None or was_fixture_notified(fixture_id):
            logging.debug("[pre-partido] fixture %s ya notificado o inválido", fixture_id)
            continue

        try:
            analysis = analyze_match(match)
        except Exception:
            logging.exception("No se pudo generar el análisis pre-partido (fixture %s)", fixture_id)
            continue  # try again next run

        home = match.get("homeTeam", {}).get("name", "Local")
        away = match.get("awayTeam", {}).get("name", "Visitante")
        mins = int(seconds_to_kickoff // 60)
        header = f"⏰ Empieza en ~{mins} min — {home} vs {away}\n\n"
        for user in users:
            send_message(user["chat_id"], header + analysis)

        mark_fixture_notified(fixture_id)
        sent += 1
        logging.info("[pre-partido] fixture %s notificado a %s usuarios", fixture_id, len(users))

    return sent


# --------------------------------------------------------------------------- #
# Job 2: result notifications (every 15 min)
# --------------------------------------------------------------------------- #

def _team_names(details: dict, bet: dict):
    teams = (details or {}).get("teams", {}) or {}
    home = (teams.get("home", {}) or {}).get("name")
    away = (teams.get("away", {}) or {}).get("name")
    if (not home or not away) and bet.get("match_name") and " vs " in bet["match_name"]:
        a, b = bet["match_name"].split(" vs ", 1)
        home = home or a.strip()
        away = away or b.strip()
    return home, away


def _match_name_score(match_name: str, home: str, away: str) -> int:
    name = _norm(match_name)
    home_n = _norm(home)
    away_n = _norm(away)
    score = 0
    if home_n and home_n in name:
        score += 2
    if away_n and away_n in name:
        score += 2
    if home_n and away_n and f"{home_n} vs {away_n}" in name:
        score += 1
    if home_n and away_n and f"{away_n} vs {home_n}" in name:
        score += 1
    return score


def _candidate_fixture_dates(bet: dict) -> list[str]:
    dates = []
    for raw in (bet.get("created_at"), datetime.utcnow().isoformat()):
        parsed = _parse_kickoff(raw)
        if parsed:
            for day in (parsed.date(), (parsed + timedelta(days=1)).date(), (parsed - timedelta(days=1)).date()):
                value = day.isoformat()
                if value not in dates:
                    dates.append(value)
    return dates


def _find_finished_fixture_for_bet(bet: dict):
    match_name = bet.get("match_name")
    if not match_name:
        return None

    best = None
    best_score = 0
    for date_str in _candidate_fixture_dates(bet):
        try:
            data = _get("/fixtures", params={"date": date_str})
        except Exception:
            continue
        for item in data.get("response", []):
            teams = item.get("teams", {}) or {}
            home = (teams.get("home", {}) or {}).get("name", "")
            away = (teams.get("away", {}) or {}).get("name", "")
            score = _match_name_score(match_name, home, away)
            if score > best_score:
                best_score = score
                best = item

    if best_score >= 3:
        return best
    return None


def _details_for_bet(bet: dict, details_cache: dict):
    fixture_id = bet.get("fixture_id")
    if fixture_id is not None:
        if fixture_id not in details_cache:
            details_cache[fixture_id] = get_fixture_details(fixture_id) or {}
        return details_cache[fixture_id], fixture_id

    details = _find_finished_fixture_for_bet(bet) or {}
    fixture_id = (details.get("fixture") or {}).get("id")
    if fixture_id is not None:
        details_cache[fixture_id] = details
    return details, fixture_id


def check_result_notifications() -> int:
    """Resolve finished bets, notify every pick result, and update balances."""
    pending = get_all_pending_bets()
    if not pending:
        return 0

    details_cache = {}
    resolved_count = 0
    for bet in pending:
        details, fixture_id = _details_for_bet(bet, details_cache)
        if not details or fixture_id is None:
            continue

        short = (details.get("fixture", {}) or {}).get("status", {}).get("short")
        if short not in FINISHED_STATUSES:
            continue

        goals = details.get("goals", {}) or {}
        hg, ag = goals.get("home"), goals.get("away")
        if hg is None or ag is None:
            continue

        home_name, away_name = _team_names(details, bet)
        chat_id = get_user_chat_id(bet["telegram_user_id"])
        score_line = f"⚽ {home_name} {hg} - {ag} {away_name}"
        context = {
            "home_name": home_name,
            "away_name": away_name,
            "home_goals": hg,
            "away_goals": ag,
            "stats": get_match_stats(fixture_id),
            "players": _fetch_fixture_player_stats(fixture_id),
        }
        evaluations = _evaluate_bet_picks(bet, context)
        unknown = [item for item in evaluations if item.get("ok") is None]
        failed = [item for item in evaluations if item.get("ok") is False]

        if unknown:
            if not bet.get("result_notified"):
                mark_result_notified(bet["id"])
                if chat_id is not None:
                    lines = [
                        "🏁 PARTIDO TERMINADO",
                        score_line,
                        "",
                        f"Tu apuesta @ {bet.get('odds')}:",
                    ]
                    for item in evaluations:
                        if item.get("ok") is None:
                            lines.append(f"⏳ {item['pick']} → {item['detail']}")
                        else:
                            lines.append(f"{_pick_label(item['ok'])} {item['pick']} → {_message_detail(item)}")
                    lines.extend([
                        "",
                        "No pude cerrar automáticamente todos los picks. Cerralo con /resultado para actualizar tu balance.",
                    ])
                    send_message(chat_id, "\n".join(lines))
            continue

        result = "perdida" if failed else "ganada"
        profit = _profit_for(result, bet["stake"], bet["odds"])
        resolve_bet(bet["id"], result, profit)
        month = datetime.utcnow().strftime("%Y-%m")
        update_monthly_balance(bet["telegram_user_id"], month, profit)
        resolved_count += 1

        if chat_id is not None:
            lines = [
                "🏁 PARTIDO TERMINADO",
                score_line,
                "",
                f"Tu apuesta @ {bet.get('odds')}:",
            ]
            for item in evaluations:
                lines.append(f"{_pick_label(item['ok'])} {item['pick']} → {_message_detail(item)}")

            if result == "ganada":
                result_line = "💰 RESULTADO: GANADA"
            else:
                suffix = "pick" if len(failed) == 1 else "picks"
                result_line = f"💰 RESULTADO: PERDIDA (falló {len(failed)} {suffix})"
            lines.extend(["", result_line, f"Balance: {_money(profit)}"])
            send_message(chat_id, "\n".join(lines))

    return resolved_count


def retrain_corners_model_job() -> int:
    result = retrain_if_needed()
    if not result.get("retrained"):
        logging.info("[model-corners] reentrenamiento omitido: %s", result.get("reason"))
        return 0

    metrics = result.get("metrics") or {}
    logging.info(
        "[model-corners] reentrenado: hit_rate=%s roi=%s total_picks=%s filas=%s",
        metrics.get("hit_rate"),
        metrics.get("roi"),
        metrics.get("total_picks"),
        result.get("row_count"),
    )
    return int(result.get("new_matches") or 0)


def evaluation_engine_job() -> int:
    result = run_daily_evaluation()
    performance = result.get("performance") or {}
    models = performance.get("models") or []
    logging.info("[evaluation-engine] modelos evaluados=%s", len(models))
    return int(performance.get("total_predictions") or 0)


# --------------------------------------------------------------------------- #
# Threading scheduler (no APScheduler)
# --------------------------------------------------------------------------- #

def _run_loop(job, interval: int, first_delay: int, label: str):
    """Run `job` forever every `interval` seconds, swallowing errors so the
    thread never dies."""
    time.sleep(first_delay)
    while True:
        try:
            count = job()
            if count:
                metric = "items procesados" if label.startswith("collector-") else "notificaciones enviadas"
                logging.info("[%s] %s: %s", label, metric, count)
        except Exception:
            logging.exception("[%s] error en el job de notificaciones", label)
        time.sleep(interval)


def _seconds_until_next_sunday_0300_utc() -> int:
    now = datetime.now(timezone.utc)
    days_until_sunday = (6 - now.weekday()) % 7
    target = now.replace(hour=3, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
    if target <= now:
        target += timedelta(days=7)
    return max(1, int((target - now).total_seconds()))


def start_schedulers():
    """Launch notification and collector loops in daemon threads."""
    threading.Thread(
        target=_run_loop,
        args=(check_prematch_notifications, PRE_MATCH_INTERVAL, 30, "pre-partido"),
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_loop,
        args=(check_result_notifications, RESULTS_INTERVAL, 60, "resultados"),
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_loop,
        args=(collect_upcoming_fixtures, FIXTURES_COLLECTOR_INTERVAL, 10, "collector-fixtures"),
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_loop,
        args=(collect_finished_match_stats, STATS_COLLECTOR_INTERVAL, 90, "collector-stats"),
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_loop,
        args=(collect_odds, ODDS_COLLECTOR_INTERVAL, 120, "collector-odds"),
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_loop,
        args=(collect_injuries, INJURIES_COLLECTOR_INTERVAL, 150, "collector-injuries"),
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_loop,
        args=(evaluation_engine_job, EVALUATION_ENGINE_INTERVAL, 210, "evaluation-engine"),
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_loop,
        args=(
            retrain_corners_model_job,
            MODEL_RETRAIN_INTERVAL,
            _seconds_until_next_sunday_0300_utc(),
            "model-corners",
        ),
        daemon=True,
    ).start()
    logging.info(
        (
            "Schedulers iniciados: pre-partido cada %smin, resultados cada %smin, "
            "fixtures cada %smin, stats cada %smin, odds cada %smin, injuries cada %smin, "
            "evaluation cada %sh, modelo domingos 03:00 UTC."
        ),
        PRE_MATCH_INTERVAL // 60,
        RESULTS_INTERVAL // 60,
        FIXTURES_COLLECTOR_INTERVAL // 60,
        STATS_COLLECTOR_INTERVAL // 60,
        ODDS_COLLECTOR_INTERVAL // 60,
        INJURIES_COLLECTOR_INTERVAL // 60,
        EVALUATION_ENGINE_INTERVAL // 3600,
    )
