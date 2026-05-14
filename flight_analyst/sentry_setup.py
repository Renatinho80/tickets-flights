"""flight_analyst/sentry_setup.py
Inicialização do Sentry SDK para monitoramento de erros em produção.
Captura exceptions com contexto de rota para diagnóstico preciso.
"""

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration

import structlog

log = structlog.get_logger(__name__)


def init_sentry(dsn: str, environment: str = "development", release: str = "0.1.0") -> None:
    """
    Inicializa o Sentry com integrações async e httpx.
    Chame no startup da aplicação (main.py / FastAPI lifespan).
    """
    if not dsn:
        log.info("sentry_disabled", reason="DSN não configurado")
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=f"flight-analyst@{release}",
        traces_sample_rate=0.1,  # 10% de traces de performance
        integrations=[
            AsyncioIntegration(),
            HttpxIntegration(),
        ],
        # Não enviar dados pessoais
        send_default_pii=False,
        # Ignorar erros esperados de rate limit de scrapers
        ignore_errors=[
            "httpx.HTTPStatusError",
            "playwright._impl._errors.TimeoutError",
        ],
    )
    log.info("sentry_initialized", environment=environment)


def capture_scraper_error(
    exc: Exception,
    scraper_source: str,
    route_label: str,
    extra: dict | None = None,
) -> None:
    """
    Captura uma exceção de scraper com contexto de rota.
    Use nos scrapers ao invés de sentry_sdk.capture_exception diretamente.
    """
    with sentry_sdk.push_scope() as scope:
        scope.set_tag("scraper", scraper_source)
        scope.set_tag("route", route_label)
        scope.set_context("scraper_context", {
            "source": scraper_source,
            "route": route_label,
            **(extra or {}),
        })
        sentry_sdk.capture_exception(exc)
