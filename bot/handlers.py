import base64
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from services.anthropic_client import analyze_match, analyze_bet_image
from services.database import (
    add_bet,
    get_bet,
    get_monthly_balance,
    get_pending_bets,
    get_stats,
    resolve_bet,
    update_monthly_balance,
    upsert_user,
)
from services.football_data import (
    get_fixtures_by_date,
    get_fixture_details,
    get_match_stats,
    get_player_stats,
)

logger = logging.getLogger(__name__)

LEAGUES = [
    "Torneo Argentino / Copa Argentina",
    "Copa Libertadores",
    "Premier League",
    "La Liga",
    "Mundial 2026",
]

MARKETS = [
    "faltas",
    "remates al arco",
    "atajadas arquero",
    "corners",
    "tiros libres",
    "over/under goles",
    "barridas",
]

PICK_LEAGUES = [
    {"key": "wc", "id": 1, "label": "🏆 Mundial 2026"},
    {"key": "arg", "id": 128, "label": "🇦🇷 Torneo Argentino"},
    {"key": "pl", "id": 39, "label": "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League"},
    {"key": "laliga", "id": 140, "label": "🇪🇸 La Liga"},
    {"key": "lib", "id": 13, "label": "🌍 Copa Libertadores"},
    {"key": "bra", "id": 71, "label": "🇧🇷 Brasilerao"},
]
PICK_LEAGUE_BY_KEY = {league["key"]: league for league in PICK_LEAGUES}
ALL_LEAGUES_KEY = "all"
API_LIMIT_MESSAGE = (
    "⚠️ Servicio temporalmente no disponible. Los requests de la API se renuevan "
    "a las 00:00 UTC. Intentá de nuevo más tarde."
)

USER_STATE: Dict[int, Dict[str, Any]] = {}

# Fixture statuses (API-Football short codes) that mean the match is being played
# right now. Live matches get both the pre-match picks and the live-stats button;
# everything else (TIMED/NS/finished) only offers the pre-match picks button.
LIVE_STATUSES = {"1H", "2H", "HT", "ET", "BT", "P", "LIVE", "INT", "SUSP"}


def _is_live(status: Any) -> bool:
    return str(status or "").upper() in LIVE_STATUSES


def _is_api_limit_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "api-football error" in message and "requests" in message and "limit" in message


def arg_to_date(arg: str) -> Optional[str]:
    normalized = arg.strip().lower()
    today = (datetime.utcnow() - timedelta(hours=3)).date()
    if normalized in {"hoy", "today"}:
        return today.isoformat()
    if normalized in {"mañana", "manana", "tomorrow"}:
        return (today + timedelta(days=1)).isoformat()

    try:
        parsed = datetime.strptime(arg, "%Y-%m-%d").date()
        return parsed.isoformat()
    except ValueError:
        return None


def build_picks_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📅 Hoy", callback_data="picks_today")],
        [InlineKeyboardButton("📅 Mañana", callback_data="picks_tomorrow")],
        [InlineKeyboardButton("📅 Elegir fecha", callback_data="picks_date")],
    ]
    return InlineKeyboardMarkup(buttons)


def build_league_buttons(date_str: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(league["label"], callback_data=f"league_{date_str}_{league['key']}")]
        for league in PICK_LEAGUES
    ]
    buttons.append([InlineKeyboardButton("Ver todos los partidos", callback_data=f"league_{date_str}_{ALL_LEAGUES_KEY}")])
    buttons.append([InlineKeyboardButton("← Volver", callback_data="picks_menu")])
    return InlineKeyboardMarkup(buttons)


def league_label(league_key: str) -> str:
    if league_key == ALL_LEAGUES_KEY:
        return "Todos los partidos"
    return PICK_LEAGUE_BY_KEY.get(league_key, {}).get("label", "Liga")


def format_fixtures_text(fixtures: list, date_str: str) -> str:
    fixtures_text = "\n".join(
        f"{i+1}. {m.get('homeTeam', {}).get('name')} vs {m.get('awayTeam', {}).get('name')} - {m.get('competition', {}).get('name')} ({m.get('status')})"
        for i, m in enumerate(fixtures)
    )
    return f"Partidos para {date_str}:\n{fixtures_text}\n\nToca el partido para generar los picks."


def build_match_buttons(fixtures: list, date_str: str, league_key: str) -> InlineKeyboardMarkup:
    buttons = []
    for match in fixtures:
        match_id = match.get("id")
        home = match.get("homeTeam", {}).get("name", "Local")
        away = match.get("awayTeam", {}).get("name", "Visitante")
        callback_data = f"match_{date_str}_{league_key}_{match_id}"
        buttons.append([InlineKeyboardButton(f"{home} vs {away}", callback_data=callback_data)])
    buttons.append([InlineKeyboardButton("← Volver a ligas", callback_data=f"picks_leagues_{date_str}")])
    return InlineKeyboardMarkup(buttons)


