"""flight_analyst/scripts/seed_historical_data.py
Script de seed — popula historical_stats com 6 meses de dados via Amadeus API.
Execute uma vez após criar uma nova rota para ter dados históricos suficientes
para o motor de inteligência funcionar (mínimo: 20 snapshots).

Uso:
    python -m flight_analyst.scripts.seed_historical_data --route-id <UUID>
    python -m flight_analyst.scripts.seed_historical_data --all
"""

import asyncio
from datetime import date, timedelta
from uuid import UUID

import structlog
import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

console = Console()
log = structlog.get_logger(__name__)

app = typer.Typer(help="Seed de dados históricos via Amadeus API.")


async def _seed_route(route_id_str: str, months_back: int = 6) -> None:
    """Popula snapshots históricos para uma rota específica."""
    from flight_analyst.config import settings
    from flight_analyst.infra.db.supabase_client import db
    from flight_analyst.infra.db.repositories.route_repo import RouteRepository
    from flight_analyst.infra.scrapers.amadeus_scraper import AmadeusScraper
    from flight_analyst.application.monitor_service import MonitorService
    from flight_analyst.infra.db.repositories.snapshot_repo import SnapshotRepository

    # Conectar ao banco
    await db.connect()
    route_repo = RouteRepository(db)
    snapshot_repo = SnapshotRepository(db)

    route = await route_repo.get_by_id(UUID(route_id_str))
    if not route:
        console.print(f"[red]Rota não encontrada: {route_id_str}[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold cyan]Seed histórico: {route.label}[/bold cyan]")

    if not settings.has_amadeus:
        console.print("[red]Credenciais Amadeus não configuradas. Configure AMADEUS_CLIENT_ID e AMADEUS_CLIENT_SECRET no .env[/red]")
        raise typer.Exit(1)

    amadeus = AmadeusScraper(
        client_id=settings.amadeus_client_id,
        client_secret=settings.amadeus_client_secret,
        base_url=settings.amadeus_base_url,
    )

    # Verificar saúde
    healthy = await amadeus.health_check()
    if not healthy:
        console.print("[red]Não foi possível autenticar com Amadeus. Verifique as credenciais.[/red]")
        raise typer.Exit(1)

    # Calcular meses a buscar (N meses atrás até hoje)
    today = date.today()
    months_to_seed: list[tuple[int, int]] = []
    for i in range(months_back, 0, -1):
        # i meses atrás
        year = today.year
        month = today.month - i
        while month <= 0:
            month += 12
            year -= 1
        months_to_seed.append((year, month))

    total_saved = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Coletando dados históricos...", total=len(months_to_seed))

        for year, month in months_to_seed:
            progress.update(task, description=f"Buscando {month:02d}/{year}...")

            result = await amadeus.search_month(route, year, month)
            if result.success and result.snapshots:
                saved = await snapshot_repo.save_bulk(result.snapshots)
                total_saved += saved
                log.info("month_seeded", year=year, month=month, count=saved)

            progress.advance(task)
            await asyncio.sleep(1.5)  # Rate limiting

    await amadeus.close()
    console.print(f"\n[green]✓ Seed concluído: {total_saved} snapshots salvos para {route.label}[/green]")


async def _seed_all_routes(months_back: int = 6) -> None:
    """Popula histórico para todas as rotas ativas."""
    from flight_analyst.infra.db.supabase_client import db
    from flight_analyst.infra.db.repositories.route_repo import RouteRepository

    await db.connect()
    route_repo = RouteRepository(db)
    routes = await route_repo.get_all_active()

    if not routes:
        console.print("[yellow]Nenhuma rota ativa encontrada.[/yellow]")
        return

    console.print(f"[bold]Seeding {len(routes)} rota(s)...[/bold]")
    for route in routes:
        await _seed_route(str(route.id), months_back)
        await asyncio.sleep(2.0)


@app.command()
def seed(
    route_id: str = typer.Option(None, "--route-id", "-r", help="UUID da rota a fazer seed"),
    all_routes: bool = typer.Option(False, "--all", "-a", help="Seed de todas as rotas ativas"),
    months_back: int = typer.Option(6, "--months", "-m", help="Quantos meses para trás buscar"),
) -> None:
    """Popula dados históricos de preços via Amadeus API."""
    if not route_id and not all_routes:
        console.print("[red]Especifique --route-id <UUID> ou --all[/red]")
        raise typer.Exit(1)

    if all_routes:
        asyncio.run(_seed_all_routes(months_back))
    else:
        asyncio.run(_seed_route(route_id, months_back))


if __name__ == "__main__":
    app()
