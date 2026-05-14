"""flight_analyst/infra/db/repositories/recommendation_repo.py
Repositório para persistência das recomendações geradas pelo motor de inteligência.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog

from flight_analyst.domain.models import Recommendation
from flight_analyst.infra.db.supabase_client import DatabaseClient

log = structlog.get_logger(__name__)

TABLE = "recommendations"


def _row_to_recommendation(row: dict[str, Any]) -> Recommendation:
    # Converter metadados se for string (sqlite às vezes retorna string)
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}

    return Recommendation(
        id=UUID(str(row["id"])),
        route_id=UUID(str(row["route_id"])),
        generated_at=datetime.fromisoformat(str(row["generated_at"])),
        opportunity_score=float(row["opportunity_score"]),
        current_price=Decimal(str(row["current_price"])) if row.get("current_price") else None,
        avg_price_30d=Decimal(str(row["avg_price_30d"])) if row.get("avg_price_30d") else None,
        avg_price_historical=Decimal(str(row["avg_price_historical"])) if row.get("avg_price_historical") else None,
        yoy_delta_pct=row.get("yoy_delta_pct"),
        trend_slope=row.get("trend_slope"),
        days_to_departure=row.get("days_to_departure"),
        recommendation_text=row.get("recommendation_text"),
        is_error_fare=bool(row.get("is_error_fare", False)),
        metadata=meta,
    )


def _recommendation_to_dict(rec: Recommendation) -> dict[str, Any]:
    return {
        "id": str(rec.id),
        "route_id": str(rec.route_id),
        "generated_at": rec.generated_at.isoformat(),
        "opportunity_score": rec.opportunity_score,
        "current_price": float(rec.current_price) if rec.current_price else None,
        "avg_price_30d": float(rec.avg_price_30d) if rec.avg_price_30d else None,
        "avg_price_historical": float(rec.avg_price_historical) if rec.avg_price_historical else None,
        "yoy_delta_pct": rec.yoy_delta_pct,
        "trend_slope": rec.trend_slope,
        "days_to_departure": rec.days_to_departure,
        "recommendation_text": rec.recommendation_text,
        "is_error_fare": int(rec.is_error_fare),
        "metadata": json.dumps(rec.metadata or {}),
    }


class RecommendationRepository:
    def __init__(self, db: DatabaseClient) -> None:
        self._db = db

    async def save(self, rec: Recommendation) -> Recommendation:
        """Salva uma nova recomendação no banco."""
        data = _recommendation_to_dict(rec)
        await self._db.execute(TABLE, "insert", data=data)
        log.debug("recommendation_saved", route_id=str(rec.route_id), score=rec.opportunity_score)
        return rec

    async def get_latest(self, route_id: UUID) -> Recommendation | None:
        """Busca a recomendação mais recente para uma rota."""
        rows = await self._db.execute(
            TABLE, "select",
            filters={"route_id": str(route_id)},
            order="generated_at",
            desc=True,
            limit=1,
        )
        if not rows:
            return None
        return _row_to_recommendation(rows[0])
