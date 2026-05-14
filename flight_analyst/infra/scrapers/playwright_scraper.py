"""flight_analyst/infra/scrapers/playwright_scraper.py
Scraper PRIMÁRIO — Google Flights via browser headless com Playwright.
Usa playwright-stealth para evitar detecção. Ilimitado e sem cota de API.
"""

import asyncio
import calendar
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from flight_analyst.domain.models import (
    Currency,
    PriceSnapshot,
    Route,
    ScraperSource,
    SearchResult,
)
from flight_analyst.infra.scrapers.base import BaseScraper

log = structlog.get_logger(__name__)

# URL base do Google Flights
GOOGLE_FLIGHTS_BASE = "https://www.google.com/travel/flights"

# Timeout para carregar a página (ms)
PAGE_TIMEOUT_MS = 30_000

# Pausa entre interações para simular comportamento humano (segundos)
HUMAN_PAUSE_MIN = 1.5
HUMAN_PAUSE_MAX = 3.0


class PlaywrightScraper(BaseScraper):
    """
    Scraper de preços usando Playwright + playwright-stealth.
    Estratégia: abre o Google Flights, preenche a busca e extrai preços.
    """

    def __init__(self) -> None:
        self._browser: Any = None
        self._playwright: Any = None

    @property
    def source(self) -> ScraperSource:
        return ScraperSource.PLAYWRIGHT

    async def _get_browser(self) -> Any:
        """Inicializa o Playwright e o browser se ainda não estiverem ativos."""
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
        return self._browser

    async def _new_page(self) -> Any:
        """Cria uma nova página com stealth aplicado."""
        browser = await self._get_browser()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
        )
        page = await context.new_page()

        # Aplicar stealth se disponível
        try:
            from playwright_stealth import stealth_async  # type: ignore[import]
            await stealth_async(page)
        except ImportError:
            log.warning("playwright_stealth_not_installed")

        return page

    async def close(self) -> None:
        """Fecha o browser e o Playwright."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def health_check(self) -> bool:
        """Verifica se o Playwright consegue abrir o Google Flights."""
        try:
            page = await self._new_page()
            response = await page.goto(
                "https://www.google.com/travel/flights",
                timeout=PAGE_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            await page.close()
            return response is not None and response.ok
        except Exception as exc:
            log.warning("playwright_health_check_failed", error=str(exc))
            return False

    def _build_flights_url(
        self,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: date | None,
        currency: str = "BRL",
    ) -> str:
        """
        Constrói a URL do Google Flights para uma busca específica.
        Formato: /travel/flights/search com parâmetros de texto.
        """
        dep_str = departure_date.strftime("%Y-%m-%d")
        if return_date:
            ret_str = return_date.strftime("%Y-%m-%d")
            # Round-trip URL
            return (
                f"{GOOGLE_FLIGHTS_BASE}?q=Flights+from+{origin}+to+{destination}"
                f"&tfs=&curr={currency}"
            )
        return (
            f"{GOOGLE_FLIGHTS_BASE}?q=Flights+from+{origin}+to+{destination}"
            f"&tfs=&curr={currency}"
        )

    async def _search_with_form(
        self,
        page: Any,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: date | None,
        currency: str,
    ) -> list[dict[str, Any]]:
        """Preenche o formulário do Google Flights e extrai resultados."""
        await page.goto(GOOGLE_FLIGHTS_BASE, timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
        await asyncio.sleep(HUMAN_PAUSE_MIN)

        # Se for round-trip, garantir que o modo está selecionado
        # Google Flights abre em round-trip por padrão

        # Limpar e preencher campo de origem
        try:
            # Tentar encontrar o campo de origem
            origin_input = await page.query_selector('[placeholder*="Where from"], [aria-label*="Where from"], [data-placeholder*="Origem"]')
            if origin_input:
                await origin_input.click()
                await asyncio.sleep(0.5)
                await origin_input.triple_click()
                await origin_input.type(origin, delay=100)
                await asyncio.sleep(1.0)
                # Selecionar primeira sugestão
                suggestion = await page.query_selector('[role="option"]')
                if suggestion:
                    await suggestion.click()
                await asyncio.sleep(0.5)
        except Exception as exc:
            log.debug("origin_field_error", error=str(exc))

        # Preencher destino
        try:
            dest_input = await page.query_selector('[placeholder*="Where to"], [aria-label*="Where to"], [data-placeholder*="Destino"]')
            if dest_input:
                await dest_input.click()
                await asyncio.sleep(0.5)
                await dest_input.triple_click()
                await dest_input.type(destination, delay=100)
                await asyncio.sleep(1.0)
                suggestion = await page.query_selector('[role="option"]')
                if suggestion:
                    await suggestion.click()
                await asyncio.sleep(0.5)
        except Exception as exc:
            log.debug("destination_field_error", error=str(exc))

        # Aguardar carregamento de resultados
        await asyncio.sleep(HUMAN_PAUSE_MAX)

        # Extrair preços do DOM
        return await self._extract_prices_from_page(page)

    async def _extract_prices_from_page(self, page: Any) -> list[dict[str, Any]]:
        """
        Extrai informações de preços e voos da página atual.
        Usa múltiplos seletores para resiliência a mudanças no layout.
        """
        results: list[dict[str, Any]] = []

        try:
            # Aguardar resultados de voos
            await page.wait_for_selector(
                '[data-iata], .pIav2d, [aria-label*="price"], [class*="flight"]',
                timeout=15_000,
            )
        except Exception:
            pass

        # Extrair via JavaScript para maior confiabilidade
        prices_js = await page.evaluate("""
            () => {
                const results = [];
                // Seletor genérico para cards de voo
                const cards = document.querySelectorAll(
                    'li[data-iata], [jsname="IWWDBc"], [class*="pIav2d"], ' +
                    'ul.Rk10dc > li, [role="listitem"]'
                );

                cards.forEach(card => {
                    const priceEl = card.querySelector(
                        '[class*="YMlIz"], [class*="FpEdX"], [data-gs], ' +
                        '[aria-label*="R$"], [aria-label*="BRL"]'
                    );
                    const airlineEl = card.querySelector(
                        '[class*="sSHqwe"], [class*="h1fkLb"], [class*="Ir0Voe"]'
                    );
                    const durationEl = card.querySelector(
                        '[class*="gvkrdb"], [class*="AdWm1c"]'
                    );
                    const stopsEl = card.querySelector(
                        '[class*="EfT7Ae"], [class*="ogfYpf"]'
                    );

                    if (priceEl) {
                        results.push({
                            price_text: priceEl.textContent || priceEl.getAttribute('aria-label') || '',
                            airline: airlineEl ? airlineEl.textContent : null,
                            duration: durationEl ? durationEl.textContent : null,
                            stops: stopsEl ? stopsEl.textContent : null,
                        });
                    }
                });

                return results.slice(0, 20);  // Max 20 resultados
            }
        """)

        for item in (prices_js or []):
            price = self._parse_price(item.get("price_text", ""))
            if price:
                results.append({
                    "price": price,
                    "airline": self._clean_text(item.get("airline")),
                    "duration": item.get("duration"),
                    "stops_text": item.get("stops"),
                    "stops": self._parse_stops(item.get("stops", "")),
                })

        log.debug("prices_extracted", count=len(results))
        return results

    def _parse_price(self, text: str) -> float | None:
        """Extrai valor numérico de texto de preço (ex: 'R$\u00a04.523' → 4523.0)."""
        if not text:
            return None
        # Remove símbolos de moeda e espaços não-quebráveis
        cleaned = re.sub(r"[^\d.,]", "", text.replace("\xa0", "").replace(" ", ""))
        if not cleaned:
            return None
        # Formato brasileiro: 4.523,00 ou americano: 4,523.00
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                # Formato BR: 4.523,00
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                # Formato US: 4,523.00
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try:
            value = float(cleaned)
            return value if value > 0 else None
        except ValueError:
            return None

    def _parse_stops(self, text: str) -> int:
        """Extrai número de escalas do texto (ex: '1 stop' → 1, 'Nonstop' → 0)."""
        if not text:
            return 0
        text_lower = text.lower()
        if "nonstop" in text_lower or "direto" in text_lower or "sem escala" in text_lower:
            return 0
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else 1

    def _clean_text(self, text: str | None) -> str | None:
        if not text:
            return None
        return " ".join(text.split())

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=30),
        retry=retry_if_exception_type(Exception),
    )
    async def search(
        self,
        route: Route,
        departure_date: date,
        return_date: date | None = None,
    ) -> SearchResult:
        """Busca preços para uma data específica via Playwright."""
        page = await self._new_page()
        snapshots: list[PriceSnapshot] = []

        try:
            raw_results = await self._search_with_form(
                page,
                origin=route.origin,
                destination=route.destination,
                departure_date=departure_date,
                return_date=return_date,
                currency=route.currency.value,
            )

            now = datetime.utcnow()
            for item in raw_results:
                if item["price"] is None:
                    continue
                snapshot = PriceSnapshot(
                    id=uuid4(),
                    route_id=route.id,
                    scraped_at=now,
                    departure_date=departure_date,
                    return_date=return_date,
                    price=Decimal(str(item["price"])),
                    currency=route.currency,
                    airline=item.get("airline"),
                    stops=item.get("stops", 0),
                    source=ScraperSource.PLAYWRIGHT,
                    raw_data=item,
                )
                snapshots.append(snapshot)

            return SearchResult(
                route_id=route.id,
                snapshots=snapshots,
                source=ScraperSource.PLAYWRIGHT,
                success=True,
            )

        finally:
            await page.close()

    async def search_month(
        self,
        route: Route,
        year: int,
        month: int,
    ) -> SearchResult:
        """
        Busca preços para o mês inteiro usando o calendário do Google Flights.
        Faz uma busca por semana para maximizar cobertura.
        """
        all_snapshots: list[PriceSnapshot] = []
        _, num_days = calendar.monthrange(year, month)

        # Buscar uma vez por semana para cobrir o mês
        check_dates = [
            date(year, month, 1),
            date(year, month, 8) if num_days >= 8 else date(year, month, num_days),
            date(year, month, 15) if num_days >= 15 else date(year, month, num_days),
            date(year, month, 22) if num_days >= 22 else date(year, month, num_days),
        ]

        for dep_date in check_dates:
            ret_date = dep_date + timedelta(days=route.target_duration_days)
            result = await self.safe_search(route, dep_date, ret_date)
            if result.success:
                all_snapshots.extend(result.snapshots)
            # Pausa entre buscas para não triggerar bloqueios
            await asyncio.sleep(HUMAN_PAUSE_MAX * 2)

        return SearchResult(
            route_id=route.id,
            snapshots=all_snapshots,
            source=ScraperSource.PLAYWRIGHT,
            success=len(all_snapshots) > 0,
            error_message=None if all_snapshots else "Nenhum preço encontrado no mês",
        )
