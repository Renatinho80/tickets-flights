"""flight_analyst/infra/db/repositories/snapshot_repo.py
Repositório de price_snapshots — inserção em massa e consultas analíticas.
Suporta queries temporais necessárias para o motor de inteligência.
"""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog

from flight_analyst.domain.models import PriceSnapshot, ScraperSource, Currency
from flight_analyst.infra.db.supabase_client import DatabaseClient

log = structlog.get_logger(__name__)

TABLE = "price_snapshots"


def _row_to_snapshot(row: dict[str, Any]) -> PriceSnapshot:
    raw = row.get("raw_data")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = None

    return PriceSnapshot(
        id=UUID(str(row["id"])),
        route_id=UUID(str(row["route_id"])),
        scraped_at=row["scraped_at"],
        departure_date=row["departure_date"],
        return_date=row.get("return_date"),
        price=Decimal(str(row["price"])),
        currency=Currency(row["currency"]),
        airline=row.get("airline"),
        stops=row.get("stops", 0),
        source=ScraperSource(row["source"]),
        raw_data=raw,
    )


def _snapshot_to_dict(snapshot: PriceSnapshot) -> dict[str, Any]:
    return {
        "id": str(snapshot.id),
        "route_id": str(snapshot.route_id),
        "scraped_at": snapshot.scraped_at.isoformat(),
        "departure_date": snapshot.departure_date.isoformat(),
        "return_date": snapshot.return_date.isoformat() if snapshot.return_date else None,
        "price": float(snapshot.price),
        "currency": snapshot.currency.value,
        "airline": snapshot.airline,
        "stops": snapshot.stops,
        "source": snapshot.source.value,
        "raw_data": json.dumps(snapshot.raw_data) if snapshot.raw_data else None,
        "advance_days": snapshot.advance_days,
        "duration_days": snapshot.duration_days,
    }


class SnapshotRepository:
    """Operações de escrita e consulta em price_snapshots."""

    def __init__(self, db: DatabaseClient) -> None:
        self._db = db

    async def save(self, snapshot: PriceSnapshot) -> PriceSnapshot:
        """Persiste um único snapshot."""
        data = _snapshot_to_dict(snapshot)
        await self._db.execute(TABLE, "insert", data=data)
        log.debug(
            "snapshot_saved",
            route_id=str(snapshot.route_id),
            price=float(snapshot.price),
            source=snapshot.source.value,
        )
        return snapshot

    async def save_bulk(self, snapshots: list[PriceSnapshot]) -> int:
        """Persiste múltiplos snapshots de uma vez. Retorna quantidade salva."""
        if not snapshots:
            return 0
        data = [_snapshot_to_dict(s) for s in snapshots]
        await self._db.execute(TABLE, "insert", data=data)
        log.info(
            "snapshots_bulk_saved",
            count=len(snapshots),
            route_id=str(snapshots[0].route_id) if snapshots else None,
        )
        return len(snapshots)

    async def get_latest(self, route_id: UUID, limit: int = 10) -> list[PriceSnapshot]:
        """Retorna os N snapshots mais recentes de uma rota."""
        rows = await self._db.execute(
            TABLE, "select",
            filters={"route_id": str(route_id)},
            order="scraped_at",
            desc=True,
            limit=limit,
        )
        return [_row_to_snapshot(r) for r in rows]

    async def get_last_price(self, route_id: UUID) -> PriceSnapshot | None:
        """Retorna o snapshot mais recente."""
        snapshots = await self.get_latest(route_id, limit=1)
        return snapshots[0] if snapshots else None

    async def get_by_date_range(
        self,
        route_id: UUID,
        start_date: date,
        end_date: date,
    ) -> list[PriceSnapshot]:
        """
        Retorna snapshots com departure_date dentro de um período.
        Usado para análise de calendário de preços.
        """
        # Para Supabase, passaremos por filtros custom no backend
        # Esta implementação simplificada usa filtros básicos
        rows = await self._db.execute(
            TABLE, "select",
            filters={"route_id": str(route_id)},
            order="departure_date",
        )
        return [
            _row_to_snapshot(r)
            for r in rows
            if start_date <= date.fromisoformat(str(r["departure_date"])) <= end_date
        ]

    async def get_recent(self, route_id: UUID, days: int = 30) -> list[PriceSnapshot]:
        """
        Retorna snapshots capturados nos últimos N dias.
        Usado para cálculo de Z-score 30d.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = await self._db.execute(
            TABLE, "select",
            filters={"route_id": str(route_id)},
            order="scraped_at",
            desc=True,
        )
        return [
            _row_to_snapshot(r)
            for r in rows
            if datetime.fromisoformat(str(r["scraped_at"])) >= cutoff
        ]

    async def count(self, route_id: UUID) -> int:
        """Retorna o total de snapshots disponíveis para uma rota."""
        rows = await self._db.execute(
            TABLE, "select",
            columns="id",
            filters={"route_id": str(route_id)},
        )
        return len(rows)

    async def get_prices_for_month(
        self,
        route_id: UUID,
        year: int,
        month: int,
    ) -> list[PriceSnapshot]:
        """Retorna todos os snapshots com partida em um mês específico."""
        start = date(year, month, 1)
        # Último dia do mês
        if month == 12:
            end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)
        return await self.get_by_date_range(route_id, start, end)

    async def get_prices_by_advance(
        self,
        route_id: UUID,
        advance_days: int,
        tolerance: int = 3,
    ) -> list[PriceSnapshot]:
        """
        Retorna snapshots capturados com uma antecedência específica (±tolerance dias).
        Usado para análise da janela ideal de compra.
        """
        rows = await self._db.execute(
            TABLE, "select",
            filters={"route_id": str(route_id)},
            order="scraped_at",
            desc=True,
        )
        result = []
        for r in rows:
            adv = r.get("advance_days")
            if adv is not None and abs(int(adv) - advance_days) <= tolerance:
                result.append(_row_to_snapshot(r))
        return result

    async def delete_older_than(self, days: int) -> int:
        """
        Apaga snapshots mais antigos que 'days' dias da base de dados.
        Retorna a quantidade aproximada de registros apagados (para logs).
        Nota: Em Supabase, a melhor prática é rodar via RPC. Aqui implementamos via client simples.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()
        
        # Como o PostgREST nativamente não tem delete() por < (less than) simples via python SDK do supabase-client puro
        # E nosso wrapper só tem delete com eq(), faremos a query direta via exec ou contornamos.
        # Devido às limitações do nosso wrapper genérico, pegamos os IDs e apagamos em lote (ineficiente para milhares), 
        # ou ajustamos o DB client se suportar.
        
        # Para ficar independente, vamos buscar os velhos e apagar um por um, 
        # mas como são muitos, em produção real faríamos via function no Supabase.
        # Aqui vamos fazer um select na memória (apenas ID para não pesar)
        rows = await self._db.execute(TABLE, "select", columns="id, scraped_at")
        
        ids_to_delete = []
        for r in rows:
            if r["scraped_at"] < cutoff_iso:
                ids_to_delete.append(r["id"])
                
        deleted = 0
        for snapshot_id in ids_to_delete:
            await self._db.execute(TABLE, "delete", filters={"id": snapshot_id})
            deleted += 1
            
        return deleted
