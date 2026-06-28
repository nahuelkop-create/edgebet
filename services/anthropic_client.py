import json
import os
import re
import unicodedata
from dotenv import load_dotenv

import anthropic
from services.football_data import (
    get_team_recent_matches,
    get_group_standings,
    get_player_stats,
    get_team_stats,
    get_tournament_player_leaders,
    get_referee_profile,
    get_head_to_head,
    get_fixture_lineups,
)
from services.odds_service import get_match_odds

load_dotenv()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").replace(" ", "").strip()



def create_client():
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY no está definido")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


BET_IMAGE_PROMPT = (
    "Analizá esta captura de apuesta deportiva y extraé: partido, picks apostados, "
    "monto apostado y cuota total. Respondé SOLO en JSON así: "
    '{"partido": "...", "picks": ["...", "..."], "monto": 1000, "cuota": 2.50}'
)


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of Claude's reply, tolerating ```json fences."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No se encontró JSON en la respuesta: {text[:200]}")
    return json.loads(text[start : end + 1])


def analyze_bet_image(image_base64: str, media_type: str = "image/jpeg") -> dict:
    """Send a betting-slip screenshot to Claude Vision and return the parsed bet.

    Returns a dict with keys: partido (str), picks (list[str]), monto (number),
    cuota (number). Raises on API/parse errors.
    """
    client = create_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_base64,
                        },
                    },
                    {"type": "text", "text": BET_IMAGE_PROMPT},
                ],
            }
        ],
    )

    if not response.content:
        raise RuntimeError("Anthropic returned empty content")

    return _extract_json(response.content[0].text.strip())


def format_recent_matches(matches: list) -> str:
    if not matches:
        return "[]"

    lines = []
    for match in matches:
        home = match.get("homeTeam", {}).get("name", "?")
        away = match.get("awayTeam", {}).get("name", "?")
        utc = match.get("utcDate", "?")
        score = match.get("score", {})
        full = score.get("fullTime", {})
        home_goals = full.get("home")
        away_goals = full.get("away")
        lines.append(f"{utc}: {home} {home_goals}-{away_goals} {away}")
    return "[" + "; ".join(lines) + "]"


def format_referee_context(referee: dict) -> str:
    if not referee or not referee.get("name"):
        return "Árbitro: N/D\n"

    yellow = _num(referee.get("yellow_cards_per_match"), decimals=2)
    fouls = _num(referee.get("fouls_per_match"), decimals=2)
    sample = referee.get("matches_analyzed", 0)
    style = referee.get("style") or "N/D"
    return (
        f"Árbitro: {referee.get('name')} | Perfil: {style} | "
        f"Promedio amarillas: {yellow} | Promedio faltas cobradas: {fouls} "
        f"| Muestra: {sample} partidos\n"
    )


def format_h2h_context(h2h: dict) -> str:
    if not h2h or not h2h.get("matches"):
        return "H2H: sin cruces recientes disponibles.\n"

    lines = [
        (
            "H2H últimos enfrentamientos: "
            f"{_num(h2h.get('avg_goals'), decimals=2)} goles promedio, "
            f"{_num(h2h.get('avg_fouls'), decimals=2)} faltas promedio, "
            f"tendencia: {h2h.get('foul_tendency', 'N/D')}."
        )
    ]
    for match in h2h.get("matches", [])[:5]:
        score = match.get("score", {})
        fouls = _num(match.get("fouls"), decimals=0)
        lines.append(
            f"- {match.get('date', 'N/D')}: {match.get('home', '?')} "
            f"{score.get('home')}-{score.get('away')} {match.get('away', '?')} | "
            f"Ganador: {match.get('winner', 'N/D')} | Faltas: {fouls}"
        )
    return "\n".join(lines) + "\n"


def format_lineups_context(lineups: dict) -> str:
    if not lineups or not lineups.get("confirmed"):
        return "Alineaciones: Alineación no confirmada\n"

    lines = ["Alineaciones oficiales:"]
    for team in lineups.get("teams", []):
        starters = []
        for player in team.get("startXI", [])[:11]:
            name = player.get("name") or "N/D"
            pos = player.get("position") or "N/D"
            number = player.get("number")
            number_text = f"#{number} " if number is not None else ""
            starters.append(f"{number_text}{name} ({pos})")
        formation = team.get("formation") or "N/D"
        lines.append(f"- {team.get('team', 'Equipo')} ({formation}): " + "; ".join(starters))
    return "\n".join(lines) + "\n"


