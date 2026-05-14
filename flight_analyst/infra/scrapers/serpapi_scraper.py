"""flight_analyst/infra/scrapers/serpapi_scraper.py
Scraper FALLBACK — Google Flights via SerpApi (API JSON limpa).
Ativa automaticamente quando Playwright falha 3x consecutivas.
Cota: 100 requisições/mês no tier gratuito — usar com parcimônia.
"""

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from flight_analyst.domain.models import (
    Currency,
    PriceSnapshot,
    Route,
    ScraperSource,
    SearchResult,
)
from flight_analyst.infra.scrapers.base import BaseScraper

log = structlog.get_logger(__name__)

SERPAPI_BASE_URL = "https://serpapi.com/search"


class SerpApiScraper(BaseScraper):
    """
    Wrapper da SerpApi para Google Flights.
    Documentação: https://serpapi.com/google-flights-api
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=30.0)

    @property
    def source(self) -> ScraperSource:
        return ScraperSource.SERPAPI

    async def health_check(self) -> bool:
        """Verifica se a chave da SerpApi é válida."""
        if not self._api_key:
            return False
        try:
            resp = await self._client.get(
                "https://serpapi.com/account",
                params={"api_key": self._api_key},
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception as exc:
            log.warning("serpapi_health_check_failed", error=str(exc))
            return False

    def _build_params(
        self,
        route: Route,
        departure_date: date,
        return_date: date | None,
    ) -> dict[str, str]:
        params: dict[str, str] = {
            "engine": "google_flights",
            "departure_id": route.origin,
            "arrival_id": route.destination,
            "outbound_date": departure_date.strftime("%Y-%m-%d"),
            "currency": route.currency.value,
            "hl": "pt",
            "gl": "br",
            "api_key": self._api_key,
            "max_stops": str(route.max_stops),
        }
        if return_date:
            params["return_date"] = return_date.strftime("%Y-%m-%d")
            params["type"] = "1"  # Round-trip
        else:
            params["type"] = "2"  # One-way
        return params

    def _parse_flight_result(
        self,
        item: dict[str, Any],
        route: Route,
        departure_date: date,
        return_date: date | None,
        scraped_at: datetime,
    ) -> PriceSnapshot | None:
        """Converte um resultado da SerpApi em PriceSnapshot."""
        price_raw = item.get("price")
        if price_raw is None:
            return None

        try:
            price = Decimal(str(price_raw))
        except Exception:
            return None

        # Extrai airline do primeiro leg
        airline = None
        flights = item.get("flights", [])
        if flights:
            airline = flights[0].get("airline")

        stops = len(item.get("layovers", []))

        return PriceSnapshot(
            id=uuid4(),
            route_id=route.id,
            scraped_at=scraped_at,
            departure_date=departure_date,
            return_date=return_date,
            price=price,
            currency=route.currency,
            airline=airline,
            stops=stops,
            source=ScraperSource.SERPAPI,
            raw_data={
                "total_duration": item.get("total_duration"),
                "carbon_emissions": item.get("carbon_emissions"),
                "type": item.get("type"),
            },
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=30),
    )
    async def search(
        self,
        route: Route,
        departure_date: date,
        return_date: date | None = None,
    ) -> SearchResult:
        """Busca voos via SerpApi para datas específicas."""
        params = self._build_params(route, departure_date, return_date)
        snapshots: list[PriceSnapshot] = []

        try:
            resp = await self._client.get(SERPAPI_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

            if error := data.get("error"):
                return SearchResult(
                    route_id=route.id,
                    snapshots=[],
                    source=ScraperSource.SERPAPI,
                    success=False,
                    error_message=error,
                )

            now = datetime.utcnow()

            # best_flights: melhores opções
            # other_flights: demais opções
            for key in ("best_flights", "other_flights"):
                for item in data.get(key, []):
                    snapshot = self._parse_flight_result(
                        item, route, departure_date, return_date, now
                    )
                    if snapshot:
                        snapshots.append(snapshot)

            log.info(
                "serpapi_search_done",
                route=route.label,
                results=len(snapshots),
                remaining_credits=data.get("search_information", {}).get(
                    "query_displayed", "?"
                ),
            )

            return SearchResult(
                route_id=route.id,
                snapshots=snapshots,
                source=ScraperSource.SERPAPI,
                success=True,
            )

        except httpx.HTTPStatusError as exc:
            log.error("serpapi_http_error", status=exc.response.status_code, error=str(exc))
            raise
        except Exception as exc:
            log.error("serpapi_error", error=str(exc))
            raise

    async def search_month(
        self,
        route: Route,
        year: int,
        month: int,
    ) -> SearchResult:
        """
        Busca preços para dias-chave do mês.
        Limitado pelas cotas — usa apenas 4 buscas por mês.
        """
        all_snapshots: list[PriceSnapshot] = []
        _, num_days = calendar.monthrange(year, month)

        sample_days = [1, 8, 15, 22]
        for day in sample_days:
            if day > num_days:
                break
            dep_date = date(year, month, day)
            ret_date = dep_date + timedelta(days=route.target_duration_days)
            result = await self.safe_search(route, dep_date, ret_date)
            if result.success:
                all_snapshots.extend(result.snapshots)

        return SearchResult(
            route_id=route.id,
            snapshots=all_snapshots,
            source=ScraperSource.SERPAPI,
            success=len(all_snapshots) > 0,
        )

    async def close(self) -> None:
        await self._client.aclose()
