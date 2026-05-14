"""flight_analyst/api/task_manager.py
Gerenciador de tarefas assíncronas em memória — thread-safe com auto-limpeza.

Responsável por rastrear o ciclo de vida de tarefas de coleta de preços
disparadas pelo Dashboard, permitindo polling de status sem bloquear a API.

Design decisions:
  - In-memory dict (ideal para single-instance). Para multi-instance, trocar por Redis
    sem alterar a interface pública.
  - TTL de 10 minutos: tarefas concluídas são removidas automaticamente para evitar
    vazamento de memória em processos de longa duração.
  - Lock assíncrono (asyncio.Lock) garante consistência em operações concorrentes
    dentro do mesmo event loop.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

TASK_TTL_MINUTES = 10           # Tempo de vida de uma task concluída na memória
CLEANUP_INTERVAL_SECONDS = 60   # Frequência da rotina de limpeza automática


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """Estados possíveis de uma tarefa de coleta."""
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TaskInfo(BaseModel):
    """Representação completa de uma tarefa de coleta."""
    task_id: UUID = Field(default_factory=uuid4)
    route_id: UUID
    status: TaskStatus = TaskStatus.RUNNING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    # Resultado (preenchido quando status != RUNNING)
    source: str | None = None
    snapshots_count: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None


# ---------------------------------------------------------------------------
# TaskManager
# ---------------------------------------------------------------------------


class TaskManager:
    """
    Gerenciador thread-safe de tarefas assíncronas.

    Uso:
        manager = TaskManager()
        task = manager.create(route_id)
        # ... executa coleta em background ...
        manager.mark_done(task.task_id, source="playwright", snapshots_count=12)
    """

    def __init__(self) -> None:
        self._tasks: dict[UUID, TaskInfo] = {}
        self._route_locks: dict[UUID, UUID] = {}   # route_id → task_id ativo
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None

    # --- Lifecycle ---

    async def start(self) -> None:
        """Inicia a rotina de limpeza periódica. Chamar no lifespan da API."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        log.info("task_manager_started")

    async def stop(self) -> None:
        """Encerra a rotina de limpeza. Chamar no shutdown da API."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        log.info("task_manager_stopped")

    # --- Operações públicas ---

    async def create(self, route_id: UUID) -> TaskInfo:
        """
        Registra uma nova tarefa para a rota.

        Se já existir uma tarefa RUNNING para esta rota, retorna a existente
        (proteção anti-spam contra cliques duplicados).
        """
        async with self._lock:
            # Proteção anti-spam: reutiliza task ativa
            existing_task_id = self._route_locks.get(route_id)
            if existing_task_id and existing_task_id in self._tasks:
                existing = self._tasks[existing_task_id]
                if existing.status == TaskStatus.RUNNING:
                    log.info("task_reused", route_id=str(route_id), task_id=str(existing.task_id))
                    return existing

            task = TaskInfo(route_id=route_id)
            self._tasks[task.task_id] = task
            self._route_locks[route_id] = task.task_id

            log.info("task_created", task_id=str(task.task_id), route_id=str(route_id))
            return task

    async def get(self, task_id: UUID) -> TaskInfo | None:
        """Consulta o estado de uma tarefa pelo ID."""
        async with self._lock:
            return self._tasks.get(task_id)

    async def mark_done(
        self,
        task_id: UUID,
        *,
        source: str,
        snapshots_count: int,
        duration_seconds: float = 0.0,
    ) -> None:
        """Marca uma tarefa como concluída com sucesso."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                log.warning("task_not_found_for_completion", task_id=str(task_id))
                return

            task.status = TaskStatus.DONE
            task.completed_at = datetime.now(timezone.utc)
            task.source = source
            task.snapshots_count = snapshots_count
            task.duration_seconds = duration_seconds

            # Libera o lock da rota para permitir novas coletas
            self._route_locks.pop(task.route_id, None)
            log.info(
                "task_completed",
                task_id=str(task_id),
                source=source,
                snapshots=snapshots_count,
            )

    async def mark_failed(self, task_id: UUID, *, error: str) -> None:
        """Marca uma tarefa como falha."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                log.warning("task_not_found_for_failure", task_id=str(task_id))
                return

            task.status = TaskStatus.FAILED
            task.completed_at = datetime.now(timezone.utc)
            task.error_message = error

            self._route_locks.pop(task.route_id, None)
            log.error("task_failed", task_id=str(task_id), error=error)

    # --- Limpeza automática ---

    async def _cleanup_loop(self) -> None:
        """Remove tarefas concluídas que ultrapassaram o TTL."""
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                await self._purge_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Nunca deixa o cleanup crashar o processo inteiro
                log.error("task_cleanup_error", error=str(e))

    async def _purge_expired(self) -> None:
        """Remove tasks expiradas da memória."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=TASK_TTL_MINUTES)
        async with self._lock:
            expired_ids = [
                tid for tid, task in self._tasks.items()
                if task.status != TaskStatus.RUNNING
                and task.completed_at
                and task.completed_at < cutoff
            ]
            for tid in expired_ids:
                del self._tasks[tid]

            if expired_ids:
                log.info("tasks_purged", count=len(expired_ids))


# Singleton global — importado pelo main.py da API
task_manager = TaskManager()
"""Instância global do gerenciador de tarefas. Inicializada no lifespan da API."""
