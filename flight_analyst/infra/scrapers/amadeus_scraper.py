"""flight_analyst/infra/scrapers/amadeus_scraper.py
Scraper HISTÓRICO — Amadeus for Developers API.
Usado para seed inicial de dados históricos (Flight Inspiration + Offers).
Cota: 2.000 requisições/mês no tier gratuito (test environment).
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


class AmadeusAuthError(Exception):
    """Falha na autenticação com Amadeus."""


class AmadeusScraper(BaseScraper):
    """
    Integração com Amadeus for Developers.
    Endpoints utilizados:
    - Flight Inspiration Search: preços por destino (sem datas fixas)
    - Flight Offers Search: preços para datas específicas
    """

    def __init__(self, client_id: str, client_secret: str, base_url: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._http = httpx.AsyncClient(timeout=30.0)

    @property
    def source(self) -> ScraperSource:
        return ScraperSource.AMADEUS

    async def _get_token(self) -> str:
        """Obtém (ou reutiliza) o access token OAuth2 do Amadeus."""
        now = datetime.utcnow()
        if (
            self._access_token
            and self._token_expires_at
            and now < self._token_expires_at
        ):
            return self._access_token

        resp = await self._http.post(
            f"{self._base_url}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code != 200:
            raise AmadeusAuthError(
                f"Falha na autenticação Amadeus: {resp.status_code} {resp.text}"
            )

        data = resp.json()
        self._access_token = data["access_token"]
        expires_in = data.get("expires_in", 1799)
        self._token_expires_at = now + timedelta(seconds=expires_in - 60)
        log.info("amadeus_token_refreshed", expires_in=expires_in)
        return self._access_token

    async def _auth_headers(self) -> dict[str, str]:
        token = await self._get_token()
        return {"Authorization": f"Bearer {token}"}

    async def health_check(self) -> bool:
        """Verifica se as credenciais Amadeus são válidas."""
        if not self._client_id or not self._client_secret:
            return False
        try:
            await self._get_token()
            return True
        except Exception as exc:
            log.warning("amadeus_health_check_failed", error=str(exc))
            return False

    async def get_flight_inspiration(
        self, origin: str, currency: str = "BRL"
    ) -> list[dict[str, Any]]:
        """
        Flight Inspiration Search — retorna destinos baratos a partir de uma origem.
        Útil para descobrir sazonalidade e meses mais baratos.
        """
        headers = await self._auth_headers()
        resp = await self._http.get(
            f"{self._base_url}/v1/shopping/flight-destinations",
            headers=headers,
            params={"origin": origin, "currency": currency, "oneWay": "false"},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=30),
    )
    async def search_offers(
        self,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: date | None,
        adults: int = 1,
        currency: str = "BRL",
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Flight Offers Search para datas específicas."""
        headers = await self._auth_headers()
        params: dict[str, Any] = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure_date.strftime("%Y-%m-%d"),
            "adults": adults,
            "currencyCode": currency,
            "max": max_results,
        }
        if return_date:
            params["returnDate"] = return_date.strftime("%Y-%m-%d")

        resp = await self._http.get(
            f"{self._base_url}/v2/shopping/flight-offers",
            headers=headers,
            params=params,
        )

        if resp.status_code == 400:
            log.warning("amadeus_bad_request", params=params, body=resp.text[:200])
            return []

        resp.raise_for_status()
        return resp.json().get("data", [])

    def _parse_offer(
        self,
        offer: dict[str, Any],
        route: Route,
        departure_date: date,
        return_date: date | None,
        scraped_at: datetime,
    ) -> PriceSnapshot | None:
        """Converte uma oferta Amadeus em PriceSnapshot."""
        try:
            price_total = offer["price"]["grandTotal"]
            price = Decimal(str(price_total))
        except (KeyError, TypeError, Exception):
            return None

        # Extrai companhia do primeiro itinerário
        airline = None
        itineraries = offer.get("itineraries", [])
        if itineraries:
            segments = itineraries[0].get("segments", [])
            if segments:
                airline = segments[0].get("carrierCode")

        # Número de escalas (segmentos - 1)
        stops = max(0, len(itineraries[0].get("segments", [])) - 1) if itineraries else 0

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
            source=ScraperSource.AMADEUS,
            raw_data={"offer_id": offer.get("id"), "source": offer.get("source")},
        )

    async def search(
        self,
        route: Route,
        departure_date: date,
        return_date: date | None = None,
    ) -> SearchResult:
        """Busca ofertas no Amadeus para datas específicas."""
        snapshots: list[PriceSnapshot] = []
        now = datetime.utcnow()

        try:
            offers = await self.search_offers(
                origin=route.origin,
                destination=route.destination,
                departure_date=departure_date,
                return_date=return_date,
                currency=route.currency.value,
            )

            for offer in offers:
                snapshot = self._parse_offer(offer, route, departure_date, return_date, now)
                if snapshot:
                    snapshots.append(snapshot)

            return SearchResult(
                route_id=route.id,
                snapshots=snapshots,
                source=ScraperSource.AMADEUS,
                success=True,
            )

        except Exception as exc:
            log.error("amadeus_search_error", error=str(exc))
            return SearchResult(
                route_id=route.id,
                snapshots=[],
                source=ScraperSource.AMADEUS,
                success=False,
                error_message=str(exc),
            )

    async def search_month(
        self,
        route: Route,
        year: int,
        month: int,
    ) -> SearchResult:
        """
        Busca preços para múltiplas datas no mês via Amadeus.
        Usa Flight Inspiration + Offers Search para cobrir o período.
        """
        all_snapshots: list[PriceSnapshot] = []
        _, num_days = calendar.monthrange(year, month)

        # Amostrar 4 semanas do mês
        sample_days = [d for d in [1, 8, 15, 22] if d <= num_days]

        for day in sample_days:
            dep_date = date(year, month, day)
            ret_date = dep_date + timedelta(days=route.target_duration_days)

            result = await self.safe_search(route, dep_date, ret_date)
            if result.success:
                all_snapshots.extend(result.snapshots)

        return SearchResult(
            route_id=route.id,
            snapshots=all_snapshots,
            source=ScraperSource.AMADEUS,
            success=len(all_snapshots) > 0,
        )

    async def close(self) -> None:
        await self._http.aclose()