def format_odds_context(odds: dict) -> str:
    if not odds or odds.get("error"):
        error = odds.get("error") if odds else "sin respuesta"
        return f"Cuotas reales: no disponibles ({error}).\n"

    def _fmt(entry):
        if not entry:
            return "N/D"
        bookmaker = entry.get("bookmaker") or odds.get("bookmaker") or "book"
        return f"@{entry.get('price')} ({bookmaker})"

    h2h = odds.get("h2h", {}) or {}
    totals = odds.get("totals", {}) or {}
    btts = odds.get("btts", {}) or {}
    return (
        "Cuotas reales (The Odds API; usar estas cuotas, no inventar cuotas estimadas):\n"
        f"- Resultado 1X2: {odds.get('home_team')} {_fmt(h2h.get('home'))} | "
        f"Empate {_fmt(h2h.get('draw'))} | {odds.get('away_team')} {_fmt(h2h.get('away'))}\n"
        f"- Goles 2.5: Over 2.5 {_fmt(totals.get('over_2_5'))} | "
        f"Under 2.5 {_fmt(totals.get('under_2_5'))}\n"
        f"- BTTS: Sí {_fmt(btts.get('yes'))} | No {_fmt(btts.get('no'))}\n"
        "Probabilidad implícita: 1/cuota. Value Bet si probabilidad estimada > probabilidad implícita.\n"
    )


