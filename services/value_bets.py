from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValueBetResult:
    implied_probability: float | None
    edge: float | None
    expected_value: float | None
    value_score: float | None
    recommended: bool
    pick_type: str | None


def implied_probability(decimal_odds: float | None) -> float | None:
    if decimal_odds is None or decimal_odds <= 1:
        return None
    return 1 / decimal_odds


def expected_value(probability: float, decimal_odds: float | None, stake: float = 1.0) -> float | None:
    if decimal_odds is None or decimal_odds <= 1:
        return None
    return probability * (decimal_odds - 1) * stake - (1 - probability) * stake


def classify_pick(probability: float, ev: float | None, confidence: float) -> str | None:
    if ev is None or ev <= 0:
        return None
    if probability >= 0.68 and confidence >= 70:
        return "Pick Seguro"
    if probability >= 0.54 and confidence >= 60:
        return "Value Bet"
    if probability >= 0.45 and ev >= 0.18:
        return "Soñada"
    return None


def analyze_value(probability: float, decimal_odds: float | None, confidence: float) -> ValueBetResult:
    implied = implied_probability(decimal_odds)
    ev = expected_value(probability, decimal_odds)
    edge = probability - implied if implied is not None else None
    value_score = edge * confidence / 100 if edge is not None else None
    pick_type = classify_pick(probability, ev, confidence)
    return ValueBetResult(
        implied_probability=round(implied, 4) if implied is not None else None,
        edge=round(edge, 4) if edge is not None else None,
        expected_value=round(ev, 4) if ev is not None else None,
        value_score=round(value_score, 4) if value_score is not None else None,
        recommended=pick_type is not None,
        pick_type=pick_type,
    )