def get_fixtures_for_date(date_str: str, league_key: str = ALL_LEAGUES_KEY) -> list:
    if league_key == ALL_LEAGUES_KEY:
        fixtures = []
        seen_ids = set()
        for league in PICK_LEAGUES:
            for match in get_fixtures_by_date(date_str, league_id=league["id"]):
                match_id = match.get("id")
                if match_id in seen_ids:
                    continue
                seen_ids.add(match_id)
                fixtures.append(match)
        return sorted(fixtures, key=lambda m: m.get("utcDate") or "")

    league = PICK_LEAGUE_BY_KEY.get(league_key)
    if not league:
        return []
    return get_fixtures_by_date(date_str, league_id=league["id"])


def build_match_action_buttons(date_str: str, league_key: str, match_id, is_live: bool) -> InlineKeyboardMarkup:
    """Buttons shown after picking a match: always 'Picks pre-partido'; live
    matches additionally get the '🔴 En vivo' real-time stats button."""
    rows = [[InlineKeyboardButton("📊 Picks pre-partido", callback_data=f"prematch_{date_str}_{match_id}")]]
    if is_live:
        rows.append([InlineKeyboardButton("🔴 En vivo", callback_data=f"live_{date_str}_{match_id}")])
    rows.append([InlineKeyboardButton("← Volver a partidos", callback_data="picks_menu")])
    return InlineKeyboardMarkup(rows)


def build_contextual_match_action_buttons(date_str: str, league_key: str, match_id, is_live: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("ðŸ“Š Picks pre-partido", callback_data=f"prematch_{date_str}_{league_key}_{match_id}")]
    ]
    if is_live:
        rows.append([InlineKeyboardButton("ðŸ”´ En vivo", callback_data=f"live_{date_str}_{league_key}_{match_id}")])
    rows.append([InlineKeyboardButton("← Volver a partidos", callback_data=f"league_{date_str}_{league_key}")])
    return InlineKeyboardMarkup(rows)


def match_options_text(match: dict) -> str:
    home = match.get("homeTeam", {}).get("name", "Local")
    away = match.get("awayTeam", {}).get("name", "Visitante")
    competition = match.get("competition", {}).get("name", "")
    if _is_live(match.get("status")):
        head = f"🔴 {home} vs {away} — EN VIVO"
        body = "El partido está en juego. ¿Qué querés ver?"
    else:
        head = f"⚪ {home} vs {away}"
        body = "Elegí una opción:"
    return f"{head}\n{competition}\n\n{body}"


def _resolve_match(state: Optional[dict], date_str: str, league_key: str, match_id) -> Optional[dict]:
    """Find the selected match from the cached state, falling back to a re-fetch."""
    fixtures = None
    if state and state.get("date") == date_str and state.get("league_key") == league_key:
        fixtures = state.get("fixtures")
    if not fixtures:
        fixtures = get_fixtures_for_date(date_str, league_key)
    return next((m for m in (fixtures or []) if str(m.get("id")) == str(match_id)), None)


def _live_minute(status_short: Optional[str], elapsed, extra=None) -> str:
    status_short = (status_short or "").upper()
    if status_short == "HT":
        return "Entretiempo"
    if elapsed is None:
        return status_short or "EN VIVO"
    return f"{elapsed}+{extra}'" if extra else f"{elapsed}'"


def _sv(team_stats: dict, key: str, default="N/D"):
    """Safe stat value: returns the default when the API gives None/missing."""
    if not team_stats:
        return default
    value = team_stats.get(key)
    return default if value is None else value


def _rating_of(player: dict) -> float:
    try:
        return float(str(player.get("rating") or 0).replace(",", "."))
    except (ValueError, TypeError):
        return 0.0


