import json
import unicodedata
from datetime import datetime

from flask import Flask, jsonify, render_template

from services.database import get_connection, initialize_db
from services.evaluation_engine import (
    get_model_performance,
    get_model_performance_history,
    get_postgres_stats,
    get_recent_predictions,
    get_value_bet_performance,
    get_value_bets,
)

app = Flask(__name__)


def _money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(value: str):
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower()


def _result_emoji(result: str) -> str:
    if result == "ganada":
        return "✅"
    if result == "perdida":
        return "❌"
    return "⏳"


def _classify_bet(bet: dict) -> str:
    text = _normalize(f"{bet.get('market', '')} {bet.get('pick', '')}")
    if any(word in text for word in ("corner", "corners")):
        return "corners"
    if any(word in text for word in ("jugador", "remate", "faltas", "atajadas", "tarjeta")):
        return "jugadores"
    if any(word in text for word in ("resultado", "ganador", "empate", "winner", "1x2")):
        return "resultado"
    if any(word in text for word in ("gol", "goles", "over", "under", "btts", "ambos")):
        return "goles"
    return "resultado"


def _current_streak(resolved: list[dict]) -> str:
    ordered = sorted(
        resolved,
        key=lambda b: (_parse_dt(b.get("resolved_at") or b.get("created_at")), b.get("id") or 0),
        reverse=True,
    )
    if not ordered:
        return "Sin apuestas cerradas"

    streak_type = ordered[0].get("result")
    count = 0
    for bet in ordered:
        if bet.get("result") != streak_type:
            break
        count += 1

    label = "ganadas" if streak_type == "ganada" else "perdidas"
    return f"{count} {label}"


def _summary(bets: list[dict]) -> dict:
    resolved = [b for b in bets if b.get("result") in ("ganada", "perdida")]
    won = [b for b in resolved if b.get("result") == "ganada"]
    lost = [b for b in resolved if b.get("result") == "perdida"]
    pending = [b for b in bets if b.get("result") is None]
    total_profit = round(sum(_money(b.get("profit")) for b in resolved), 2)
    decided = len(won) + len(lost)

    return {
        "total_profit": total_profit,
        "win_rate": round(len(won) / decided * 100, 1) if decided else 0.0,
        "total": len(bets),
        "won": len(won),
        "lost": len(lost),
        "pending": len(pending),
        "streak": _current_streak(resolved),
    }


def _bankroll_series(bets: list[dict]) -> dict:
    bankroll = 0.0
    labels = []
    values = []
    resolved = [b for b in bets if b.get("result") in ("ganada", "perdida")]
    resolved.sort(key=lambda b: (_parse_dt(b.get("resolved_at") or b.get("created_at")), b.get("id") or 0))

    for bet in resolved:
        bankroll = round(bankroll + _money(bet.get("profit")), 2)
        dt = _parse_dt(bet.get("resolved_at") or bet.get("created_at"))
        labels.append(dt.strftime("%d/%m"))
        values.append(bankroll)

    if not labels:
        labels = ["Sin apuestas"]
        values = [0]

    return {"labels": labels, "values": values}


def _category_stats(bets: list[dict]) -> dict:
    stats = {
        "goles": {"label": "Goles", "won": 0, "lost": 0, "rate": 0.0},
        "corners": {"label": "Corners", "won": 0, "lost": 0, "rate": 0.0},
        "jugadores": {"label": "Jugadores", "won": 0, "lost": 0, "rate": 0.0},
        "resultado": {"label": "Resultado", "won": 0, "lost": 0, "rate": 0.0},
    }

    for bet in bets:
        if bet.get("result") not in ("ganada", "perdida"):
            continue
        category = _classify_bet(bet)
        bucket = stats[category]
        if bet.get("result") == "ganada":
            bucket["won"] += 1
        else:
            bucket["lost"] += 1

    for bucket in stats.values():
        total = bucket["won"] + bucket["lost"]
        bucket["rate"] = round(bucket["won"] / total * 100, 1) if total else 0.0
        bucket["total"] = total

    return stats


def _display_bets(bets: list[dict]) -> list[dict]:
    ordered = sorted(bets, key=lambda b: (_parse_dt(b.get("created_at")), b.get("id") or 0), reverse=True)
    display = []
    for bet in ordered:
        row = dict(bet)
        dt = _parse_dt(row.get("created_at"))
        row["date_label"] = dt.strftime("%d/%m/%Y %H:%M") if dt != datetime.min else "N/D"
        row["match_label"] = row.get("match_name") or row.get("league") or "Apuesta"
        row["result_label"] = _result_emoji(row.get("result"))
        row["profit_value"] = _money(row.get("profit")) if row.get("result") else None
        display.append(row)
    return display


def _load_bets() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM bets ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _load_performance_dashboard() -> dict:
    return {
        "stats": get_postgres_stats(),
        "performance_rows": get_model_performance_history(limit=50),
        "computed_performance": get_model_performance(persist=False),
        "recent_predictions": get_recent_predictions(limit=50),
        "value_bet_performance": get_value_bet_performance(),
        "value_bets": get_value_bets(limit=50),
    }


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/performance")
def api_performance():
    return jsonify({
        "stats": get_postgres_stats(),
        "computed": get_model_performance(persist=False),
        "history": get_model_performance_history(limit=50),
    })


@app.get("/api/predictions")
def api_predictions():
    return jsonify({"predictions": get_recent_predictions(limit=50)})


@app.get("/api/value-bets")
def api_value_bets():
    return jsonify({
        "performance": get_value_bet_performance(),
        "picks": get_value_bets(limit=50),
    })


@app.route("/")
@app.route("/dashboard")
def dashboard():
    bets = _load_bets()
    bankroll = _bankroll_series(bets)
    performance = _load_performance_dashboard()
    return render_template(
        "dashboard.html",
        summary=_summary(bets),
        bets=_display_bets(bets),
        category_stats=_category_stats(bets),
        postgres_stats=performance["stats"],
        model_performance=performance["performance_rows"],
        computed_model_performance=performance["computed_performance"].get("models", []),
        recent_predictions=performance["recent_predictions"],
        value_bet_performance=performance["value_bet_performance"],
        value_bets=performance["value_bets"],
        chart_labels=json.dumps(bankroll["labels"]),
        chart_values=json.dumps(bankroll["values"]),
    )


if __name__ == "__main__":
    initialize_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
