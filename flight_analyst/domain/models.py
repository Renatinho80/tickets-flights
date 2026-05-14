"""flight_analyst/domain/models.py
Modelos de domínio Pydantic v2 — entidades centrais do sistema.
Usados por scrapers, repositórios, serviços e API sem dependências externas.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, computed_field


# ---------------------------------------------------------------------------
# Enumerações
# ---------------------------------------------------------------------------



class Currency(str, Enum):
    BRL = "BRL"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


class ScraperSource(str, Enum):
    PLAYWRIGHT = "playwright"
    SERPAPI = "serpapi"
    AMADEUS = "amadeus"


class AlertType(str, Enum):
    OPPORTUNITY = "opportunity"
    ERROR_FARE = "error_fare"
    YOY_CHEAPER = "yoy_cheaper"
    BEST_ADVANCE = "best_advance"
    PRICE_DROP = "price_drop"


class OpportunityLevel(str, Enum):
    """Nível textual do Opportunity Score."""
    BUY_NOW = "COMPRE AGORA"        # 80–100
    GOOD_DEAL = "BOA OPORTUNIDADE"  # 60–79
    AVERAGE = "PREÇO MÉDIO"         # 40–59
    EXPENSIVE = "CARO"              # 20–39
    VERY_EXPENSIVE = "MUITO CARO"   # 0–19


# ---------------------------------------------------------------------------
# Entidades
# ---------------------------------------------------------------------------


class Route(BaseModel):
    """Rota monitorada — configuração de origem/destino e parâmetros de coleta."""

    id: UUID = Field(default_factory=uuid4)
    origin: str = Field(..., min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    destination: str = Field(..., min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    travel_month: date  # primeiro dia do mês alvo
    target_duration_days: int = Field(default=14, ge=1, le=90)
    flexibility_days: int = Field(default=3, ge=0, le=14)
    max_stops: int = Field(default=1, ge=0, le=3)
    currency: Currency = Currency.BRL
    poll_interval_minutes: int = Field(default=60, ge=15, le=1440)
    aggressive_poll_minutes: int = Field(default=15, ge=5, le=60)
    is_active: bool = True
    consecutive_errors: int = Field(default=0, ge=0)
    paused_until: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def uppercase_iata(cls, v: str) -> str:
        return v.strip().upper()

    @computed_field
    @property
    def label(self) -> str:
        return f"{self.origin}→{self.destination} {self.travel_month.strftime('%b/%Y')}"

    @property
    def route_key(self) -> str:
        """Chave única para identificar a rota sem considerar o mês."""
        return f"{self.origin}_{self.destination}"


class PriceSnapshot(BaseModel):
    """Snapshot de preço capturado por um scraper em um momento específico."""

    id: UUID = Field(default_factory=uuid4)
    route_id: UUID
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    departure_date: date
    return_date: date | None = None
    duration_days: int | None = None
    price: Decimal = Field(..., ge=Decimal("0"))
    currency: Currency
    airline: str | None = None
    stops: int = Field(default=0, ge=0)
    source: ScraperSource
    raw_data: dict[str, Any] | None = None

    @property
    def advance_days(self) -> int:
        """Quantos dias antes da partida o snapshot foi capturado."""
        delta = self.departure_date - self.scraped_at.date()
        return max(0, delta.days)

    def model_post_init(self, __context: Any) -> None:
        """Calcula campos automáticos após inicialização."""
        if self.departure_date and self.return_date and self.duration_days is None:
            self.duration_days = (self.return_date - self.departure_date).days


class HistoricalStat(BaseModel):
    """Estatísticas agregadas por rota, período e antecedência."""

    id: UUID = Field(default_factory=uuid4)
    route_id: UUID
    stat_year: int
    stat_month: int = Field(..., ge=1, le=12)
    stat_week_of_month: int | None = Field(default=None, ge=1, le=5)
    stat_day_of_week: int | None = Field(default=None, ge=0, le=6)  # 0=Seg
    duration_days: int | None = None
    advance_days: int | None = None
    price_min: Decimal | None = None
    price_avg: Decimal | None = None
    price_max: Decimal | None = None
    price_p25: Decimal | None = None
    price_p75: Decimal | None = None
    sample_count: int = 0
    yoy_delta_pct: float | None = None  # >0 mais caro, <0 mais barato vs ano anterior
    is_high_season: bool = False
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DurationEntry(BaseModel):
    """Entrada da matriz de comparação de durações para um mês alvo."""

    id: UUID = Field(default_factory=uuid4)
    route_id: UUID
    travel_month: date
    duration_days: int
    price_avg: Decimal | None = None
    price_min: Decimal | None = None
    delta_vs_base_pct: float | None = None  # % em relação à duração base da rota
    best_departure_date: date | None = None
    best_return_date: date | None = None
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Recommendation(BaseModel):
    """Recomendação gerada pelo motor de inteligência para uma rota."""

    id: UUID = Field(default_factory=uuid4)
    route_id: UUID
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    opportunity_score: float = Field(..., ge=0, le=100)
    current_price: Decimal | None = None
    avg_price_30d: Decimal | None = None
    avg_price_historical: Decimal | None = None
    yoy_delta_pct: float | None = None
    trend_slope: float | None = None
    days_to_departure: int | None = None
    recommendation_text: str | None = None
    is_error_fare: bool = False
    metadata: dict[str, Any] | None = None

    @property
    def opportunity_level(self) -> OpportunityLevel:
        if self.opportunity_score >= 80:
            return OpportunityLevel.BUY_NOW
        elif self.opportunity_score >= 60:
            return OpportunityLevel.GOOD_DEAL
        elif self.opportunity_score >= 40:
            return OpportunityLevel.AVERAGE
        elif self.opportunity_score >= 20:
            return OpportunityLevel.EXPENSIVE
        return OpportunityLevel.VERY_EXPENSIVE


class Alert(BaseModel):
    """Alerta disparado ao usuário via Telegram/Ntfy."""

    id: UUID = Field(default_factory=uuid4)
    route_id: UUID
    alert_type: AlertType
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    opportunity_score: float | None = None
    current_price: Decimal | None = None
    message: str
    sent_via: list[str] = Field(default_factory=list)
    is_sent: bool = False


# ---------------------------------------------------------------------------
# Input / Transfer models
# ---------------------------------------------------------------------------


class RouteCreate(BaseModel):
    """Dados de entrada para criação de uma nova rota (CLI ou API)."""

    origin: str = Field(..., min_length=3, max_length=3)
    destination: str = Field(..., min_length=3, max_length=3)
    travel_month: date
    target_duration_days: int = 14
    flexibility_days: int = 3
    max_stops: int = 1
    currency: Currency = Currency.BRL
    poll_interval_minutes: int = 60

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def uppercase_iata(cls, v: str) -> str:
        return v.strip().upper()

    def to_route(self) -> Route:
        return Route(**self.model_dump())


class RouteUpdate(BaseModel):
    """Dados de entrada para atualizar uma rota existente via API."""

    target_duration_days: int | None = Field(default=None, ge=1, le=90)
    flexibility_days: int | None = Field(default=None, ge=0, le=14)
    max_stops: int | None = Field(default=None, ge=0, le=3)
    poll_interval_minutes: int | None = Field(default=None, ge=15, le=1440)
    is_active: bool | None = None


class SearchResult(BaseModel):
    """Resultado retornado por um scraper após uma busca."""

    route_id: UUID
    snapshots: list[PriceSnapshot]
    source: ScraperSource
    success: bool
    error_message: str | None = None
    search_duration_seconds: float = 0.0