def format_live_stats(match: dict) -> str:
    """Build the '🔴 En vivo' block: current score + minute, per-team corners,
    fouls, shots and cards, plus the standout players of the match in progress."""
    fixture_id = match.get("id")
    home_name = match.get("homeTeam", {}).get("name", "Local")
    away_name = match.get("awayTeam", {}).get("name", "Visitante")

    # Fresh score + minute from the canonical fixture payload.
    details = get_fixture_details(fixture_id) or {}
    fx = details.get("fixture", {}) or {}
    st = fx.get("status", {}) or {}
    short = st.get("short") or match.get("status")
    minute = _live_minute(short, st.get("elapsed"), st.get("extra"))

    goals = details.get("goals", {}) or {}
    hg, ag = goals.get("home"), goals.get("away")
    if hg is None or ag is None:
        sft = match.get("score", {}).get("fullTime", {})
        hg = sft.get("home") if hg is None else hg
        ag = sft.get("away") if ag is None else ag
    hg = 0 if hg is None else hg
    ag = 0 if ag is None else ag

    # Per-team match statistics, keyed by team name (mapped by order as fallback).
    stats = get_match_stats(fixture_id) or {}
    hs = stats.get(home_name) or {}
    as_ = stats.get(away_name) or {}
    if (not hs or not as_) and len(stats) == 2:
        keys = list(stats.keys())
        hs = hs or stats.get(keys[0], {})
        as_ = as_ or stats.get(keys[1], {})

    lines = [
        f"🔴 EN VIVO — {home_name} vs {away_name}",
        f"⏱️ {minute}  |  {home_name} {hg} - {ag} {away_name}",
        "",
        f"📐 Corners: {home_name} {_sv(hs, 'corners')} - {_sv(as_, 'corners')} {away_name}",
        f"⚠️ Faltas: {home_name} {_sv(hs, 'fouls')} - {_sv(as_, 'fouls')} {away_name}",
        (
            f"🎯 Remates: {home_name} {_sv(hs, 'total_shots')} ({_sv(hs, 'shots_on_goal')} al arco)"
            f" - {_sv(as_, 'total_shots')} ({_sv(as_, 'shots_on_goal')} al arco) {away_name}"
        ),
        (
            f"🟨 Tarjetas: {home_name} {_sv(hs, 'yellow_cards', 0)}🟨 {_sv(hs, 'red_cards', 0)}🟥"
            f" - {_sv(as_, 'yellow_cards', 0)}🟨 {_sv(as_, 'red_cards', 0)}🟥 {away_name}"
        ),
    ]

    # Standout players of the match in progress (real per-fixture data).
    pdata = get_player_stats(
        fixture_id,
        status=short,
        teams=[
            {"id": match.get("homeTeam", {}).get("id"), "name": home_name},
            {"id": match.get("awayTeam", {}).get("id"), "name": away_name},
        ],
    )
    players = []
    if pdata.get("mode") == "in_play":
        for tname, plist in (pdata.get("teams") or {}).items():
            for p in plist or []:
                entry = dict(p)
                entry["team"] = tname
                players.append(entry)
    players.sort(key=_rating_of, reverse=True)

    lines.append("")
    lines.append("⭐ Jugadores destacados:")
    if players:
        for p in players[:5]:
            bits = []
            if p.get("goals"):
                bits.append(f"{p['goals']}⚽")
            if p.get("assists"):
                bits.append(f"{p['assists']} asist")
            if p.get("shots"):
                bits.append(f"{p['shots']} rem ({p.get('shots_on_target', 0)} al arco)")
            if p.get("fouls_committed"):
                bits.append(f"{p['fouls_committed']} faltas")
            if p.get("saves"):
                bits.append(f"{p['saves']} atajadas")
            rating = p.get("rating")
            rtxt = f" (rating {rating})" if rating else ""
            extra_txt = (" — " + ", ".join(bits)) if bits else ""
            lines.append(f"  • {p['name']} ({p['team']}){rtxt}{extra_txt}")
    else:
        lines.append("  Sin datos de jugadores del partido todavía.")

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Remember the chat so the notification jobs can reach this user.
    upsert_user(user.id, update.effective_chat.id, user.username or user.first_name)
    text = (
        f"Hola {user.first_name}! Soy EdgeBet Bot.\n\n"
        "Comandos:\n"
        "• /apuesta - registrar una apuesta (partido, pick, monto, cuota)\n"
        "• /resultado - cerrar una apuesta pendiente (ganada/perdida)\n"
        "• /balance - ver tu rendimiento\n"
        "• /picks - generar picks con análisis de Claude"
    )
    return await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# /apuesta - registro de apuestas (partido -> pick -> monto -> cuota)
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return (datetime.utcnow() - timedelta(hours=3)).date().isoformat()


def _arg_time_label(match: dict) -> str:
    """Kickoff time in Argentina (UTC-3) as 'HH:MM', or '' when unavailable."""
    utc = match.get("utcDate")
    if not utc:
        return ""
    try:
        dt = datetime.fromisoformat(str(utc).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=-3))).strftime("%H:%M")


def _bet_match_label(match: dict) -> str:
    """Button label like 'Argentina vs Jordan - Mundial 2026 (23:00 hs ARG)'."""
    home = match.get("homeTeam", {}).get("name", "Local")
    away = match.get("awayTeam", {}).get("name", "Visitante")
    competition = match.get("competition", {}).get("name", "")
    label = f"{home} vs {away}"
    if competition:
        label += f" - {competition}"
    arg_time = _arg_time_label(match)
    if arg_time:
        label += f" ({arg_time} hs ARG)"
    return label


def build_bet_match_buttons(fixtures: list) -> InlineKeyboardMarkup:
    buttons = []
    for i, m in enumerate(fixtures):
        buttons.append([InlineKeyboardButton(_bet_match_label(m), callback_data=f"bet_match_{i}")])
    return InlineKeyboardMarkup(buttons)


async def apuesta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    today_date = (datetime.utcnow() - timedelta(hours=3)).date()
    today_str = today_date.isoformat()
    tomorrow_str = (today_date + timedelta(days=1)).isoformat()

    # Today + tomorrow: matches kicking off after 23h ARG fall on the next UTC
    # day, so fetching both makes them available to bet on. Dedupe by fixture id.
    fixtures = []
    seen = set()
    for date_str in (today_str, tomorrow_str):
        for m in get_fixtures_by_date(date_str):
            fid = m.get("id")
            if fid in seen:
                continue
            seen.add(fid)
            fixtures.append(m)

    if not fixtures:
        return await update.message.reply_text(
            f"No hay partidos disponibles para hoy ni mañana ({today_str} / {tomorrow_str})."
        )

    USER_STATE[user.id] = {"step": "apuesta_match", "fixtures": fixtures, "date": today_str}
    return await update.message.reply_text(
        f"📝 Nueva apuesta — partidos de hoy y mañana.\n¿A qué partido querés apostar?",
        reply_markup=build_bet_match_buttons(fixtures),
    )


