from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Fixture(Base):
    __tablename__ = "fixtures"

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

    team: Mapped[Team | None] = relationship(back_populates="players")


class PlayerStat(Base):
    __tablename__ = "player_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"))
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    shots: Mapped[int | None] = mapped_column(Integer)
    shots_on_target: Mapped[int | None] = mapped_column(Integer)
    fouls_committed: Mapped[int | None] = mapped_column(Integer)
    fouls_drawn: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    assists: Mapped[int | None] = mapped_column(Integer)
    goals: Mapped[int | None] = mapped_column(Integer)
    rating: Mapped[float | None] = mapped_column(Float)


class TeamStat(Base):
    __tablename__ = "team_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    corners: Mapped[int | None] = mapped_column(Integer)
    possession: Mapped[float | None] = mapped_column(Float)
    shots: Mapped[int | None] = mapped_column(Integer)
    fouls: Mapped[int | None] = mapped_column(Integer)


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    market: Mapped[str | None] = mapped_column(String(100))
    probability: Mapped[float | None] = mapped_column(Float)
    fair_odds: Mapped[float | None] = mapped_column(Float)
    real_odds: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    recommended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Bet(Base):
    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fixture_id: Mapped[int | None] = mapped_column(ForeignKey("fixtures.id"))
    user_id: Mapped[int | None] = mapped_column(Integer)
    picks: Mapped[str | None] = mapped_column(Text)
    stake: Mapped[float | None] = mapped_column(Float)
    odds: Mapped[float | None] = mapped_column(Float)
    result: Mapped[str | None] = mapped_column(String(50))
    profit: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )


class ModelPerformance(Base):
    __tablename__ = "model_performance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    market: Mapped[str] = mapped_column(String(100), nullable=False)
    hit_rate: Mapped[float | None] = mapped_column(Float)
    roi: Mapped[float | None] = mapped_column(Float)
    sample_size: Mapped[int | None] = mapped_column(Integer)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )
