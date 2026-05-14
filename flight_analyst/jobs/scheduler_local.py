"""flight_analyst/jobs/scheduler_local.py
Agendador local usando APScheduler.
Ideal para rodar na máquina de desenvolvimento.
"""

import asyncio
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from flight_analyst.infra.db.supabase_client import db
from flight_analyst.jobs.routines import poll_prices_routine

log = structlog.get_logger(__name__)


async def start_local_worker() -> None:
    """Inicia o worker do APScheduler."""
    await db.connect()
    
    scheduler = AsyncIOScheduler()
    
    # Roda a rotina imediatamente ao iniciar
    scheduler.add_job(poll_prices_routine, args=[db], id="startup_poll")
    
    # E depois a cada 1 hora
    scheduler.add_job(
        poll_prices_routine,
        "interval",
        hours=1,
        args=[db],
        id="hourly_poll",
        replace_existing=True
    )
    
    # Limpeza de dados antigos roda 1x por dia (meia-noite)
    from flight_analyst.jobs.routines import cleanup_old_data_routine
    scheduler.add_job(
        cleanup_old_data_routine,
        "cron",
        hour=0,
        minute=0,
        id="daily_cleanup",
        replace_existing=True
    )
    
    scheduler.start()
    log.info("apscheduler_started", interval="1 hour", cleanup="00:00")
    
    try:
        # Mantém o processo rodando
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        log.info("apscheduler_stopping")
        scheduler.shutdown()
