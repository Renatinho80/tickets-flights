"""flight_analyst/application/monitor_service.py
Serviço de monitoramento — orquestra a coleta de preços com cascata de scrapers.
Implementa circuit breaker, rate limiting e seleção automática de fonte.
"""

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from flight_analyst.domain.models import Route, SearchResult, ScraperSource
from flight_analyst.domain.rules import (
    CIRCUIT_BREAKER_MAX_FAILURES,
    CIRCUIT_BREAKER_PAUSE_HOURS,
    MIN_POLL_INTERVAL_SECONDS,
)
from flight_analyst.infra.db.repositories.route_repo import RouteRepository
from flight_analyst.infra.db.repositories.snapshot_repo import SnapshotRepository
from flight_analyst.infra.scrapers.base import BaseScraper

log = structlog.get_logger(__name__)


class CircuitBreaker:
    """
    Circuit breaker por scraper/rota.
    Após MAX_FAILURES falhas consecutivas, pausa por PAUSE_HOURS.
    """

    def __init__(self) -> None:
        self._failures: dict[str, int] = defaultdict(int)
        self._paused_until: dict[str, datetime] = {}

    def record_success(self, key: str) -> None:
        self._failures[key] = 0
        self._paused_until.pop(key, None)

    def record_failure(self, key: str) -> None:
        self._failures[key] += 1
        if self._failures[key] >= CIRCUIT_BREAKER_MAX_FAILURES:
            pause_until = datetime.utcnow() + timedelta(hours=CIRCUIT_BREAKER_PAUSE_HOURS)
            self._paused_until[key] = pause_until
            log.warning(
                "circuit_breaker_open",
                key=key,
                failures=self._failures[key],
                paused_until=pause_until.isoformat(),
            )

    def is_open(self, key: str) -> bool:
        """Retorna True se o circuit breaker está aberto (pausado)."""
        if key not in self._paused_until:
            return False
        if datetime.utcnow() >= self._paused_until[key]:
            # Resetar após o período de pausa
            del self._paused_until[key]
            self._failures[key] = 0
            log.info("circuit_breaker_reset", key=key)
            return False
        return True

    def status(self) -> dict[str, Any]:
        return {
            "failures": dict(self._failures),
            "paused_until": {k: v.isoformat() for k, v in self._paused_until.items()},
        }


class MonitorService:
    """
    Serviço central de monitoramento de preços.

    Fluxo de coleta:
    1. Carrega rotas ativas
    2. Para cada rota, tenta scrapers em cascata: Playwright → SerpApi → Amadeus
    3. Persiste snapshots no banco
    4. Emite eventos para o motor de análise
    """

    def __init__(
        self,
        scrapers: list[BaseScraper],
        route_repo: RouteRepository,
        snapshot_repo: SnapshotRepository,
    ) -> None:
        self._scrapers = scrapers  # Ordem = prioridade
        self._route_repo = route_repo
        self._snapshot_repo = snapshot_repo
        self._circuit_breaker = CircuitBreaker()
        self._last_poll: dict[str, datetime] = {}

    def _scraper_key(self, scraper: BaseScraper, route: Route) -> str:
        return f"{scraper.source.value}:{route.id}"

    def _route_key(self, route: Route) -> str:
        return str(route.id)

    def _can_poll(self, route: Route) -> bool:
        """Verifica se a rota está pronta para um novo poll (rate limiting)."""
        key = self._route_key(route)
        last = self._last_poll.get(key)
        if last is None:
            return True
        elapsed = (datetime.utcnow() - last).total_seconds()
        return elapsed >= MIN_POLL_INTERVAL_SECONDS

    async def collect_for_route(
        self,
        route: Route,
        departure_date: date | None = None,
        return_date: date | None = None,
    ) -> SearchResult | None:
        """
        Executa a coleta para uma rota específica usando cascata de scrapers.
        Retorna o primeiro SearchResult bem-sucedido.
        """
        if not self._can_poll(route):
            log.debug("poll_skipped_rate_limit", route=route.label)
            return None

        # Datas padrão: 1º do mês alvo + target_duration
        if departure_date is None:
            departure_date = route.travel_month
        if return_date is None:
            return_date = departure_date + timedelta(days=route.target_duration_days)

        for scraper in self._scrapers:
            cb_key = self._scraper_key(scraper, route)

            if self._circuit_breaker.is_open(cb_key):
                log.info(
                    "scraper_circuit_open_skip",
                    scraper=scraper.source.value,
                    route=route.label,
                )
                continue

            result = await scraper.safe_search(route, departure_date, return_date)

            if result.success and result.snapshots:
                self._circuit_breaker.record_success(cb_key)
                self._last_poll[self._route_key(route)] = datetime.utcnow()

                # Persistir snapshots
                saved = await self._snapshot_repo.save_bulk(result.snapshots)
                log.info(
                    "snapshots_saved",
                    route=route.label,
                    source=scraper.source.value,
                    count=saved,
                    min_price=float(min(s.price for s in result.snapshots)),
                )
                return result
            else:
                self._circuit_breaker.record_failure(cb_key)
                log.warning(
                    "scraper_failed",
                    scraper=scraper.source.value,
                    route=route.label,
                    error=result.error_message,
                )

        log.error("all_scrapers_failed", route=route.label)
        return None

    async def collect_all_routes(self) -> dict[str, SearchResult | None]:
        """
        Executa a coleta para todas as rotas ativas.
        Retorna mapa route_id → resultado.
        """
        routes = await self._route_repo.get_all_active()
        if not routes:
            log.info("no_active_routes")
            return {}

        log.info("collecting_all_routes", count=len(routes))
        results: dict[str, SearchResult | None] = {}

        for route in routes:
            result = await self.collect_for_route(route)
            results[str(route.id)] = result
            # Pausa entre rotas para evitar sobrecarga
            await asyncio.sleep(MIN_POLL_INTERVAL_SECONDS)

        return results

    async def collect_month_history(
        self,
        route: Route,
        year: int,
        month: int,
    ) -> SearchResult | None:
        """
        Coleta preços para um mês inteiro (usado no seed histórico).
        Usa o método search_month de cada scraper.
        """
        for scraper in self._scrapers:
            cb_key = self._scraper_key(scraper, route)

            if self._circuit_breaker.is_open(cb_key):
                continue

            try:
                result = await scraper.search_month(route, year, month)
                if result.success and result.snapshots:
                    self._circuit_breaker.record_success(cb_key)
                    await self._snapshot_repo.save_bulk(result.snapshots)
                    log.info(
                        "month_history_collected",
                        route=route.label,
                        year=year,
                        month=month,
                        source=scraper.source.value,
                        count=len(result.snapshots),
                    )
                    return result
                else:
                    self._circuit_breaker.record_failure(cb_key)
            except Exception as exc:
                log.error(
                    "month_collect_error",
                    scraper=scraper.source.value,
                    route=route.label,
                    error=str(exc),
                )
                self._circuit_breaker.record_failure(cb_key)

        return None

    def get_circuit_breaker_status(self) -> dict[str, Any]:
        """Retorna o status atual do circuit breaker para diagnóstico."""
        return self._circuit_breaker.status()