def _profit_for(result: str, stake: float, odds: float) -> float:
    """Net profit of a bet: stake*(odds-1) if won, -stake if lost."""
    if result == "ganada":
        return round(stake * (odds - 1), 2)
    return round(-stake, 2)


# ---------------------------------------------------------------------------
# /resultado - cerrar una apuesta pendiente
# ---------------------------------------------------------------------------

def build_pending_buttons(pending: list) -> InlineKeyboardMarkup:
    buttons = []
    for b in pending:
        label = b.get("match_name") or b.get("league") or "Apuesta"
        pick = (b.get("pick") or "")[:25]
        buttons.append([
            InlineKeyboardButton(
                f"#{b['id']} {label} · {pick} (${b['stake']:.0f} @ {b['odds']})",
                callback_data=f"res_{b['id']}",
            )
        ])
    return InlineKeyboardMarkup(buttons)


async def resultado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pending = get_pending_bets(user.id)
    if not pending:
        return await update.message.reply_text("No tenés apuestas pendientes. 🎉")

    return await update.message.reply_text(
        "Apuestas pendientes. Elegí cuál querés cerrar:",
        reply_markup=build_pending_buttons(pending),
    )


async def handle_bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = update.effective_user
    state = USER_STATE.get(user.id) or {}
    data = query.data

    # 1) Match chosen for a new bet
    if data.startswith("bet_match_"):
        try:
            idx = int(data.rsplit("_", 1)[1])
            fixtures = state.get("fixtures", [])
            match = fixtures[idx]
        except (ValueError, IndexError):
            return await query.edit_message_text(
                "No pude identificar ese partido. Usá /apuesta de nuevo."
            )
        home = match.get("homeTeam", {}).get("name", "Local")
        away = match.get("awayTeam", {}).get("name", "Visitante")
        competition = match.get("competition", {}).get("name", "")
        state.update({
            "step": "apuesta_pick",
            "match_name": f"{home} vs {away}",
            "competition": competition,
            "fixture_id": match.get("id"),
        })
        USER_STATE[user.id] = state
        return await query.edit_message_text(
            f"Partido: {home} vs {away}\n\n¿Qué pick querés registrar? Describilo libremente "
            "(ej: 'Mbappé +2 remates al arco')."
        )

    # 2) A pending bet was selected -> ask outcome
    if data.startswith("res_"):
        try:
            bet_id = int(data.rsplit("_", 1)[1])
        except ValueError:
            return await query.edit_message_text("Apuesta inválida.")
        bet = get_bet(bet_id)
        if not bet or bet.get("result") is not None:
            return await query.edit_message_text("Esa apuesta ya no está pendiente.")
        label = bet.get("match_name") or bet.get("league") or "Apuesta"
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Ganada", callback_data=f"resw_{bet_id}"),
            InlineKeyboardButton("❌ Perdida", callback_data=f"resl_{bet_id}"),
        ]])
        return await query.edit_message_text(
            f"#{bet_id} {label}\nPick: {bet.get('pick')}\nStake: {bet['stake']:.2f} @ {bet['odds']}\n\n"
            "¿Cómo salió?",
            reply_markup=buttons,
        )

    # 3) Outcome chosen -> resolve and report
    if data.startswith("resw_") or data.startswith("resl_"):
        result = "ganada" if data.startswith("resw_") else "perdida"
        try:
            bet_id = int(data.rsplit("_", 1)[1])
        except ValueError:
            return await query.edit_message_text("Apuesta inválida.")
        bet = get_bet(bet_id)
        if not bet or bet.get("result") is not None:
            return await query.edit_message_text("Esa apuesta ya fue cerrada.")

        profit = _profit_for(result, bet["stake"], bet["odds"])
        resolve_bet(bet_id, result, profit)
        month = datetime.utcnow().strftime("%Y-%m")
        update_monthly_balance(user.id, month, profit)

        emoji = "✅" if result == "ganada" else "❌"
        sign = "+" if profit >= 0 else ""
        return await query.edit_message_text(
            f"{emoji} Apuesta #{bet_id} marcada como {result}.\n"
            f"Resultado: {sign}{profit:.2f}\n\n"
            "Usá /balance para ver tu rendimiento actualizado."
        )


async def newbet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    USER_STATE[user.id] = {"step": "league"}
    leagues_text = "\n".join(f"{i+1}. {league}" for i, league in enumerate(LEAGUES))
    await update.message.reply_text(
        "Seleccione la liga para la apuesta:\n" + leagues_text + "\nEnvía el número de la liga."
    )


async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info("/picks recibido user_id=%s chat_id=%s args=%s", user.id if user else None, chat_id, args)

    if args:
        date_arg = " ".join(args)
        date_str = arg_to_date(date_arg)
        logger.debug("/picks fecha parseada user_id=%s date_arg=%s date_str=%s", user.id, date_arg, date_str)
        if not date_str:
            return await update.message.reply_text(
                "Formato inválido. Usa /picks hoy, /picks mañana o /picks YYYY-MM-DD."
            )
        USER_STATE[user.id] = {"step": "picks_league", "date": date_str}
        logger.debug("/picks estado actualizado user_id=%s step=picks_league", user.id)
        await update.message.reply_text(
            f"ElegÃ­ una liga para {date_str}:",
            reply_markup=build_league_buttons(date_str),
        )
        return

    logger.debug("/picks mostrando menu user_id=%s", user.id if user else None)
    await update.message.reply_text(
        "Selecciona una opción para ver partidos:",
        reply_markup=build_picks_keyboard(),
    )


