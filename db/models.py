from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Fixture(Base):
    __tablename__ = "fixtures"
    __table_args__ = (
        Index("ix_fixtures_status_date", "status", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    home_team: Mapped[str] = mapped_column(String(255), nullable=False)
    away_team: Mapped[str] = mapped_column(String(255), nullable=False)
    league: Mapped[str | None] = mapped_column(String(255))
    date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(50))
    result: Mapped[str | None] = mapped_column(String(100))


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    league: Mapped[str | None] = mapped_column(String(255))

    players: Mapped[list["Player"]] = relationship(back_populates="team")


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    position: Mapped[str | None] = mapped_column(String(50))
    nationality: Mapped[str | None] = mapped_column(String(100))
    age: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[str | None] = mapped_column(String(50))
    weight: Mapped[str | None] = mapped_column(String(50))
    birth_date: Mapped[str | None] = mapped_column(String(50))
    birth_place: Mapped[str | None] = mapped_column(String(255))

    team: Mapped[Team | None] = relationship(back_populates="players")


class PlayerStat(Base):
    __tablename__ = "player_stats"
    __table_args__ = (
        Index("ix_player_stats_fixture_player", "fixture_id", "player_id"),
        UniqueConstraint("fixture_id", "player_id", name="uq_player_stats_fixture_player"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    appearances: Mapped[int | None] = mapped_column(Integer)
    minutes: Mapped[int | None] = mapped_column(Integer)
    shots: Mapped[int | None] = mapped_column(Integer)
    shots_on_target: Mapped[int | None] = mapped_column(Integer)
    fouls_committed: Mapped[int | None] = mapped_column(Integer)
    fouls_drawn: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    assists: Mapped[int | None] = mapped_column(Integer)
    goals: Mapped[int | None] = mapped_column(Integer)
    yellow_cards: Mapped[int | None] = mapped_column(Integer)
    red_cards: Mapped[int | None] = mapped_column(Integer)
    rating: Mapped[float | None] = mapped_column(Float)


class InjuryReport(Base):
    __tablename__ = "injury_reports"
    __table_args__ = (
        UniqueConstraint("fixture_id", "player_id", "reason", name="uq_injury_fixture_player_reason"),
        Index("ix_injury_reports_fixture_id", "fixture_id"),
        Index("ix_injury_reports_team_id", "team_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    player_name: Mapped[str | None] = mapped_column(String(255))
    team_name: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str | None] = mapped_column(String(100))
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )


class TeamStat(Base):
    __tablename__ = "team_stats"
    __table_args__ = (
        Index("ix_team_stats_fixture_team", "fixture_id", "team_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    corners: Mapped[int | None] = mapped_column(Integer)
    possession: Mapped[float | None] = mapped_column(Float)
    shots: Mapped[int | None] = mapped_column(Integer)
    fouls: Mapped[int | None] = mapped_column(Integer)


class GameEvent(Base):
    __tablename__ = "game_events"
    __table_args__ = (
        UniqueConstraint("source", "source_event_id", name="uq_game_events_source_event"),
        Index("ix_game_events_fixture_id", "fixture_id"),
        Index("ix_game_events_player_id", "player_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(100), nullable=False)
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    related_player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    minute: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    event_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlayerAttribute(Base):
    __tablename__ = "player_attributes"
    __table_args__ = (
        UniqueConstraint("source", "source_attribute_id", name="uq_player_attributes_source_attribute"),
        Index("ix_player_attributes_player_id", "player_id"),
        Index("ix_player_attributes_date", "attribute_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_attribute_id: Mapped[str] = mapped_column(String(100), nullable=False)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    attribute_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    overall_rating: Mapped[int | None] = mapped_column(Integer)
    potential: Mapped[int | None] = mapped_column(Integer)
    preferred_foot: Mapped[str | None] = mapped_column(String(50))
    attacking_work_rate: Mapped[str | None] = mapped_column(String(50))
    defensive_work_rate: Mapped[str | None] = mapped_column(String(50))


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    __table_args__ = (
        Index("ix_odds_snapshots_fixture_market_time", "fixture_id", "market", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    bookmaker: Mapped[str | None] = mapped_column(String(255))
    market: Mapped[str | None] = mapped_column(String(100))
    home_odds: Mapped[float | None] = mapped_column(Float)
    draw_odds: Mapped[float | None] = mapped_column(Float)
    away_odds: Mapped[float | None] = mapped_column(Float)
    over_odds: Mapped[float | None] = mapped_column(Float)
    under_odds: Mapped[float | None] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )


class Prediction(Base):
    __tablename__ = "predictions"
    # One stored prediction per (fixture, market, model). This keeps model
    # versions comparable without inserting duplicate rows for repeated runs.
    __table_args__ = (
        UniqueConstraint("fixture_id", "market", "model_name", name="uq_predictions_fixture_market_model"),
        Index("ix_predictions_fixture_id", "fixture_id"),
        Index("ix_predictions_market_model", "market", "model_name"),
        Index("ix_predictions_predicted_at", "predicted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    market: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(255), default="unknown", nullable=False)
    league: Mapped[str | None] = mapped_column(String(255))
    probability: Mapped[float | None] = mapped_column(Float)
    implied_probability: Mapped[float | None] = mapped_column(Float)
    fair_odds: Mapped[float | None] = mapped_column(Float)
    real_odds: Mapped[float | None] = mapped_column(Float)
    edge: Mapped[float | None] = mapped_column(Float)
    value_score: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    recommended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pick_type: Mapped[str | None] = mapped_column(String(50))
    predicted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )
    closing_odds: Mapped[float | None] = mapped_column(Float)
    closing_implied_probability: Mapped[float | None] = mapped_column(Float)
    clv: Mapped[float | None] = mapped_column(Float)
    correct: Mapped[bool | None] = mapped_column(Boolean)
    profit: Mapped[float | None] = mapped_column(Float)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# NOTE: bets are stored exclusively in SQLite (services/database.py). There is no
# PostgreSQL bets table on purpose, to avoid a second, unused source of truth.


class ModelPerformance(Base):
    __tablename__ = "model_performance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    market: Mapped[str] = mapped_column(String(100), nullable=False)
    hit_rate: Mapped[float | None] = mapped_column(Float)
    roi: Mapped[float | None] = mapped_column(Float)
    yield_rate: Mapped[float | None] = mapped_column(Float)
    brier_score: Mapped[float | None] = mapped_column(Float)
    log_loss: Mapped[float | None] = mapped_column(Float)
    league: Mapped[str | None] = mapped_column(String(255))
    best_league: Mapped[str | None] = mapped_column(String(255))
    best_market: Mapped[str | None] = mapped_column(String(100))
    sample_size: Mapped[int | None] = mapped_column(Integer)
    # Real number of picks evaluated on the held-out test set (not the full
    # dataset size, which is stored in sample_size).
    total_picks: Mapped[int | None] = mapped_column(Integer)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )
