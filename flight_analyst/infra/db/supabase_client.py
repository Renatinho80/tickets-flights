"""flight_analyst/infra/db/supabase_client.py
Cliente de banco de dados com Supabase como primário e SQLite como fallback local.
Em produção (Railway), apenas o Supabase é usado — o SQLite é somente para dev.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import structlog

log = structlog.get_logger(__name__)


class DatabaseUnavailableError(Exception):
    """Levantado quando nenhum backend de BD está disponível."""


class SupabaseBackend:
    """
    Wrapper do cliente Supabase via PostgREST direto.
    Evita a dependência da biblioteca completa `supabase` que puxa `pyiceberg` (e requer C++ no Windows).
    """

    def __init__(self, url: str, key: str) -> None:
        self._url = url.rstrip("/")
        self._key = key
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        from postgrest import AsyncPostgrestClient  # type: ignore[import]

        self._loop = asyncio.get_running_loop()
        rest_url = f"{self._url}/rest/v1"
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
        }
        self._client = AsyncPostgrestClient(rest_url, headers=headers)
        log.info("supabase_connected_via_postgrest", url=rest_url[:40] + "...")

    @property
    def client(self) -> Any:
        if self._client is None:
            raise DatabaseUnavailableError("PostgREST client não inicializado.")
        return self._client

    async def health_check(self) -> bool:
        try:
            await self._client.table("routes").select("id").limit(1).execute()
            return True
        except Exception as exc:
            log.warning("supabase_health_check_failed", error=str(exc))
            return False

    async def execute(self, table: str, operation: str, **kwargs: Any) -> Any:
        """Executa uma operação na tabela especificada."""
        builder = self.client.table(table)

        match operation:
            case "select":
                q = builder.select(kwargs.get("columns", "*"))
                if filters := kwargs.get("filters"):
                    for col, val in filters.items():
                        q = q.eq(col, val)
                if order := kwargs.get("order"):
                    q = q.order(order, desc=kwargs.get("desc", False))
                if limit := kwargs.get("limit"):
                    q = q.limit(limit)
                result = await q.execute()
                return result.data

            case "insert":
                result = await builder.insert(kwargs["data"]).execute()
                return result.data

            case "upsert":
                result = await builder.upsert(
                    kwargs["data"],
                    on_conflict=kwargs.get("on_conflict", "id"),
                ).execute()
                return result.data

            case "update":
                q = builder.update(kwargs["data"])
                if filters := kwargs.get("filters"):
                    for col, val in filters.items():
                        q = q.eq(col, val)
                result = await q.execute()
                return result.data

            case "delete":
                q = builder.delete()
                if filters := kwargs.get("filters"):
                    for col, val in filters.items():
                        q = q.eq(col, val)
                result = await q.execute()
                return result.data

            case _:
                raise ValueError(f"Operação desconhecida: {operation}")


class SQLiteBackend:
    """
    Backend SQLite para desenvolvimento local.
    Implementa a mesma interface do SupabaseBackend.
    NÃO usar em produção — o disco é efêmero em Railway/Render.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        import aiosqlite

        self._loop = asyncio.get_running_loop()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()
        log.info("sqlite_connected", path=str(self._db_path))

    async def _create_tables(self) -> None:
        """Cria tabelas SQLite com schema simplificado (sem features PG)."""
        schema = """
        CREATE TABLE IF NOT EXISTS routes (
            id TEXT PRIMARY KEY,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            travel_month TEXT NOT NULL,
            target_duration_days INTEGER NOT NULL DEFAULT 14,
            flexibility_days INTEGER NOT NULL DEFAULT 3,
            max_stops INTEGER NOT NULL DEFAULT 1,
            currency TEXT NOT NULL DEFAULT 'BRL',
            poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
            aggressive_poll_minutes INTEGER NOT NULL DEFAULT 15,
            is_active INTEGER NOT NULL DEFAULT 1,
            consecutive_errors INTEGER NOT NULL DEFAULT 0,
            paused_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id TEXT PRIMARY KEY,
            route_id TEXT NOT NULL REFERENCES routes(id),
            scraped_at TEXT NOT NULL,
            departure_date TEXT NOT NULL,
            return_date TEXT,
            duration_days INTEGER,
            price REAL NOT NULL,
            currency TEXT NOT NULL,
            airline TEXT,
            stops INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL,
            raw_data TEXT,
            advance_days INTEGER
        );
        CREATE TABLE IF NOT EXISTS historical_stats (
            id TEXT PRIMARY KEY,
            route_id TEXT NOT NULL REFERENCES routes(id),
            stat_year INTEGER NOT NULL,
            stat_month INTEGER NOT NULL,
            stat_week_of_month INTEGER,
            stat_day_of_week INTEGER,
            duration_days INTEGER,
            advance_days INTEGER,
            price_min REAL,
            price_avg REAL,
            price_max REAL,
            price_p25 REAL,
            price_p75 REAL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            yoy_delta_pct REAL,
            is_high_season INTEGER NOT NULL DEFAULT 0,
            computed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recommendations (
            id TEXT PRIMARY KEY,
            route_id TEXT NOT NULL REFERENCES routes(id),
            generated_at TEXT NOT NULL,
            opportunity_score REAL NOT NULL,
            current_price REAL,
            avg_price_30d REAL,
            avg_price_historical REAL,
            yoy_delta_pct REAL,
            trend_slope REAL,
            days_to_departure INTEGER,
            recommendation_text TEXT,
            is_error_fare INTEGER NOT NULL DEFAULT 0,
            metadata TEXT
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            route_id TEXT NOT NULL REFERENCES routes(id),
            alert_type TEXT NOT NULL,
            triggered_at TEXT NOT NULL,
            opportunity_score REAL,
            current_price REAL,
            message TEXT NOT NULL,
            sent_via TEXT,
            is_sent INTEGER NOT NULL DEFAULT 0
        );
        """
        async with self._conn.executescript(schema):
            pass
        await self._conn.commit()

    async def health_check(self) -> bool:
        try:
            await self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def execute(self, table: str, operation: str, **kwargs: Any) -> Any:
        """Interface compatível com SupabaseBackend para SQLite."""
        match operation:
            case "select":
                cols = kwargs.get("columns", "*")
                query = f"SELECT {cols} FROM {table} WHERE 1=1"
                params: list[Any] = []
                if filters := kwargs.get("filters"):
                    for col, val in filters.items():
                        query += f" AND {col} = ?"
                        params.append(val)
                if order := kwargs.get("order"):
                    desc = "DESC" if kwargs.get("desc") else "ASC"
                    query += f" ORDER BY {order} {desc}"
                if limit := kwargs.get("limit"):
                    query += f" LIMIT {limit}"
                async with self._conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]

            case "insert" | "upsert":
                data = kwargs["data"]
                if isinstance(data, list):
                    for row in data:
                        await self._insert_row(table, row)
                else:
                    await self._insert_row(table, data)
                await self._conn.commit()
                return data

            case "update":
                data = kwargs["data"]
                filters = kwargs.get("filters", {})
                set_clause = ", ".join(f"{k} = ?" for k in data)
                where_clause = " AND ".join(f"{k} = ?" for k in filters)
                params_list = list(data.values()) + list(filters.values())
                await self._conn.execute(
                    f"UPDATE {table} SET {set_clause} WHERE {where_clause}", params_list
                )
                await self._conn.commit()
                return data

            case "delete":
                filters = kwargs.get("filters", {})
                where_clause = " AND ".join(f"{k} = ?" for k in filters)
                await self._conn.execute(
                    f"DELETE FROM {table} WHERE {where_clause}", list(filters.values())
                )
                await self._conn.commit()
                return {}

            case _:
                raise ValueError(f"Operação desconhecida: {operation}")

    async def _insert_row(self, table: str, data: dict[str, Any]) -> None:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        values = [
            json.dumps(v) if isinstance(v, (dict, list)) else v
            for v in data.values()
        ]
        await self._conn.execute(
            f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})", values
        )


