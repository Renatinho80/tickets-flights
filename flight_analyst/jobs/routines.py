"""flight_analyst/jobs/routines.py
Agrupa as rotinas que serão chamadas por qualquer agendador (Inngest, APScheduler, etc).
"""

import asyncio
from datetime import datetime, timezone
import structlog

from flight_analyst.domain.models import Route
from flight_analyst.infra.db.supabase_client import DatabaseClient
from flight_analyst.infra.db.repositories.route_repo import RouteRepository
from flight_analyst.infra.db.repositories.snapshot_repo import SnapshotRepository
from flight_analyst.infra.db.repositories.alert_repo import AlertRepository
from flight_analyst.infra.db.repositories.recommendation_repo import RecommendationRepository
from flight_analyst.infra.scrapers.playwright_scraper import PlaywrightScraper
from flight_analyst.infra.scrapers.serpapi_scraper import SerpApiScraper
from flight_analyst.application.monitor_service import MonitorService
from flight_analyst.application.intelligence_service import IntelligenceService
from flight_analyst.application.notification_service import NotificationService
from flight_analyst.infra.scrapers.base import BaseScraper
from flight_analyst.config import settings

log = structlog.get_logger(__name__)


async def poll_prices_routine(db: DatabaseClient) -> None:
    """
    1. Busca rotas ativas.
    2. Coleta preços de cada uma.
    3. Roda a inteligência.
    4. Envia notificação se Opportunity Score for alto e não tiver alertado recentemente.
    """
    log.info("poll_prices_routine_start")
    
    # Inicia dependências
    route_repo = RouteRepository(db)
    snapshot_repo = SnapshotRepository(db)
    alert_repo = AlertRepository(db)
    recommendation_repo = RecommendationRepository(db)
    
    scrapers: list[BaseScraper] = [PlaywrightScraper()]
    if settings.has_serpapi:
        scrapers.append(SerpApiScraper(settings.serpapi_key))
        
    monitor = MonitorService(scrapers, route_repo, snapshot_repo)
    intelligence = IntelligenceService()
    notifier = NotificationService()
    
    routes = await route_repo.get_all_active()
    if not routes:
        log.info("poll_prices_routine_skip_no_routes")
        return

    # Semáforo: no máximo 3 coletas simultâneas para não estourar a RAM (512MB no Render)
    semaphore = asyncio.Semaphore(3)

    async def _process_route(route: Route) -> None:
        async with semaphore:
            try:
                # Filtro Inteligente: Verifica se já deu o tempo do intervalo definido no Dashboard
                agora = datetime.now(timezone.utc)
                minutos_passados = (agora - route.updated_at).total_seconds() / 60
                
                # Se o tempo passado for menor que o intervalo (com margem de 1 min para evitar delays de rede)
                if minutos_passados < (route.poll_interval_minutes - 1):
                    log.info("routine_skip_route_cooldown", route=route.label, remaining=round(route.poll_interval_minutes - minutos_passados))
                    return

                # Feature 4: Alerta de Falha Consecutiva
                if route.consecutive_errors >= 3:
                    log.warning("routine_route_has_many_errors", route=route.label, errors=route.consecutive_errors)
                    await notifier.send_test_message(
                        f"⚠️ <b>Atenção!</b> A rota <b>{route.label}</b> falhou "
                        f"{route.consecutive_errors}x consecutivas. Verifique os scrapers."
                    )

                log.info("routine_checking_route", route=route.label)
                collect_result = await monitor.collect_for_route(route)

                if not collect_result or not collect_result.success:
                    log.warning("routine_collect_failed", route=route.label)
                    await route_repo.register_error(route.id)
                    return

                await route_repo.register_success(route.id)

                snapshots = await snapshot_repo.get_recent(route.id, days=30)
                if not snapshots:
                    return

                rec = intelligence.generate_recommendation(route, snapshots)
                if not rec:
                    return

                await recommendation_repo.save(rec)

                if rec.opportunity_score >= 80 or rec.is_error_fare:
                    has_recent = await alert_repo.has_recent_alert_for_route(route.id, hours=24)
                    if not has_recent:
                        log.info("routine_sending_alert", route=route.label, score=rec.opportunity_score)

                        from flight_analyst.domain.models import Alert, AlertType

                        alert = Alert(
                            route_id=route.id,
                            alert_type=AlertType.ERROR_FARE if rec.is_error_fare else AlertType.PRICE_DROP,
                            opportunity_score=rec.opportunity_score,
                            current_price=rec.current_price,
                            message=rec.recommendation_text or "Nova oportunidade de preço encontrada!",
                        )

                        sent = await notifier.notify_opportunity(route, rec)
                        telegram_sent = bool(notifier._telegram_bot and sent)
                        ntfy_sent = bool(settings.ntfy_topic and sent)
                        alert.sent_via = (["telegram"] if telegram_sent else []) + (["ntfy"] if ntfy_sent else [])
                        alert.is_sent = sent

                        await alert_repo.save(alert)
                    else:
                        log.info("routine_skipping_alert_cooldown", route=route.label)
            except Exception as exc:
                log.error("routine_error_on_route", route=route.label, error=str(exc))
                await route_repo.register_error(route.id)

    # Executa todas as rotas em paralelo (respeitando o semáforo)
    await asyncio.gather(*[_process_route(route) for route in routes])
            
    log.info("poll_prices_routine_done")

async def cleanup_old_data_routine() -> None:
    """
    Remove snapshots antigos (mais velhos que 180 dias) para economizar
    armazenamento no banco de dados e evitar lentidão.
    """
    log.info("cleanup_old_data_routine_start")
    
    from flight_analyst.infra.db.supabase_client import db
    from flight_analyst.infra.db.repositories.snapshot_repo import SnapshotRepository
    
    await db.connect()
    snapshot_repo = SnapshotRepository(db)
    
    try:
        deleted = await snapshot_repo.delete_older_than(days=180)
        log.info("cleanup_old_data_routine_done", deleted=deleted)
    except Exception as exc:
        log.error("cleanup_old_data_routine_failed", error=str(exc))
