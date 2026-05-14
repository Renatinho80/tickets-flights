"""flight_analyst/api/main.py
API REST da Fase 2.
Fornece endpoints para o dashboard e integra os serviços de inteligência e IA.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Depends, HTTPException, status, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import structlog

from flight_analyst.config import settings
from flight_analyst.domain.models import Route, Recommendation, RouteCreate, RouteUpdate
from flight_analyst.infra.db.supabase_client import db, get_db, DatabaseClient
from flight_analyst.infra.db.repositories.route_repo import RouteRepository
from flight_analyst.infra.db.repositories.snapshot_repo import SnapshotRepository
from flight_analyst.application.intelligence_service import IntelligenceService
from flight_analyst.application.ask_service import AskService, AskResponse
from flight_analyst.application.monitor_service import MonitorService
from flight_analyst.infra.scrapers.playwright_scraper import PlaywrightScraper
from flight_analyst.infra.scrapers.serpapi_scraper import SerpApiScraper
from flight_analyst.infra.scrapers.amadeus_scraper import AmadeusScraper
from flight_analyst.infra.scrapers.base import BaseScraper
from flight_analyst.api.task_manager import task_manager, TaskStatus

log = structlog.get_logger(__name__)

# Segurança: Header de API Key
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

async def verify_api_key(api_key: str = Depends(api_key_header)):
    if api_key != settings.app_api_key:
        log.warning("api_unauthorized_access", provided_key=api_key[:4] + "***")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate API Key",
        )
    return api_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup na inicialização
    await task_manager.start()
    yield
    # Cleanup no encerramento
    await task_manager.stop()


app = FastAPI(
    title="Flight Analyst API",
    description="API do motor de inteligência de preços de passagens aéreas.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS para permitir chamadas do Dashboard no Streamlit
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Dependências injetadas
async def get_route_repo(db: DatabaseClient = Depends(get_db)) -> RouteRepository:
    return RouteRepository(db)


async def get_snapshot_repo(db: DatabaseClient = Depends(get_db)) -> SnapshotRepository:
    return SnapshotRepository(db)


def get_intelligence_service() -> IntelligenceService:
    return IntelligenceService()


def get_ask_service() -> AskService:
    return AskService()


async def get_monitor_service(
    route_repo: RouteRepository = Depends(get_route_repo),
    snapshot_repo: SnapshotRepository = Depends(get_snapshot_repo),
) -> MonitorService:
    """Dependência para o serviço de monitoramento com scrapers configurados."""
    scrapers: list[BaseScraper] = []
    
    # Ordem de prioridade (Fase 1: Playwright primeiro)
    scrapers.append(PlaywrightScraper())
    
    if settings.has_serpapi:
        scrapers.append(SerpApiScraper(settings.serpapi_key))
        
    if settings.has_amadeus:
        scrapers.append(AmadeusScraper(
            client_id=settings.amadeus_client_id,
            client_secret=settings.amadeus_client_secret,
            base_url=settings.amadeus_base_url,
        ))
        
    return MonitorService(scrapers, route_repo, snapshot_repo)


# ---------------------------------------------------------------------------
# Models de Request/Response
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env.value}


@app.get("/routes", response_model=list[Route], dependencies=[Depends(verify_api_key)])
async def list_routes(
    repo: RouteRepository = Depends(get_route_repo),
) -> Any:
    """Retorna todas as rotas ativas."""
    return await repo.get_all_active()


@app.post("/routes", response_model=Route, status_code=status.HTTP_201_CREATED, dependencies=[Depends(verify_api_key)])
async def create_route(
    route_data: RouteCreate,
    repo: RouteRepository = Depends(get_route_repo),
) -> Any:
    """Cria uma nova rota para monitoramento."""
    try:
        travel_month_str = route_data.travel_month.strftime("%Y-%m")
        exists = await repo.exists(route_data.origin, route_data.destination, travel_month_str)
        if exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Rota {route_data.origin}→{route_data.destination} para {travel_month_str} já existe."
            )
        
        route = await repo.create(route_data)
        return route
    except HTTPException:
        raise
    except Exception as e:
        log.error("error_creating_route", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Erro interno ao criar rota: {str(e)}")


@app.patch("/routes/{route_id}", response_model=Route, dependencies=[Depends(verify_api_key)])
async def update_route(
    route_id: UUID,
    route_update: RouteUpdate,
    repo: RouteRepository = Depends(get_route_repo),
) -> Any:
    """Atualiza os parâmetros de uma rota existente."""
    try:
        route = await repo.get_by_id(route_id)
        if not route:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rota não encontrada")

        if route_update.target_duration_days is not None:
            route.target_duration_days = route_update.target_duration_days
        if route_update.flexibility_days is not None:
            route.flexibility_days = route_update.flexibility_days
        if route_update.max_stops is not None:
            route.max_stops = route_update.max_stops
        if route_update.poll_interval_minutes is not None:
            route.poll_interval_minutes = route_update.poll_interval_minutes
        if route_update.is_active is not None:
            route.is_active = route_update.is_active

        updated_route = await repo.update(route)
        return updated_route
    except HTTPException:
        raise
    except Exception as e:
        log.error("error_updating_route", error=str(e), route_id=str(route_id))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Erro ao atualizar rota: {str(e)}")


@app.delete("/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response, dependencies=[Depends(verify_api_key)])
async def delete_route(
    route_id: UUID,
    repo: RouteRepository = Depends(get_route_repo),
):
    """Remove permanentemente uma rota."""
    try:
        route = await repo.get_by_id(route_id)
        if not route:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rota não encontrada")
            
        success = await repo.delete(route_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Falha ao deletar rota do banco de dados")
            
        return None
    except HTTPException:
        raise
    except Exception as e:
        log.error("error_deleting_route", error=str(e), route_id=str(route_id))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Erro ao deletar rota: {str(e)}")


@app.post("/routes/{route_id}/collect", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(verify_api_key)])
async def collect_route_prices(
    route_id: UUID,
    route_repo: RouteRepository = Depends(get_route_repo),
) -> Any:
    """
    Dispara uma coleta de preços em background.
    Retorna imediatamente com um task_id para polling de status.
    """
    try:
        route = await route_repo.get_by_id(route_id)
        if not route:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rota não encontrada")

        task = await task_manager.create(route_id)

        # Se a task já estava em andamento (anti-spam), retorna ela sem disparar novamente
        if task.status == TaskStatus.RUNNING and task.snapshots_count == 0:
            # Dispara a coleta em background apenas para tasks novas
            asyncio.create_task(
                _run_background_collection(task.task_id, route)
            )

        return {
            "task_id": str(task.task_id),
            "status": task.status.value,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error("collect_dispatch_error", error=str(e), route_id=str(route_id))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro ao iniciar coleta: {str(e)}"
        )


@app.post("/collect-all", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(verify_api_key)])
async def collect_all_prices(
    route_repo: RouteRepository = Depends(get_route_repo),
) -> Any:
    """
    Dispara a coleta para TODAS as rotas ativas em background.
    """
    try:
        routes = await route_repo.get_all_active()
        tasks_info = []
        
        for route in routes:
            task = await task_manager.create(route.id)
            if task.status == TaskStatus.RUNNING and task.snapshots_count == 0:
                asyncio.create_task(_run_background_collection(task.task_id, route))
            tasks_info.append({"route_id": str(route.id), "task_id": str(task.task_id)})
            
        return {
            "status": "accepted",
            "message": f"Coleta iniciada para {len(routes)} rotas.",
            "tasks": tasks_info
        }
    except Exception as e:
        log.error("collect_all_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro ao iniciar coleta global: {str(e)}"
        )


@app.get("/tasks/{task_id}", dependencies=[Depends(verify_api_key)])
async def get_task_status(task_id: UUID) -> Any:
    """Consulta o status de uma tarefa de coleta."""
    task = await task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tarefa não encontrada ou expirada")

    return {
        "task_id": str(task.task_id),
        "route_id": str(task.route_id),
        "status": task.status.value,
        "created_at": task.created_at.isoformat(),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "source": task.source,
        "snapshots_count": task.snapshots_count,
        "duration_seconds": task.duration_seconds,
        "error_message": task.error_message,
    }


async def _run_background_collection(task_id: UUID, route: Route) -> None:
    """
    Executa a coleta de preços em background.
    Isolada do request lifecycle — nunca propaga exceções para o caller.
    """
    import time
    start = time.monotonic()

    try:
        # Garantir conexão com o banco (singleton db)
        if not db._backend:
            await db.connect()
        route_repo = RouteRepository(db)
        snapshot_repo = SnapshotRepository(db)

        scrapers: list[BaseScraper] = [PlaywrightScraper()]
        if settings.has_serpapi:
            scrapers.append(SerpApiScraper(settings.serpapi_key))
        if settings.has_amadeus:
            scrapers.append(AmadeusScraper(
                client_id=settings.amadeus_client_id,
                client_secret=settings.amadeus_client_secret,
                base_url=settings.amadeus_base_url,
            ))

        monitor = MonitorService(scrapers, route_repo, snapshot_repo)

        log.info("background_collect_started", route=route.label, task_id=str(task_id))
        result = await monitor.collect_for_route(route)
        elapsed = time.monotonic() - start

        if result and result.success and result.snapshots:
            await task_manager.mark_done(
                task_id,
                source=result.source.value,
                snapshots_count=len(result.snapshots),
                duration_seconds=round(elapsed, 2),
            )
        else:
            await task_manager.mark_failed(
                task_id,
                error="Nenhum scraper obteve resultados para esta rota."
            )
    except Exception as e:
        elapsed = time.monotonic() - start
        log.error(
            "background_collect_crash",
            task_id=str(task_id),
            error=str(e),
            duration=round(elapsed, 2),
        )
        # Garante que a task NUNCA fica presa em "running" eternamente
        try:
            await task_manager.mark_failed(task_id, error=str(e))
        except Exception:
            pass  # Última linha de defesa — log já foi emitido acima


@app.get("/routes/{route_id}/snapshots", response_model=list[dict], dependencies=[Depends(verify_api_key)])
async def get_route_snapshots(
    route_id: UUID,
    snapshot_repo: SnapshotRepository = Depends(get_snapshot_repo),
) -> Any:
    """Retorna os últimos 100 snapshots de uma rota para exibição no gráfico."""
    snapshots = await snapshot_repo.get_latest(route_id, limit=100)
    # Convertendo para dicionário simples pro Streamlit consumir facilmente
    return [{
        "scraped_at": s.scraped_at.isoformat(),
        "price": float(s.price),
        "airline": s.airline
    } for s in snapshots]


@app.get("/routes/{route_id}/recommendations", response_model=Recommendation, dependencies=[Depends(verify_api_key)])
async def get_route_recommendation(
    route_id: UUID,
    route_repo: RouteRepository = Depends(get_route_repo),
    snapshot_repo: SnapshotRepository = Depends(get_snapshot_repo),
    intelligence_service: IntelligenceService = Depends(get_intelligence_service),
) -> Any:
    """
    Calcula e retorna a recomendação em tempo real para uma rota.
    (Em produção, isso seria pré-calculado por um worker e lido da tabela).
    """
    route = await route_repo.get_by_id(route_id)
    if not route:
        raise HTTPException(status_code=404, detail="Rota não encontrada")

    snapshots = await snapshot_repo.get_recent(route_id, days=30)
    if not snapshots:
        raise HTTPException(status_code=404, detail="Nenhum dado recente encontrado para gerar recomendação")

    # TODO: Integrar com repositório de histórico longo (HistoricalStatsRepo)
    recommendation = intelligence_service.generate_recommendation(route, snapshots)
    
    if not recommendation:
        raise HTTPException(status_code=500, detail="Falha ao gerar recomendação")
        
    return recommendation


@app.post("/routes/{route_id}/ask", response_model=AskResponse, dependencies=[Depends(verify_api_key)])
async def ask_about_route(
    route_id: UUID,
    request: AskRequest,
    route_repo: RouteRepository = Depends(get_route_repo),
    snapshot_repo: SnapshotRepository = Depends(get_snapshot_repo),
    intelligence_service: IntelligenceService = Depends(get_intelligence_service),
    ask_service: AskService = Depends(get_ask_service),
) -> Any:
    """
    Pergunta ao Gemini sobre os preços da rota.
    Injeta o contexto de mercado antes de enviar a pergunta.
    """
    if not ask_service.is_ready:
        raise HTTPException(status_code=503, detail="Módulo Ask (Gemini) desativado.")

    route = await route_repo.get_by_id(route_id)
    if not route:
        raise HTTPException(status_code=404, detail="Rota não encontrada")

    # Obtém recomendação mais recente para dar contexto à IA, mas os snapshots agora são consultados sob demanda
    recommendation = None
    snapshots = await snapshot_repo.get_recent(route_id, days=1)
    if snapshots:
        recommendation = intelligence_service.generate_recommendation(route, snapshots)

    response = await ask_service.ask(request.question, route, recommendation)
    return response


# Rota do Inngest (Apenas se configurado)
try:
    import inngest.fast_api
    from flight_analyst.jobs.inngest_client import inngest_client, poll_prices_cron, cleanup_old_data_cron
    
    # Só servimos o Inngest se houver uma chave de assinatura ou se estivermos em desenvolvimento
    if settings.inngest_signing_key or not settings.is_production:
        inngest.fast_api.serve(app, inngest_client, [poll_prices_cron, cleanup_old_data_cron])
    else:
        log.warning("inngest_skipped", reason="INNGEST_SIGNING_KEY ausente em produção")
except ImportError:
    log.warning("inngest_skipped", reason="Pacote inngest não instalado")
except Exception as e:
    log.warning("inngest_init_error", error=str(e))

# Para rodar via CLI
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("flight_analyst.api.main:app", host="127.0.0.1", port=settings.api_port, reload=True)