class DatabaseClient:
    """
    Cliente de banco de dados com fallback automático.
    Tenta Supabase primeiro; em desenvolvimento local, cai para SQLite.
    """

    def __init__(self) -> None:
        self._backend: SupabaseBackend | SQLiteBackend | None = None
        self._is_supabase: bool = False
        self._connected_loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        from flight_analyst.config import settings

        if settings.has_supabase:
            try:
                backend = SupabaseBackend(settings.supabase_url, settings.supabase_key)
                await backend.connect()
                healthy = await backend.health_check()
                if healthy:
                    self._backend = backend
                    self._is_supabase = True
                    self._connected_loop = asyncio.get_running_loop()
                    log.info("database_backend", backend="supabase")
                    return
            except Exception as exc:
                log.warning("supabase_connection_failed", error=str(exc))

        if settings.is_production:
            raise DatabaseUnavailableError(
                "Supabase indisponível em produção — sem fallback SQLite."
            )

        log.warning("database_backend", backend="sqlite_fallback")
        sqlite_backend = SQLiteBackend(settings.sqlite_path)
        await sqlite_backend.connect()
        self._backend = sqlite_backend
        self._is_supabase = False
        self._connected_loop = asyncio.get_running_loop()

    @property
    def backend(self) -> SupabaseBackend | SQLiteBackend:
        if self._backend is None:
            raise DatabaseUnavailableError("DatabaseClient não conectado. Chame connect() primeiro.")
        return self._backend

    @property
    def using_supabase(self) -> bool:
        return self._is_supabase

    def is_connected(self) -> bool:
        """Retorna True se o cliente já está conectado a um backend."""
        return self._backend is not None

    async def execute(self, table: str, operation: str, **kwargs: Any) -> Any:
        # Se o loop mudou (comum em Streamlit/Threads), reconectamos
        current_loop = asyncio.get_running_loop()
        if self._connected_loop != current_loop:
            log.warning("event_loop_changed", old=str(self._connected_loop), new=str(current_loop))
            await self.connect()
            
        return await self.backend.execute(table, operation, **kwargs)

    async def close(self) -> None:
        if self._backend and isinstance(self._backend, SQLiteBackend):
            if self._backend._conn:
                await self._backend._conn.close()


# Instância singleton do cliente
db = DatabaseClient()


async def get_db() -> AsyncIterator[DatabaseClient]:
    """Context manager para uso em FastAPI (dependency injection)."""
    if not db.is_connected():
        await db.connect()
    try:
        yield db
    finally:
        pass  # Manter conexão aberta entre requests
