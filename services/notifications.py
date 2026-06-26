"""Automatic Telegram notifications, driven by a threading-based scheduler.

Two background daemon threads (started from run_bot.py via start_schedulers()):

1. Pre-match (every 30 min): for each of today's fixtures kicking off within the
   next two hours, send the full Claude analysis to every user once (deduped in
   the DB so it never repeats).
2. Results (every 15 min): for each pending bet whose fixture has finished,
   grade it from the real API result, message GANASTE/PERDISTE and update the
   balance. Picks we can't grade with confidence trigger a one-off "close it
   manually" nudge instead of a wrong auto-resolution.

Messages are sent with a plain HTTP call to the Telegram Bot API so the threads
stay completely independent of python-telegram-bot's asyncio event loop.
"""
import logging
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from services.anthropic_client import analyze_match
from services.football_data import (
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
PRE_MATCH_INTERVAL = 30 * 60            # scheduler: pre-match check every 30 min
RESULTS_INTERVAL = 15 * 60             # scheduler: results check every 15 min


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
        if str(match.get("status") or "").upper() not in PRE_MATCH_STATUSES:
            continue
        kickoff = _parse_kickoff(match.get("utcDate"))
        if not kickoff:
            continue
        seconds_to_kickoff = (kickoff - now).total_seconds()
        if not (0 < seconds_to_kickoff <= PRE_MATCH_WINDOW_SECONDS):
            continue

        fixture_id = match.get("id")
        if fixture_id is None or was_fixture_notified(fixture_id):
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


def check_result_notifications() -> int:
    """Resolve finished bets, push GANASTE/PERDISTE messages and update balances.
    Returns the number of bets resolved automatically."""
    pending = get_all_pending_bets()
    if not pending:
        return 0

    details_cache = {}
    resolved_count = 0
    for bet in pending:
        fixture_id = bet.get("fixture_id")
        if fixture_id is None:
            continue
        if fixture_id not in details_cache:
            details_cache[fixture_id] = get_fixture_details(fixture_id) or {}
        details = details_cache[fixture_id]

        short = (details.get("fixture", {}) or {}).get("status", {}).get("short")
        if short not in FINISHED_STATUSES:
            continue

        goals = details.get("goals", {}) or {}
        hg, ag = goals.get("home"), goals.get("away")
        home_name, away_name = _team_names(details, bet)
        match_label = bet.get("match_name") or f"{home_name} vs {away_name}"
        chat_id = get_user_chat_id(bet["telegram_user_id"])
        score_line = f"{home_name} {hg}-{ag} {away_name}"

        result = grade_bet(bet.get("pick", ""), bet.get("market", ""), hg, ag,
                           home_name, away_name, get_match_stats(fixture_id))

        if result in ("ganada", "perdida"):
            profit = _profit_for(result, bet["stake"], bet["odds"])
            resolve_bet(bet["id"], result, profit)
            month = datetime.utcnow().strftime("%Y-%m")
            update_monthly_balance(bet["telegram_user_id"], month, profit)
            resolved_count += 1
            if chat_id is not None:
                headline = (
                    f"✅ GANASTE {_money(profit)}" if result == "ganada"
                    else f"❌ PERDISTE {_money(profit)}"
                )
                msg = (
                    f"{headline}\n"
                    f"{match_label}\n"
                    f"Pick: {bet.get('pick')}\n"
                    f"Resultado final: {score_line}\n"
                    f"Balance del mes actualizado."
                )
                send_message(chat_id, msg)
        else:
            # Couldn't grade automatically: nudge the user once to close it.
            if not bet.get("result_notified"):
                mark_result_notified(bet["id"])
                if chat_id is not None:
                    msg = (
                        f"🏁 Terminó el partido — {score_line}\n"
                        f"Tu pick: {bet.get('pick')}\n"
                        "No pude calcular este pick automáticamente. "
                        "Cerralo con /resultado para actualizar tu balance."
                    )
                    send_message(chat_id, msg)

    return resolved_count


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
                logging.info("[%s] notificaciones enviadas: %s", label, count)
        except Exception:
            logging.exception("[%s] error en el job de notificaciones", label)
        time.sleep(interval)


def start_schedulers():
    """Launch the two notification loops in daemon threads."""
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
    logging.info(
        "Schedulers de notificaciones iniciados (pre-partido cada %smin, resultados cada %smin).",
        PRE_MATCH_INTERVAL // 60, RESULTS_INTERVAL // 60,
    )