def _normalize_name(name) -> str:
    """Lowercase + strip accents so 'Ødegaard' and 'Odegaard' compare equal."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(name))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().strip()


def _name_tokens(name) -> list:
    """Significant name tokens, dropping initials like 'E.' in 'E. Haaland'."""
    return [t for t in re.split(r"[\s\.]+", _normalize_name(name)) if len(t) > 1]


def _build_xi_index(lineups: dict) -> set:
    """Set of normalized surname/token strings for every confirmed starter.

    Used to decide whether a ranked player is actually in the starting XI.
    """
    tokens = set()
    for team in (lineups or {}).get("teams", []):
        for player in team.get("startXI", []) or []:
            toks = _name_tokens(player.get("name"))
            if toks:
                tokens.add(toks[-1])  # surname is the reliable match key
    return tokens


def _in_xi(name, xi_tokens: set) -> bool:
    toks = _name_tokens(name)
    if not toks or not xi_tokens:
        return False
    # Match on surname first, then any token (covers reversed/compound names).
    return toks[-1] in xi_tokens or any(t in xi_tokens for t in toks)


def _xi_tag(name, xi_tokens: set, confirmed: bool) -> str:
    """Inline marker telling the model if a player starts; empty when unconfirmed."""
    if not confirmed:
        return ""
    return " — ✅ TITULAR" if _in_xi(name, xi_tokens) else " — ❌ NO ESTÁ EN EL 11 (suplente)"


def format_lineup_status_block(lineups: dict) -> str:
    """Human/LLM-readable block stating whether the XI is confirmed, and listing
    the official starters when it is. Drives the player-pick gating in the prompt.
    """
    confirmed = bool(lineups and lineups.get("confirmed"))
    if not confirmed:
        return (
            "ESTADO DE ALINEACIÓN: ⚠️ NO CONFIRMADA\n"
            "No hay alineaciones oficiales publicadas para este partido. "
            "NO se permiten picks de jugadores individuales."
        )

    lines = ["ESTADO DE ALINEACIÓN: ✅ CONFIRMADA", "ONCE TITULAR OFICIAL:"]
    for team in lineups.get("teams", []):
        starters = []
        for player in team.get("startXI", [])[:11]:
            name = player.get("name") or "N/D"
            pos = player.get("position") or "N/D"
            number = player.get("number")
            number_text = f"#{number} " if number is not None else ""
            starters.append(f"{number_text}{name} ({pos})")
        formation = team.get("formation") or "N/D"
        lines.append(f"  - {team.get('team', 'Equipo')} ({formation}): " + "; ".join(starters))
    return "\n".join(lines)


def _get_match_status(row: dict, table: list, total_matches: int = 3) -> str:
    points = row.get("points", 0)
    played = row.get("playedGames", row.get("played", 0))
    remaining = max(0, total_matches - played)
    max_points = points + remaining * 3

    other_points = [r.get("points", 0) for r in table if r is not row]
    sorted_other = sorted(other_points, reverse=True)
    qualification_threshold = sorted_other[1] if len(sorted_other) > 1 else 0

    if max_points < qualification_threshold:
        return "eliminado"
    if points > qualification_threshold + remaining * 3:
        return "clasificado"
    return "puede clasificar"


def format_group_standings_context(group_standings: dict, home_id: int, away_id: int) -> str:
    table = group_standings.get("table", []) if group_standings else []
    if not table:
        return "No hay datos de standings de grupo disponibles.\n"

    total_matches = 3 if len(table) == 4 else 3
    lines = ["Situación en el grupo:"]
    for row in table:
        team = row.get("team", {})
        team_id = team.get("id")
        name = team.get("name", "Equipo")
        position = row.get("position", "?")
        points = row.get("points", 0)
        played = row.get("playedGames", row.get("played", 0))
        status = _get_match_status(row, table, total_matches)
        if status == "clasificado":
            need = "le alcanza empatar o incluso perder en el peor de los casos"
        elif status == "eliminado":
            need = "no puede clasificar, juega por orgullo"
        elif position == 1:
            need = "puede permitirse un empate"
        elif position == 2:
            need = "necesita empatar o ganar para no depender de terceros"
        else:
            need = "necesita ganar sí o sí"

        lines.append(
            f"  {position}. {name} - {points} pts, {played} jugados, {status}, {need}."
        )

    lines.append("Qué se juegan:")
    for row in table:
        team = row.get("team", {})
        name = team.get("name", "Equipo")
        status = _get_match_status(row, table, total_matches)
        if status == "clasificado":
            lines.append(f"  - {name}: clasificación ya asegurada, busca liderar el grupo.")
        elif status == "eliminado":
            lines.append(f"  - {name}: está eliminado, juega sin presión de clasificación.")
        else:
            lines.append(f"  - {name}: pelea por la clasificación y necesita un resultado positivo.")

    lines.append("Historial entre estos equipos en Mundiales: no disponible en los datos actuales.")
    return "\n".join(lines) + "\n\n"


def format_match_prompt(match: dict) -> str:
    home_team = match.get("homeTeam", {})
    away_team = match.get("awayTeam", {})
    home_name = home_team.get("name", "Equipo Local")
    away_name = away_team.get("name", "Equipo Visitante")
    home_id = home_team.get("id")
    away_id = away_team.get("id")
    competition = match.get("competition", {}).get("name", "Competición")
    utc_date = match.get("utcDate", "fecha desconocida")
    status = match.get("status", "desconocido")
    stage = match.get("stage", "N/A")
    group = match.get("group", "N/A")
    matchday = match.get("matchday", "N/A")
    fixture_id = match.get("id")

    match_date = match.get("utcDate", "")
    if match_date and "T" in match_date:
        date_to = match_date.split("T")[0]
    else:
        date_to = None

    home_history = get_team_recent_matches(home_id, limit=5, date_to=date_to)
    away_history = get_team_recent_matches(away_id, limit=5, date_to=date_to)
    group_standings = get_group_standings(str(match.get("competition", {}).get("id", "")), group)
    
    # Get real team statistics
    home_stats = get_team_stats(home_id)
    away_stats = get_team_stats(away_id)
    referee_profile = get_referee_profile(fixture_id) if fixture_id else {}
    h2h = get_head_to_head(home_id, away_id) if home_id and away_id else {}
    lineups = get_fixture_lineups(fixture_id) if fixture_id else {}
    odds = get_match_odds(home_name, away_name)
    
    # Player statistics: pre-match uses tournament leaders per team, live/finished
    # uses the real per-fixture player data. get_player_stats branches on status.
    player_stats = get_player_stats(
        fixture_id,
        status=status,
        teams=[{"id": home_id, "name": home_name}, {"id": away_id, "name": away_name}],
    )

    # Tournament-wide leaderboards are only needed to enrich the in-play rankings;
    # the pre-match path already embeds them in player_stats.
    tournament_leaders = (
        get_tournament_player_leaders() if player_stats.get("mode") == "in_play" else {}
    )

    home_history_text = format_recent_matches(home_history)
    away_history_text = format_recent_matches(away_history)
    group_context = format_group_standings_context(group_standings, home_id, away_id)

    # Format team stats
    home_stats_text = _format_team_stats(home_stats)
    away_stats_text = _format_team_stats(away_stats)
    referee_text = format_referee_context(referee_profile)
    h2h_text = format_h2h_context(h2h)
    lineups_text = format_lineups_context(lineups)
    odds_text = format_odds_context(odds)

    # Build real per-category player rankings (match + tournament data)
    player_stats_text = _format_player_rankings(
        player_stats, tournament_leaders, home_name, away_name, home_id, away_id, lineups
    )
    lineup_confirmed = bool(lineups.get("confirmed"))

    return (
        "Eres un analista de apuestas deportivas. Tu salida debe ser CORTA, ESPECÍFICA y ACCIONABLE. "
        "NADA de párrafos largos: solo datos y picks. Máximo 25 líneas en total. "
        "Cada razón es UNA sola línea breve. Usá exclusivamente los datos reales de abajo (no inventes jugadores ni cifras).\n\n"
        "Respondé EXACTAMENTE en este formato (sin texto extra antes ni después):\n"
        "⚽ [Local] vs [Visitante] - [Competición]\n\n"
        "📊 DATA CLAVE:\n"
        "- Alineación: ✅ confirmada / ⚠️ no confirmada (copiá el estado real de ESTADO DE ALINEACIÓN)\n"
        "- [Local]: X goles/partido, X% posesión, X corners/partido\n"
        "- [Visitante]: X goles/partido, X% posesión, X corners/partido\n"
        "- Top rematador: [Jugador] - X remates (X al arco)\n"
        "- Más faltas cometidas: [Jugador] - X faltas\n"
        "- Más faltas recibidas: [Jugador] - X faltas\n"
        "- Arquero destacado: [Jugador] - X atajadas\n\n"
        "🛡️ PICK SEGURO:\n"
        "- X% - [pick con mayor confianza >80%] @ cuota real → [razón en 1 línea]\n\n"
        "💎 VALUE BET:\n"
        "- X% - [pick con mayor edge] @ cuota real → implícita X%, edge +X pp → 💎 VALUE BET\n\n"
        "🚀 SOÑADA:\n"
        "- [Pick 1] + [Pick 2] + [Pick 3] @ cuota combinada X.XX → [razón en 1 línea]\n"
        "(Si no hay 3 picks seguros con cuota real disponible, escribí una sola línea: - No hay combinada sin inventar cuotas.)\n\n"
        "👤 PICKS DE JUGADORES:\n"
        "(Si la alineación está confirmada, primero escribí: ✅ Alineación confirmada\n"
        " y luego, por cada jugador clave que NO esté en el 11: ⚠️ [Jugador] no juega - equipo suplente)\n"
        "- X% - [Top rematador titular] +X remates → dato real: X remates\n"
        "- X% - [Jugador titular con más faltas] +X faltas cometidas → dato real: X faltas\n"
        "- X% - [Arquero titular] +X atajadas → dato real: X atajadas\n"
        "  (Si el arquero titular NO tiene dato real concreto de atajadas, NO muestres esta línea: reemplazala por el siguiente titular con el mejor dato real concreto en otra categoría disponible -faltas recibidas, remates, etc.-. NUNCA muestres una línea de pick con N/D o sin número real.)\n"
        "(Si la alineación NO está confirmada, NO escribas ninguna línea de pick: poné solo\n"
        " ⚠️ Alineación no confirmada - picks de jugadores no disponibles)\n\n"
        "💰 Confianza global: X%\n\n"
        "REGLAS:\n"
        "- DATA CLAVE: completá cada línea con los números reales provistos abajo. Si un dato no está, poné 'N/D'.\n"
        "- Si el partido NO empezó (pre-partido), no hay datos del propio partido: en DATA CLAVE usá los LÍDERES POR EQUIPO provistos abajo (torneo o temporada de club según indique el dato). Para los PICKS DE JUGADORES seguí SIEMPRE la regla de ALINEACIÓN: solo si está confirmada y solo con líderes marcados ✅ TITULAR.\n"
        "- CUOTAS: usá SOLO las cuotas reales del bloque CUOTAS REALES. Si una cuota real no está disponible para un mercado, no uses ese mercado para PICK SEGURO, VALUE BET ni SOÑADA.\n"
        "- VALUE BET: calculá probabilidad implícita = 1/cuota. Si tu probabilidad estimada supera la implícita, marcá 💎 VALUE BET. Ejemplo: 65% estimado vs cuota 2.00 (50% implícita) = edge +15 pp.\n"
        "- 🛡️ PICK SEGURO: elegí el pick con mayor confianza siempre que sea >80%; si ninguno supera 80%, escribí 'Sin pick seguro >80%'.\n"
        "- 💎 VALUE BET: mostrá UNA sola línea, únicamente el pick con mayor diferencia positiva entre tu probabilidad estimada y la probabilidad implícita de la cuota. Mostrá implícita y edge en puntos porcentuales. No muestres picks descartados ni edges negativos.\n"
        "- 🚀 SOÑADA: armá una combinada de 3 picks seguros usando cuotas reales disponibles. La cuota combinada es el producto de las 3 cuotas, redondeada a 2 decimales. Si faltan 3 cuotas reales o la tercera pierna sería inventada/contradictoria, NO armes combinada y escribí exactamente una línea: '- No hay combinada sin inventar cuotas'.\n"
        "- En SOÑADA solo podés usar selecciones que aparezcan literalmente en CUOTAS REALES: local gana, empate, visitante gana, Over 2.5, Under 2.5 o BTTS si está disponible. Prohibido inventar 'empate no', doble oportunidad, handicaps, primer tiempo, corners u otros mercados.\n"
        "- Usa el perfil del arbitro: si es estricto, prioriza picks de tarjetas y faltas; si es permisivo, baja exposicion a tarjetas.\n"
        "- Usa el H2H: si muestra muchos goles, refuerza overs de goles; si muestra muchas faltas, refuerza faltas/tarjetas.\n"
        "- ALINEACIÓN (regla CRÍTICA): mirá ESTADO DE ALINEACIÓN en RANKINGS REALES DE JUGADORES y reflejá su estado en la línea 'Alineación' de DATA CLAVE.\n"
        "- Si ESTADO DE ALINEACIÓN es ⚠️ NO CONFIRMADA: en 👤 PICKS DE JUGADORES escribí EXACTAMENTE y SOLO esta línea: ⚠️ Alineación no confirmada - picks de jugadores no disponibles. NO generes ningún pick de jugador individual. Igual generá PICK SEGURO, VALUE BET y SOÑADA normalmente.\n"
        "- Si ESTADO DE ALINEACIÓN es ✅ CONFIRMADA: arrancá 👤 PICKS DE JUGADORES con la línea ✅ Alineación confirmada. Hacé picks SOLO de jugadores marcados ✅ TITULAR. PROHIBIDO piquear a cualquiera marcado ❌ NO ESTÁ EN EL 11 / suplente o que no figure en el ONCE TITULAR OFICIAL.\n"
        "- Si un jugador clave (goleador, figura) aparece como ❌ NO ESTÁ EN EL 11 o en JUGADORES CLAVE QUE NO ESTÁN EN EL 11, agregá debajo de ✅ Alineación confirmada la línea: ⚠️ [Jugador] no juega - equipo suplente.\n"
        "- Los PICKS DE JUGADORES deben ser sobre jugadores CONCRETOS de los rankings reales y TITULARES, con líneas (+X) realistas según su número real.\n"
        "- Picks de partido: evaluá SOLO mercados con cuota real disponible: resultado 1X2, Over/Under 2.5 goles y BTTS. No uses corners para estas 3 secciones salvo que exista cuota real en CUOTAS REALES.\n"
        "- PICKS DE JUGADORES (SOLO si la alineación está ✅ CONFIRMADA): mostrá hasta 3 picks, sin filtro de confianza, eligiendo SOLO entre jugadores ✅ TITULAR y SOLO cuando exista un dato real concreto (numérico) para ese jugador: el titular con más remates reales con pick de remates, el titular con más faltas cometidas reales con pick de faltas cometidas, y el arquero titular con más atajadas reales con pick de atajadas. Si el arquero titular NO tiene dato real de atajadas (N/D o sin datos), NO muestres pick de arquero: reemplazalo por el siguiente titular con el mejor dato real concreto en otra categoría todavía no usada (faltas recibidas, remates, etc.).\n"
        "- Para PICKS DE JUGADORES compará numéricamente los datos reales de los TITULARES de ambos equipos. Nunca elijas un jugador con menos remates/faltas/atajadas que otro TITULAR disponible en la misma categoría. Si hay empate, elegí cualquiera de los empatados con el valor máximo. Ignorá por completo a los no titulares aunque tengan mejores números.\n"
        "- Los picks de jugadores deben usar jugadores CONCRETOS y TITULARES de los rankings reales e incluir su dato real concreto provisto. PROHIBIDO mostrar 'N/D', 'sin dato' o un pick de jugador sin número real: si el dato de una categoría no está disponible, NO muestres ese pick y reemplazalo por el siguiente titular con el mejor dato real concreto en otra categoría disponible. Es preferible mostrar menos de 3 picks de jugadores antes que mostrar uno con N/D.\n"
        "- NO muestres el proceso interno, candidatos descartados, tablas de evaluación ni explicación del filtro.\n"
        "- Confianza global: promedio redondeado de los picks de partido visibles y los picks de jugadores mostrados. Si no hay picks de jugadores (alineación no confirmada), promediá solo los picks de partido visibles. Si no hay ninguno de los dos, poné N/D.\n"
        "- Cuota estimada: un número plausible (ej. @1.85, @2.10). La razón: máximo una línea corta.\n"
        "- NO escribas justificaciones largas, introducciones ni conclusiones. Solo el bloque pedido.\n\n"
        "=== DATOS REALES ===\n"
        f"Partido: {home_name} (id {home_id}) vs {away_name} (id {away_id})\n"
        f"Competición: {competition} | Fase: {stage} - {group} - Jornada {matchday} | Estado: {status}\n\n"
        f"Estadísticas {home_name}:\n{home_stats_text}\n"
        f"Últimos 5: {home_history_text}\n\n"
        f"Estadísticas {away_name}:\n{away_stats_text}\n"
        f"Últimos 5: {away_history_text}\n\n"
        f"ARBITRO:\n{referee_text}\n"
        f"H2H ENTRE EQUIPOS:\n{h2h_text}\n"
        f"CUOTAS REALES:\n{odds_text}\n"
        f"ALINEACIONES:\n{lineups_text}\n"
        f"Contexto de grupo:\n{group_context}"
        f"RANKINGS REALES DE JUGADORES:\n{player_stats_text}\n"
    )


def _num(value, suffix: str = "", decimals: int = 2) -> str:
    """Format a numeric stat, or 'N/D' (no disponible) when it is missing."""
    if value is None:
        return "N/D"
    if isinstance(value, (int, float)):
        text = f"{value:.{decimals}f}" if isinstance(value, float) else str(value)
        return f"{text}{suffix}"
    return f"{value}{suffix}"


def _format_team_stats(stats: dict) -> str:
    if not stats:
        return "Sin datos disponibles."

    sample = stats.get("per_match_sample", 0)
    sample_note = (
        f" (promedio de {sample} partidos recientes)" if sample else " (sin muestra reciente)"
    )
    return (
        f"- Forma reciente: {stats.get('form') or 'N/D'}\n"
        f"- Partidos jugados (torneo): {stats.get('matches_played', 'N/D')}\n"
        f"- Goles a favor: {stats.get('goals_for', 'N/D')} ({_num(stats.get('goals_per_match'))} por partido)\n"
        f"- Goles en contra: {stats.get('goals_against', 'N/D')}\n"
        f"- Vallas invictas: {stats.get('clean_sheets', 'N/D')}\n"
        f"- Posesión promedio: {_num(stats.get('possession_avg'), '%', 1)}{sample_note}\n"
        f"- Corners por partido: {_num(stats.get('corners_per_match'))}{sample_note}\n"
        f"- Remates por partido: {_num(stats.get('shots_per_match'))}{sample_note}\n"
        f"- Faltas por partido: {_num(stats.get('fouls_per_match'))}{sample_note}\n"
    )


def _flatten_players(teams: dict) -> list:
    """Merge every team's player list into one list with the team name attached."""
    flat = []
    for team, players in (teams or {}).items():
        for player in players or []:
            entry = dict(player)
            entry["team"] = team
            flat.append(entry)
    return flat