# ---------------------------------------------------------------------------
# Reconocimiento de apuestas por imagen (Claude Vision)
# ---------------------------------------------------------------------------

_YES_ANSWERS = {"si", "sí", "s", "yes", "y", "ok", "dale", "confirmar", "confirmo"}
_NO_ANSWERS = {"no", "n", "cancelar", "cancela", "nop"}


def _to_number(value):
    """Coerce a JSON-extracted amount/odds to float, or None if not parseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        cleaned = str(value).replace("$", "").replace(",", ".").strip()
        return float(re.findall(r"-?\d+(?:\.\d+)?", cleaned)[0])
    except (ValueError, IndexError):
        return None


def _format_money(value) -> str:
    if value is None:
        return "N/D"
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.2f}"


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A photo arrived: read it with Claude Vision and ask the user to confirm
    the detected bet before saving it."""
    user = update.effective_user
    photos = update.message.photo
    if not photos:
        return

    await update.message.reply_text("🔍 Analizando la imagen de tu apuesta...")

    # Largest available size is the last entry. Download it to a temp file and
    # convert to base64 for Claude Vision.
    photo = photos[-1]
    tmp_path = None
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        await tg_file.download_to_drive(custom_path=tmp_path)
        with open(tmp_path, "rb") as fh:
            image_b64 = base64.b64encode(fh.read()).decode("utf-8")
    except Exception:
        return await update.message.reply_text(
            "⚠️ No pude descargar la imagen. Probá enviarla de nuevo."
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    try:
        parsed = analyze_bet_image(image_b64, "image/jpeg")
    except Exception:
        return await update.message.reply_text(
            "⚠️ No pude leer la apuesta de la imagen. Probá con una captura más clara."
        )

    partido = (parsed.get("partido") or "").strip() or "N/D"
    picks = parsed.get("picks") or []
    if isinstance(picks, str):
        picks = [picks]
    picks = [str(p).strip() for p in picks if str(p).strip()]
    monto = _to_number(parsed.get("monto"))
    cuota = _to_number(parsed.get("cuota"))

    USER_STATE[user.id] = {
        "step": "confirm_image_bet",
        "image_bet": {
            "partido": partido,
            "picks": picks,
            "monto": monto,
            "cuota": cuota,
        },
    }

    picks_text = ", ".join(picks) if picks else "no detectados"
    summary = (
        "📋 Apuesta detectada:\n"
        f"⚽ Partido: {partido}\n"
        f"🎯 Picks: {picks_text}\n"
        f"💰 Monto: ${_format_money(monto)}\n"
        f"📊 Cuota: {_format_money(cuota)}\n\n"
        "¿Confirmar registro? (Sí/No)"
    )
    return await update.message.reply_text(summary)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = USER_STATE.get(user.id)
    if not state:
        return await update.message.reply_text(
            "Usa /newbet para iniciar una apuesta o /balance para ver tu saldo mensual."
        )

    text = update.message.text.strip()

    # --- Confirmación de apuesta detectada por imagen ---
    if state["step"] == "confirm_image_bet":
        answer = text.lower().strip()
        bet = state.get("image_bet", {})
        if answer in _YES_ANSWERS:
            picks = bet.get("picks") or []
            pick_text = "; ".join(picks) if picks else "N/D"
            bet_id = add_bet(
                telegram_user_id=user.id,
                username=user.username or user.first_name,
                league="",
                market="imagen",
                pick=pick_text,
                stake=bet.get("monto") or 0,
                odds=bet.get("cuota") or 0,
                match_name=bet.get("partido"),
            )
            USER_STATE.pop(user.id, None)
            return await update.message.reply_text(
                "✅ Apuesta registrada (pendiente)\n"
                f"#{bet_id}\n"
                f"Partido: {bet.get('partido', 'N/D')}\n"
                f"Pick: {pick_text}\n"
                f"Monto: ${_format_money(bet.get('monto'))}\n"
                f"Cuota: {_format_money(bet.get('cuota'))}\n\n"
                "Cerrala con /resultado cuando termine el partido."
            )
        if answer in _NO_ANSWERS:
            USER_STATE.pop(user.id, None)
            return await update.message.reply_text("❌ Registro cancelado.")
        return await update.message.reply_text("Respondé Sí o No para confirmar el registro.")

    # --- /apuesta flow: pick -> stake -> odds ---
    if state["step"] == "apuesta_pick":
        if not text:
            return await update.message.reply_text("El pick no puede estar vacío. Describilo.")
        state.update({"pick": text, "step": "apuesta_stake"})
        return await update.message.reply_text("¿Cuánto apostás? Escribí el monto (ej: 1000).")

    if state["step"] == "apuesta_stake":
        try:
            stake = float(text.replace(",", "."))
            if stake <= 0:
                raise ValueError
        except ValueError:
            return await update.message.reply_text("Monto inválido. Enviá un número mayor a 0.")
        state.update({"stake": stake, "step": "apuesta_odds"})
        return await update.message.reply_text("¿A qué cuota? (ej: 1.85)")

    if state["step"] == "apuesta_odds":
        try:
            odds = float(text.replace(",", "."))
            if odds <= 1:
                raise ValueError
        except ValueError:
            return await update.message.reply_text("Cuota inválida. Enviá un número mayor a 1 (ej: 1.85).")

        bet_id = add_bet(
            telegram_user_id=user.id,
            username=user.username or user.first_name,
            league=state.get("competition", ""),
            market="manual",
            pick=state["pick"],
            stake=state["stake"],
            odds=odds,
            match_name=state.get("match_name"),
            fixture_id=state.get("fixture_id"),
        )
        USER_STATE.pop(user.id, None)
        return await update.message.reply_text(
            "✅ Apuesta registrada (pendiente)\n"
            f"#{bet_id}\n"
            f"Partido: {state.get('match_name', 'N/D')}\n"
            f"Pick: {state['pick']}\n"
            f"Monto: {state['stake']:.2f}\n"
            f"Cuota: {odds}\n\n"
            "Cerrala con /resultado cuando termine el partido."
        )

    if state["step"] == "league":
        try:
            index = int(text) - 1
            league = LEAGUES[index]
        except (ValueError, IndexError):
            return await update.message.reply_text("Número de liga inválido. Intenta de nuevo.")

        state.update({"league": league, "step": "market"})
        markets_text = "\n".join(f"{i+1}. {market}" for i, market in enumerate(MARKETS))
        return await update.message.reply_text(
            "Selecciona el mercado:\n" + markets_text + "\nEnvía el número del mercado."
        )

    if state["step"] == "picks_select":
        fixtures = state.get("fixtures", [])
        try:
            index = int(text) - 1
            match = fixtures[index]
        except (ValueError, IndexError):
            return await update.message.reply_text("Número de partido inválido. Intenta de nuevo.")

        date_str = state.get("date") or (match.get("utcDate") or "")[:10]
        league_key = state.get("league_key", ALL_LEAGUES_KEY)
        state["date"] = date_str  # ensure callbacks can re-resolve the match
        return await update.message.reply_text(
            match_options_text(match),
            reply_markup=build_contextual_match_action_buttons(date_str, league_key, match.get("id"), _is_live(match.get("status"))),
        )

    if state["step"] == "picks_date_input":
        date_str = arg_to_date(text)
        if not date_str:
            return await update.message.reply_text(
                "Fecha inválida. Usa el formato YYYY-MM-DD."
            )

        USER_STATE[user.id] = {"step": "picks_league", "date": date_str}
        return await update.message.reply_text(
            f"ElegÃ­ una liga para {date_str}:",
            reply_markup=build_league_buttons(date_str),
        )

    if state["step"] == "market":
        try:
            index = int(text) - 1
            market = MARKETS[index]
        except (ValueError, IndexError):
            return await update.message.reply_text("Número de mercado inválido. Intenta de nuevo.")

        state.update({"market": market, "step": "pick"})
        return await update.message.reply_text(
            "Describe el pick (por ejemplo: 'Más de 3 corners', 'Menos de 2.5 goles')."
        )

    if state["step"] == "pick":
        state.update({"pick": text, "step": "stake"})
        return await update.message.reply_text(
            "¿Cuánto apostaste? Escribe el monto en pesos o dólares."
        )

    if state["step"] == "stake":
        try:
            stake = float(text)
        except ValueError:
            return await update.message.reply_text("Monto inválido. Envía un número válido.")

        state.update({"stake": stake, "step": "odds"})
        return await update.message.reply_text(
            "¿Cuál fue la cuota? Por ejemplo 1.85"
        )

    if state["step"] == "odds":
        try:
            odds = float(text)
        except ValueError:
            return await update.message.reply_text("Cuota inválida. Envía un número válido.")

        state.update({"odds": odds})
        add_bet(
            telegram_user_id=user.id,
            username=user.username or user.first_name,
            league=state["league"],
            market=state["market"],
            pick=state["pick"],
            stake=state["stake"],
            odds=odds,
        )
        USER_STATE.pop(user.id, None)

        return await update.message.reply_text(
            "Apuesta registrada ✅\n"
            f"Liga: {state['league']}\n"
            f"Mercado: {state['market']}\n"
            f"Pick: {state['pick']}\n"
            f"Stake: {state['stake']}\n"
            f"Cuota: {odds}"
        )

    return await update.message.reply_text("Estado desconocido. Usa /newbet para iniciar nuevamente.")


async def handle_picks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        logger.warning("handle_picks_callback invocado sin callback_query")
        return

    await query.answer()
    user = update.effective_user
    state = USER_STATE.get(user.id)
    data = query.data
    logger.info("picks callback recibido user_id=%s data=%s state_step=%s", user.id if user else None, data, (state or {}).get("step"))

    if data == "picks_today":
        date_str = arg_to_date("hoy")
        fixtures = get_fixtures_for_date(date_str)
        if not fixtures:
            return await query.edit_message_text(f"No hay partidos disponibles para hoy ({date_str}).")
        USER_STATE[user.id] = {"step": "picks_select", "fixtures": fixtures, "date": date_str}
        return await query.edit_message_text(
            format_fixtures_text(fixtures, date_str),
            reply_markup=build_match_buttons(fixtures, date_str),
        )

    if data == "picks_tomorrow":
        date_str = arg_to_date("mañana")
        fixtures = get_fixtures_for_date(date_str)
        if not fixtures:
            return await query.edit_message_text(f"No hay partidos disponibles para mañana ({date_str}).")
        USER_STATE[user.id] = {"step": "picks_select", "fixtures": fixtures, "date": date_str}
        return await query.edit_message_text(
            format_fixtures_text(fixtures, date_str),
            reply_markup=build_match_buttons(fixtures, date_str),
        )

    if data == "picks_date":
        USER_STATE[user.id] = {"step": "picks_date_input"}
        return await query.edit_message_text(
            "Escribe la fecha en formato YYYY-MM-DD para ver los partidos de ese día."
        )

    if data and data.startswith("match_"):
        _, date_str, match_id = data.split("_", 2)
        match = _resolve_match(state, date_str, match_id)
        if not match:
            return await query.edit_message_text(
                "No pude encontrar ese partido. Intenta de nuevo desde el menú."
            )
        # Keep the state alive so the action buttons below can re-resolve the match.
        if state is not None:
            state["date"] = date_str
        return await query.edit_message_text(
            match_options_text(match),
            reply_markup=build_match_action_buttons(date_str, match_id, _is_live(match.get("status"))),
        )

    if data and data.startswith("prematch_"):
        _, date_str, match_id = data.split("_", 2)
        match = _resolve_match(state, date_str, match_id)
        if not match:
            return await query.edit_message_text(
                "No pude encontrar ese partido. Probá de nuevo desde el menú."
            )

        await query.edit_message_text("⚙️ Analizando datos previos al partido...")
        try:
            # Force pre-match treatment so the analysis uses only historical data,
            # even when the match is already live.
            pre_match = dict(match)
            pre_match["status"] = "TIMED"
            picks_text = analyze_match(pre_match)
        except Exception:
            return await query.edit_message_text("⚠️ No se pudo analizar este partido. Intentá de nuevo.")

        return await query.edit_message_text(picks_text)

    if data and data.startswith("live_"):
        _, date_str, match_id = data.split("_", 2)
        match = _resolve_match(state, date_str, match_id)
        if not match:
            return await query.edit_message_text(
                "No pude encontrar ese partido. Probá de nuevo desde el menú."
            )

        await query.edit_message_text("🔴 Trayendo estadísticas en vivo...")
        try:
            live_text = format_live_stats(match)
        except Exception:
            return await query.edit_message_text(
                "⚠️ No se pudieron traer las stats en vivo. Intentá de nuevo."
            )

        return await query.edit_message_text(live_text)

    if data == "picks_menu":
        return await query.edit_message_text(
            "Selecciona una opción para ver partidos:",
            reply_markup=build_picks_keyboard(),
        )


async def handle_picks_callback_v2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        logger.warning("handle_picks_callback_v2 invocado sin callback_query")
        return

    await query.answer()
    user = update.effective_user
    state = USER_STATE.get(user.id)
    data = query.data or ""
    logger.info("picks callback v2 user_id=%s data=%s state_step=%s", user.id if user else None, data, (state or {}).get("step"))

    if data == "picks_menu":
        USER_STATE[user.id] = {"step": "picks_menu"}
        return await query.edit_message_text(
            "Selecciona una opciÃ³n para ver partidos:",
            reply_markup=build_picks_keyboard(),
        )

    if data == "picks_today":
        date_str = arg_to_date("hoy")
        USER_STATE[user.id] = {"step": "picks_league", "date": date_str}
        return await query.edit_message_text(
            f"ElegÃ­ una liga para hoy ({date_str}):",
            reply_markup=build_league_buttons(date_str),
        )

    if data == "picks_tomorrow":
        date_str = arg_to_date("maÃ±ana")
        USER_STATE[user.id] = {"step": "picks_league", "date": date_str}
        return await query.edit_message_text(
            f"ElegÃ­ una liga para maÃ±ana ({date_str}):",
            reply_markup=build_league_buttons(date_str),
        )

    if data == "picks_date":
        USER_STATE[user.id] = {"step": "picks_date_input"}
        return await query.edit_message_text(
            "Escribe la fecha en formato YYYY-MM-DD para ver los partidos de ese dÃ­a."
        )

    if data.startswith("picks_leagues_"):
        date_str = data.replace("picks_leagues_", "", 1)
        USER_STATE[user.id] = {"step": "picks_league", "date": date_str}
        return await query.edit_message_text(
            f"ElegÃ­ una liga para {date_str}:",
            reply_markup=build_league_buttons(date_str),
        )

    if data.startswith("league_"):
        try:
            _, date_str, league_key = data.split("_", 2)
        except ValueError:
            return await query.edit_message_text("No pude leer esa liga. ProbÃ¡ de nuevo desde /picks.")

        try:
            fixtures = get_fixtures_for_date(date_str, league_key)
        except RuntimeError as exc:
            if _is_api_limit_error(exc):
                return await query.edit_message_text(API_LIMIT_MESSAGE)
            raise
        if not fixtures:
            return await query.edit_message_text(
                f"No hay partidos disponibles para {league_label(league_key)} el {date_str}.",
                reply_markup=build_league_buttons(date_str),
            )

        USER_STATE[user.id] = {
            "step": "picks_select",
            "fixtures": fixtures,
            "date": date_str,
            "league_key": league_key,
        }
        return await query.edit_message_text(
            format_fixtures_text(fixtures, date_str),
            reply_markup=build_match_buttons(fixtures, date_str, league_key),
        )

    if data.startswith("match_"):
        try:
            _, date_str, league_key, match_id = data.split("_", 3)
        except ValueError:
            return await query.edit_message_text("No pude leer ese partido. ProbÃ¡ de nuevo desde /picks.")

        try:
            match = _resolve_match(state, date_str, league_key, match_id)
        except RuntimeError as exc:
            if _is_api_limit_error(exc):
                return await query.edit_message_text(API_LIMIT_MESSAGE)
            raise
        if not match:
            return await query.edit_message_text(
                "No pude encontrar ese partido. Intenta de nuevo desde el menÃº."
            )
        if state is not None:
            state["date"] = date_str
            state["league_key"] = league_key
        return await query.edit_message_text(
            match_options_text(match),
            reply_markup=build_contextual_match_action_buttons(date_str, league_key, match_id, _is_live(match.get("status"))),
        )

    if data.startswith("prematch_"):
        try:
            _, date_str, league_key, match_id = data.split("_", 3)
        except ValueError:
            return await query.edit_message_text("No pude leer ese partido. ProbÃ¡ de nuevo desde /picks.")

        try:
            match = _resolve_match(state, date_str, league_key, match_id)
        except RuntimeError as exc:
            if _is_api_limit_error(exc):
                return await query.edit_message_text(API_LIMIT_MESSAGE)
            raise
        if not match:
            return await query.edit_message_text(
                "No pude encontrar ese partido. ProbÃ¡ de nuevo desde el menÃº."
            )

        await query.edit_message_text("âš™ï¸ Analizando datos previos al partido...")
        try:
            pre_match = dict(match)
            pre_match["status"] = "TIMED"
            picks_text = analyze_match(pre_match)
        except Exception:
            return await query.edit_message_text("âš ï¸ No se pudo analizar este partido. IntentÃ¡ de nuevo.")

        return await query.edit_message_text(picks_text)

    if data.startswith("live_"):
        try:
            _, date_str, league_key, match_id = data.split("_", 3)
        except ValueError:
            return await query.edit_message_text("No pude leer ese partido. ProbÃ¡ de nuevo desde /picks.")

        try:
            match = _resolve_match(state, date_str, league_key, match_id)
        except RuntimeError as exc:
            if _is_api_limit_error(exc):
                return await query.edit_message_text(API_LIMIT_MESSAGE)
            raise
        if not match:
            return await query.edit_message_text(
                "No pude encontrar ese partido. ProbÃ¡ de nuevo desde el menÃº."
            )

        await query.edit_message_text("ðŸ”´ Trayendo estadÃ­sticas en vivo...")
        try:
            live_text = format_live_stats(match)
        except Exception:
            return await query.edit_message_text(
                "âš ï¸ No se pudieron traer las stats en vivo. IntentÃ¡ de nuevo."
            )

        return await query.edit_message_text(live_text)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    stats = get_stats(user.id)

    if stats["won"] == 0 and stats["lost"] == 0 and stats["pending"] == 0:
        return await update.message.reply_text(
            "Todavía no registraste apuestas. Usá /apuesta para empezar."
        )

    profit = stats["total_profit"]
    sign = "+" if profit >= 0 else ""

    if stats["streak"] and stats["streak_type"]:
        n = stats["streak"]
        if stats["streak_type"] == "ganada":
            noun = "victoria" if n == 1 else "victorias"
            suffix = "seguida 🔥" if n == 1 else "seguidas 🔥"
        else:
            noun = "derrota" if n == 1 else "derrotas"
            suffix = "seguida 🧊" if n == 1 else "seguidas 🧊"
        racha_txt = f"{n} {noun} {suffix}"
    else:
        racha_txt = "sin apuestas cerradas aún"

    best = stats["best_pick"]
    if best:
        best_label = best.get("match_name") or best.get("league") or "Apuesta"
        best_txt = f"{best.get('pick')} ({best_label}) → +{(best.get('profit') or 0):.2f}"
    else:
        best_txt = "todavía no hay picks ganados"

    text = (
        "💰 BALANCE\n\n"
        f"Resultado neto: {sign}{profit:.2f}\n"
        f"% de acierto: {stats['win_rate']}%\n"
        f"Ganadas: {stats['won']} | Perdidas: {stats['lost']} | Pendientes: {stats['pending']}\n"
        f"Mejor pick: {best_txt}\n"
        f"Racha actual: {racha_txt}"
    )
    await update.message.reply_text(text)
