"""flight_analyst/infra/scrapers/base.py
Interface abstrata (ABC) para todos os scrapers de preços.
Garante que o MonitorService possa usar qualquer fonte sem conhecer detalhes.
"""

import time
from abc import ABC, abstractmethod
from datetime import date
from uuid import UUID

import structlog

from flight_analyst.domain.models import Route, SearchResult, ScraperSource

log = structlog.get_logger(__name__)


class BaseScraper(ABC):
    """
    Contrato que todo scraper deve implementar.
    Cada implementação representa uma fonte de dados diferente:
    - PlaywrightScraper: Google Flights via browser headless (primário)
    - SerpApiScraper: Google Flights via API JSON (fallback pago)
    - AmadeusScraper: Amadeus Flight API (dados históricos)
    """

    @property
    @abstractmethod
    def source(self) -> ScraperSource:
        """Identificador da fonte de dados."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Verifica se a fonte está acessível. True = OK."""
        ...

    @abstractmethod
    async def search(
        self,
        route: Route,
        departure_date: date,
        return_date: date | None = None,
    ) -> SearchResult:
        """
        Busca preços para uma rota em datas específicas.

        Args:
            route: Rota configurada (origem, destino, parâmetros)
            departure_date: Data de partida
            return_date: Data de retorno (None para só ida)

        Returns:
            SearchResult com snapshots de preços ou erro
        """
        ...

    @abstractmethod
    async def search_month(
        self,
        route: Route,
        year: int,
        month: int,
    ) -> SearchResult:
        """
        Busca os melhores preços para um mês inteiro (visão de calendário).
        Útil para popular histórico com poucas requisições.

        Args:
            route: Rota configurada
            year: Ano alvo
            month: Mês alvo (1–12)

        Returns:
            SearchResult com snapshots para múltiplos dias
        """
        ...

    async def safe_search(
        self,
        route: Route,
        departure_date: date,
        return_date: date | None = None,
    ) -> SearchResult:
        """
        Wrapper com timing e logging automático.
        Use este método em vez de search() diretamente.
        """
        started_at = time.monotonic()
        log.info(
            "scraper_search_start",
            source=self.source.value,
            route=route.label,
            departure=departure_date.isoformat(),
        )
        try:
            result = await self.search(route, departure_date, return_date)
            result.search_duration_seconds = time.monotonic() - started_at
            log.info(
                "scraper_search_done",
                source=self.source.value,
                route=route.label,
                snapshots=len(result.snapshots),
                duration_s=round(result.search_duration_seconds, 2),
            )
            return result
        except Exception as exc:
            duration = time.monotonic() - started_at
            log.error(
                "scraper_search_error",
                source=self.source.value,
                route=route.label,
                error=str(exc),
                duration_s=round(duration, 2),
            )
            return SearchResult(
                route_id=route.id,
                snapshots=[],
                source=self.source,
                success=False,
                error_message=str(exc),
                search_duration_seconds=duration,
            )
