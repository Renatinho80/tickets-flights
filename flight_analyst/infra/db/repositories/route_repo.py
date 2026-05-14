"""flight_analyst/infra/db/repositories/route_repo.py
Repositório de rotas — CRUD completo usando o DatabaseClient.
Abstrai o backend (Supabase ou SQLite) do resto da aplicação.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from flight_analyst.domain.models import Route, RouteCreate
from flight_analyst.infra.db.supabase_client import DatabaseClient

log = structlog.get_logger(__name__)

TABLE = "routes"


def _row_to_route(row: dict[str, Any]) -> Route:
    """Converte um dict do banco em um modelo Route."""
    return Route(
        id=UUID(str(row["id"])),
        origin=row["origin"],
        destination=row["destination"],
        travel_month=row["travel_month"],
        target_duration_days=row["target_duration_days"],
        flexibility_days=row["flexibility_days"],
        max_stops=row["max_stops"],
        currency=row["currency"],
        poll_interval_minutes=row["poll_interval_minutes"],
        aggressive_poll_minutes=row["aggressive_poll_minutes"],
        is_active=bool(row["is_active"]),
        consecutive_errors=int(row.get("consecutive_errors", 0)),
        paused_until=datetime.fromisoformat(str(row["paused_until"])) if row.get("paused_until") else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _route_to_dict(route: Route) -> dict[str, Any]:
    """Serializa um Route para persistência no banco."""
    return {
        "id": str(route.id),
        "origin": route.origin,
        "destination": route.destination,
        "travel_month": route.travel_month.isoformat()[:7] + "-01",  # sempre 1º do mês
        "target_duration_days": route.target_duration_days,
        "flexibility_days": route.flexibility_days,
        "max_stops": route.max_stops,
        "currency": route.currency.value,
        "poll_interval_minutes": route.poll_interval_minutes,
        "aggressive_poll_minutes": route.aggressive_poll_minutes,
        "is_active": route.is_active,
        "consecutive_errors": route.consecutive_errors,
        "paused_until": route.paused_until.isoformat() if route.paused_until else None,
        "created_at": route.created_at.isoformat(),
        "updated_at": route.updated_at.isoformat(),
    }


class RouteRepository:
    """CRUD de rotas monitoradas."""

    def __init__(self, db: DatabaseClient) -> None:
        self._db = db

    async def create(self, route_create: RouteCreate) -> Route:
        """Cria uma nova rota e persiste no banco."""
        route = route_create.to_route()
        data = _route_to_dict(route)
        await self._db.execute(TABLE, "insert", data=data)
        log.info("route_created", route=route.label, id=str(route.id))
        return route

    async def get_by_id(self, route_id: UUID) -> Route | None:
        """Busca uma rota pelo ID."""
        rows = await self._db.execute(
            TABLE, "select",
            filters={"id": str(route_id)},
            limit=1,
        )
        if not rows:
            return None
        return _row_to_route(rows[0])

    async def get_all_active(self) -> list[Route]:
        """Retorna todas as rotas ativas que não estão pausadas pelo circuit breaker."""
        rows = await self._db.execute(
            TABLE, "select",
            filters={"is_active": True},
            order="created_at",
            desc=False,
        )
        
        # Filtra na memória rotas pausadas, pois Supabase/SQLite tem sintaxes diferentes para datas nulas
        active_routes = []
        now = datetime.utcnow()
        for row in rows:
            route = _row_to_route(row)
            if not route.paused_until or route.paused_until.replace(tzinfo=None) <= now:
                active_routes.append(route)
                
        return active_routes

    async def get_all(self) -> list[Route]:
        """Retorna todas as rotas (ativas e inativas)."""
        rows = await self._db.execute(TABLE, "select", order="created_at")
        return [_row_to_route(r) for r in rows]

    async def update(self, route: Route) -> Route:
        """Atualiza uma rota existente."""
        route.updated_at = datetime.utcnow()
        data = _route_to_dict(route)
        await self._db.execute(
            TABLE, "update",
            data={k: v for k, v in data.items() if k not in ("id", "created_at")},
            filters={"id": str(route.id)},
        )
        log.info("route_updated", route=route.label, id=str(route.id))
        return route

    async def deactivate(self, route_id: UUID) -> bool:
        """Desativa uma rota sem deletá-la."""
        rows = await self._db.execute(TABLE, "select", filters={"id": str(route_id)}, limit=1)
        if not rows:
            return False
        await self._db.execute(
            TABLE, "update",
            data={"is_active": False, "updated_at": datetime.utcnow().isoformat()},
            filters={"id": str(route_id)},
        )
        log.info("route_deactivated", id=str(route_id))
        return True

    async def delete(self, route_id: UUID) -> bool:
        """Remove permanentemente uma rota e todos seus dados."""
        rows = await self._db.execute(TABLE, "select", filters={"id": str(route_id)}, limit=1)
        if not rows:
            return False
        await self._db.execute(TABLE, "delete", filters={"id": str(route_id)})
        log.info("route_deleted", id=str(route_id))
        return True

    async def exists(self, origin: str, destination: str, travel_month: str) -> bool:
        """Verifica se já existe uma rota com essa combinação."""
        rows = await self._db.execute(
            TABLE, "select",
            filters={
                "origin": origin.upper(),
                "destination": destination.upper(),
                "travel_month": travel_month + "-01",
            },
            limit=1,
        )
        return len(rows) > 0

    async def register_success(self, route_id: UUID) -> None:
        """Zera o contador de erros após um scrape bem sucedido."""
        await self._db.execute(
            TABLE, "update",
            data={"consecutive_errors": 0, "paused_until": None, "updated_at": datetime.utcnow().isoformat()},
            filters={"id": str(route_id)},
        )

    async def register_error(self, route_id: UUID) -> None:
        """Incrementa erros. Se bater 3, pausa a rota por 12h."""
        from datetime import timedelta
        
        route = await self.get_by_id(route_id)
        if not route:
            return
            
        new_errors = route.consecutive_errors + 1
        data_update: dict[str, Any] = {
            "consecutive_errors": new_errors,
            "updated_at": datetime.utcnow().isoformat()
        }
        
        if new_errors >= 3:
            paused_until = datetime.utcnow() + timedelta(hours=12)
            data_update["paused_until"] = paused_until.isoformat()
            log.error("route_circuit_breaker_activated", route_id=str(route_id), paused_until=data_update["paused_until"])
            
        await self._db.execute(TABLE, "update", data=data_update, filters={"id": str(route_id)})
