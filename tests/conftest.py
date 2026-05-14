"""tests/conftest.py
Fixtures compartilhadas para os testes da Fase 1.
Usa dados sintéticos — sem dependência de banco real ou scrapers ativos.
"""

import asyncio
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from flight_analyst.domain.models import (
    Currency,
    PriceSnapshot,
    Route,
    RouteCreate,
    ScraperSource,
)


# ---------------------------------------------------------------------------
# Fixtures de rota
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_route() -> Route:
    """Rota GRU→LHR para outubro 2025 com configurações padrão."""
    return Route(
        id=uuid4(),
        origin="GRU",
        destination="LHR",
        travel_month=date(2025, 10, 1),
        target_duration_days=14,
        flexibility_days=3,
        max_stops=1,
        currency=Currency.BRL,
        poll_interval_minutes=60,
        aggressive_poll_minutes=15,
    )


@pytest.fixture
def sample_route_create() -> RouteCreate:
    """Input válido para criar uma rota GRU→CDG."""
    return RouteCreate(
        origin="GRU",
        destination="CDG",
        travel_month=date(2025, 12, 1),
        target_duration_days=21,
        currency=Currency.BRL,
    )


# ---------------------------------------------------------------------------
# Fixtures de snapshots
# ---------------------------------------------------------------------------


def make_snapshot(
    route_id=None,
    price: float = 4500.0,
    departure_date: date | None = None,
    source: ScraperSource = ScraperSource.PLAYWRIGHT,
    airline: str | None = "LATAM",
    days_ago: int = 0,
) -> PriceSnapshot:
    """Cria um PriceSnapshot sintético."""
    from datetime import timedelta
    if route_id is None:
        route_id = uuid4()
    if departure_date is None:
        departure_date = date(2025, 10, 8)

    return PriceSnapshot(
        id=uuid4(),
        route_id=route_id,
        scraped_at=datetime.utcnow() - timedelta(days=days_ago),
        departure_date=departure_date,
        return_date=departure_date + timedelta(days=14),
        price=Decimal(str(price)),
        currency=Currency.BRL,
        airline=airline,
        stops=0,
        source=source,
    )


@pytest.fixture
def sample_snapshot(sample_route: Route) -> PriceSnapshot:
    return make_snapshot(route_id=sample_route.id)


@pytest.fixture
def snapshot_series(sample_route: Route) -> list[PriceSnapshot]:
    """
    Série de 30 snapshots com variação de preço para testes de análise.
    Simula uma sequência realista de preços ao longo de 30 dias.
    """
    prices = [
        5200, 5100, 5050, 4980, 5100, 5200, 5300,  # semana 1: alto
        4900, 4850, 4800, 4750, 4700, 4650, 4600,  # semana 2: caindo
        4500, 4480, 4450, 4420, 4400, 4380, 4350,  # semana 3: mínimo
        4400, 4450, 4500, 4600, 4700, 4800, 4900, 5000,  # semana 4: subindo
    ]
    return [
        make_snapshot(
            route_id=sample_route.id,
            price=float(p),
            days_ago=len(prices) - i,
        )
        for i, p in enumerate(prices)
    ]


@pytest.fixture
def cheap_snapshot(sample_route: Route) -> PriceSnapshot:
    """Snapshot de 'error fare' — preço muito abaixo da média."""
    return make_snapshot(route_id=sample_route.id, price=1800.0)


# ---------------------------------------------------------------------------
# Configuração de asyncio
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop_policy():
    """Usa o event loop padrão do asyncio."""
    return asyncio.DefaultEventLoopPolicy()
