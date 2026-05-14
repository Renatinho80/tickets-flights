"""flight_analyst/jobs/inngest_client.py
Configuração do Inngest para agendamento via nuvem (Serverless Cron).
"""

import inngest
import structlog

from flight_analyst.config import settings
from flight_analyst.jobs.routines import poll_prices_routine, cleanup_old_data_routine

log = structlog.get_logger(__name__)

# O cliente do Inngest
inngest_client = inngest.Inngest(
    app_id="flight-analyst",
    event_key=settings.inngest_event_key,
    signing_key=settings.inngest_signing_key,
)

@inngest_client.create_function(
    fn_id="poll-prices-cron",
    name="Monitoramento de Preços Automático",
    trigger=inngest.CronTrigger(cron="0 * * * *"),  # Roda a cada hora
)
async def poll_prices_cron(ctx: inngest.Context, step: inngest.Step) -> dict[str, str]:
    """Rotina principal acionada pelo Inngest (Serverless)."""
    log.info("inngest_poll_prices_started", run_id=ctx.run_id)
    await poll_prices_routine()
    return {"status": "success", "run_id": ctx.run_id}

@inngest_client.create_function(
    fn_id="cleanup-old-data-cron",
    name="Limpeza de Histórico Antigo",
    trigger=inngest.CronTrigger(cron="0 2 * * 0"),  # Roda todo domingo às 02:00
)
async def cleanup_old_data_cron(ctx: inngest.Context, step: inngest.Step) -> dict[str, str]:
    """Rotina de limpeza de dados antigos acionada pelo Inngest."""
    log.info("inngest_cleanup_started", run_id=ctx.run_id)
    await cleanup_old_data_routine()
    return {"status": "success", "run_id": ctx.run_id}