def _format_prematch_profiles(profiles: dict, xi_tokens: set, confirmed: bool) -> str:
    """Pre-match player block: leaders for each team (top shooter, top scorer,
    top assistant, most fouls committed, most fouls drawn, most yellow cards).
    Each leader is tagged with whether they start, and absent key players are
    flagged when the XI is confirmed so the model never picks a benched star."""
    note = (
        " (usar SOLO los marcados ✅ TITULAR)"
        if confirmed
        else " (alineación NO confirmada: no usar para picks de jugadores)"
    )
    lines = [f"📋 LÍDERES POR EQUIPO{note}:"]
    if not profiles:
        lines.append("  Sin datos de líderes disponibles.")
        return "\n".join(lines)

    def _fmt(entry, unit):
        if not entry or not entry.get("name"):
            return "N/D"
        tag = _xi_tag(entry.get("name"), xi_tokens, confirmed)
        source = entry.get("source") or "torneo"
        return f"{entry['name']} ({entry.get('value', 0)} {unit}) ({source}){tag}"

    def _fmt_shooter(entry):
        if not entry or not entry.get("name"):
            return "N/D"
        tag = _xi_tag(entry.get("name"), xi_tokens, confirmed)
        source = entry.get("source") or "torneo"
        on_target = entry.get("shots_on_target", 0)
        return (
            f"{entry['name']} ({entry.get('value', 0)} remates, "
            f"{on_target} al arco) ({source}){tag}"
        )

    absent_keys = []  # key attacking players (scorer/assist) on the bench
    for team, prof in profiles.items():
        lines.append(f"\n🎯 {team}:")
        lines.append(f"  - Top rematador: {_fmt_shooter(prof.get('top_shooter'))}")
        lines.append(f"  - Top goleador: {_fmt(prof.get('top_scorer'), 'goles')}")
        lines.append(f"  - Top asistidor: {_fmt(prof.get('top_assist'), 'asist.')}")
        lines.append(f"  - Más faltas cometidas: {_fmt(prof.get('top_fouls'), 'faltas')}")
        lines.append(f"  - Más faltas recibidas: {_fmt(prof.get('top_fouls_drawn'), 'faltas')}")
        lines.append(f"  - Más tarjetas: {_fmt(prof.get('top_cards'), 'amarillas')}")
        if prof.get("top_keeper"):
            lines.append(f"  - Arquero (más atajadas): {_fmt(prof.get('top_keeper'), 'atajadas')}")

        if confirmed:
            for role in ("top_scorer", "top_assist"):
                entry = prof.get(role)
                if entry and entry.get("name") and not _in_xi(entry.get("name"), xi_tokens):
                    label = "goleador" if role == "top_scorer" else "asistidor"
                    absent_keys.append(f"{entry['name']} ({team}, {label})")

    if confirmed and absent_keys:
        lines.append(
            "\n⚠️ JUGADORES CLAVE QUE NO ESTÁN EN EL 11 (equipo suplente): "
            + "; ".join(absent_keys)
        )

    return "\n".join(lines)


