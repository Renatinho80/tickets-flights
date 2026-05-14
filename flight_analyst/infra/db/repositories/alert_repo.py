"""flight_analyst/infra/db/repositories/alert_repo.py
Repositório para controle de alertas enviados.
Evita envio de spam (múltiplos alertas iguais em janela curta).
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import structlog

from flight_analyst.domain.models import Alert
from flight_analyst.infra.db.supabase_client import DatabaseClient

log = structlog.get_logger(__name__)

TABLE = "alerts"


def _row_to_alert(row: dict[str, Any]) -> Alert:
    return Alert(
        id=UUID(str(row["id"])),
        route_id=UUID(str(row["route_id"])),
        alert_type=row["alert_type"],
        triggered_at=datetime.fromisoformat(str(row["triggered_at"])),
        opportunity_score=row.get("opportunity_score"),
        current_price=row.get("current_price"),
        message=row["message"],
        sent_via=row.get("sent_via"),
        is_sent=bool(row.get("is_sent", False)),
    )


def _alert_to_dict(alert: Alert) -> dict[str, Any]:
    return {
        "id": str(alert.id),
        "route_id": str(alert.route_id),
        "alert_type": alert.alert_type.value,
        "triggered_at": alert.triggered_at.isoformat(),
        "opportunity_score": alert.opportunity_score,
        "current_price": float(alert.current_price) if alert.current_price else None,
        "message": alert.message,
        "sent_via": alert.sent_via,
        "is_sent": int(alert.is_sent),
    }


class AlertRepository:
    def __init__(self, db: DatabaseClient) -> None:
        self._db = db

    async def save(self, alert: Alert) -> Alert:
        """Salva um alerta no banco de dados."""
        data = _alert_to_dict(alert)
        await self._db.execute(TABLE, "insert", data=data)
        log.debug("alert_saved", route_id=str(alert.route_id), type=alert.alert_type.value)
        return alert

    async def has_recent_alert_for_route(
        self,
        route_id: UUID,
        hours: int = 24,
    ) -> bool:
        """
        Verifica se um alerta de oportunidade foi enviado nas últimas X horas.
        Serve como regra anti-spam.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        
        # Como o banco sqlite não suporta queries complexas de data bem, 
        # puxamos os recentes e validamos na memória.
        rows = await self._db.execute(
            TABLE, "select",
            filters={"route_id": str(route_id), "is_sent": 1},
            order="triggered_at",
            desc=True,
            limit=5,
        )
        
        for row in rows:
            triggered = datetime.fromisoformat(str(row["triggered_at"]))
            if triggered >= cutoff:
                return True
                
        return False