def _top_by(players: list, key, minimum: int = 1, tiebreak=None, n: int = 3) -> list:
    """Return the top `n` players ranked by `key`, keeping only those with value
    >= `minimum`. `tiebreak` is an optional secondary sort key."""
    scored = [p for p in players if (key(p) or 0) >= minimum]
    if tiebreak is not None:
        scored.sort(key=lambda p: (key(p) or 0, tiebreak(p) or 0), reverse=True)
    else:
        scored.sort(key=lambda p: key(p) or 0, reverse=True)
    return scored[:n]


def _format_player_rankings(
    player_stats: dict,
    tournament: dict,
    home_name: str,
    away_name: str,
    home_id,
    away_id,
    lineups: dict = None,
) -> str:
    """Build real per-category player rankings. Pre-match uses tournament leaders
    per team; live/finished combines per-fixture data with tournament leaders.

    The official starting XI (when confirmed) gates player picks: each ranked
    player is tagged ✅ TITULAR / ❌ suplente so the model only picks real starters.
    """
    player_stats = player_stats or {}
    lineups = lineups or {}
    confirmed = bool(lineups.get("confirmed"))
    xi_tokens = _build_xi_index(lineups)

    status_block = format_lineup_status_block(lineups)

    # Pre-match: no per-fixture data, render tournament leaders per team.
    if player_stats.get("mode") == "pre_match":
        profiles_block = _format_prematch_profiles(
            player_stats.get("profiles", {}), xi_tokens, confirmed
        )
        return status_block + "\n\n" + profiles_block

    lines = [status_block, ""]
    players = _flatten_players(player_stats.get("teams", {}))

    # --- Match-level rankings (from this fixture's player data) ---
    if players:
        shooters = _top_by(
            players,
            key=lambda p: p.get("shots", 0),
            tiebreak=lambda p: p.get("shots_on_target", 0),
        )
        lines.append("🎯 TOP 3 REMATADORES DEL PARTIDO (remates totales / al arco):")
        if shooters:
            for i, p in enumerate(shooters, 1):
                lines.append(
                    f"  {i}. {p['name']} ({p['team']}) - {p.get('shots', 0)} remates"
                    f" ({p.get('shots_on_target', 0)} al arco)"
                    f"{_xi_tag(p['name'], xi_tokens, confirmed)}"
                )
        else:
            lines.append("  Sin datos de remates en este partido.")

        fouls_committed = _top_by(players, key=lambda p: p.get("fouls_committed", 0))
        lines.append("\n🟨 TOP 3 EN FALTAS COMETIDAS (partido):")
        if fouls_committed:
            for i, p in enumerate(fouls_committed, 1):
                lines.append(
                    f"  {i}. {p['name']} ({p['team']}) - {p.get('fouls_committed', 0)} faltas cometidas"
                    f"{_xi_tag(p['name'], xi_tokens, confirmed)}"
                )
        else:
            lines.append("  Sin datos de faltas cometidas en este partido.")

        fouls_drawn = _top_by(players, key=lambda p: p.get("fouls_drawn", 0))
        lines.append("\n🆘 TOP 3 EN FALTAS RECIBIDAS (partido):")
        if fouls_drawn:
            for i, p in enumerate(fouls_drawn, 1):
                lines.append(
                    f"  {i}. {p['name']} ({p['team']}) - {p.get('fouls_drawn', 0)} faltas recibidas"
                    f"{_xi_tag(p['name'], xi_tokens, confirmed)}"
                )
        else:
            lines.append("  Sin datos de faltas recibidas en este partido.")

        keepers = _top_by(players, key=lambda p: p.get("saves", 0), n=2)
        lines.append("\n🧤 TOP ARQUERO (atajadas del partido):")
        if keepers:
            for p in keepers:
                lines.append(
                    f"  • {p['name']} ({p['team']}) - {p.get('saves', 0)} atajadas"
                    f"{_xi_tag(p['name'], xi_tokens, confirmed)}"
                )
        else:
            lines.append("  Sin datos de atajadas en este partido.")
    else:
        lines.append(
            "🎯 Sin estadísticas de jugadores del partido (fixture aún sin datos)."
        )

    # --- Tournament-level rankings (cumulative leaderboards) ---
    tournament = tournament or {}
    match_team_ids = {home_id, away_id}

    def _for_match_teams(leaders):
        return [l for l in leaders if l.get("team_id") in match_team_ids]

    yellows = _for_match_teams(tournament.get("yellow_cards", []))
    yellows.sort(key=lambda l: l.get("value", 0), reverse=True)
    lines.append("\n🟨 TOP 3 TARJETAS AMARILLAS ACUMULADAS EN EL TORNEO (estos equipos):")
    if yellows:
        for i, l in enumerate(yellows[:3], 1):
            lines.append(f"  {i}. {l['name']} ({l['team']}) - {l.get('value', 0)} amarillas")
    else:
        lines.append("  Sin datos de tarjetas acumuladas para estos equipos.")

    def _team_leader(leaders, team_id):
        candidates = [l for l in leaders if l.get("team_id") == team_id]
        candidates.sort(key=lambda l: l.get("value", 0), reverse=True)
        return candidates[0] if candidates else None

    scorers = tournament.get("scorers", [])
    assists = tournament.get("assists", [])
    lines.append("\n⚽ GOLEADOR Y ASISTIDOR DEL TORNEO POR EQUIPO:")
    for team_name, team_id in ((home_name, home_id), (away_name, away_id)):
        scorer = _team_leader(scorers, team_id)
        assistant = _team_leader(assists, team_id)
        scorer_txt = (
            f"{scorer['name']} ({scorer.get('value', 0)} goles)" if scorer else "sin dato en el top del torneo"
        )
        assist_txt = (
            f"{assistant['name']} ({assistant.get('value', 0)} asist.)" if assistant else "sin dato en el top del torneo"
        )
        lines.append(f"  • {team_name}: goleador → {scorer_txt}; asistidor → {assist_txt}")

    return "\n".join(lines)



def generate_picks(match: dict) -> str:
    client = create_client()
    prompt = format_match_prompt(match)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[
            {"role": "user", "content": prompt}
        ],
    )

    if not response.content:
        raise RuntimeError("Anthropic returned empty content")

    return response.content[0].text.strip()


def analyze_match(match: dict) -> str:
    return generate_picks(match)
