"""flight_analyst/main.py
Entrypoint CLI do Flight Analyst — Fase 1.
Comandos disponíveis: run, status, add-route, list-routes.
Usa Typer + Rich para interface amigável no terminal.
"""

import asyncio
import sys
from datetime import date
from typing import Annotated
from uuid import UUID

import typer
from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()
app = typer.Typer(
    name="flight-analyst",
    help="✈  Analista pessoal de passagens aéreas",
    add_completion=False,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Helpers de inicialização
# ---------------------------------------------------------------------------


async def _init_db() -> "DatabaseClient":  # type: ignore[name-defined]  # noqa: F821
    from flight_analyst.infra.db.supabase_client import db
    await db.connect()
    return db


async def _build_monitor() -> tuple["MonitorService", "RouteRepository", "SnapshotRepository"]:  # type: ignore[name-defined]  # noqa: F821
    from flight_analyst.config import settings
    from flight_analyst.infra.db.supabase_client import db
    from flight_analyst.infra.db.repositories.route_repo import RouteRepository
    from flight_analyst.infra.db.repositories.snapshot_repo import SnapshotRepository
    from flight_analyst.infra.scrapers.playwright_scraper import PlaywrightScraper
    from flight_analyst.infra.scrapers.serpapi_scraper import SerpApiScraper
    from flight_analyst.infra.scrapers.amadeus_scraper import AmadeusScraper
    from flight_analyst.infra.scrapers.base import BaseScraper
    from flight_analyst.application.monitor_service import MonitorService

    await db.connect()
    route_repo = RouteRepository(db)
    snapshot_repo = SnapshotRepository(db)

    # Montar lista de scrapers na ordem de prioridade
    scrapers: list[BaseScraper] = []

    pw = PlaywrightScraper()
    scrapers.append(pw)

    if settings.has_serpapi:
        scrapers.append(SerpApiScraper(settings.serpapi_key))

    if settings.has_amadeus:
        scrapers.append(AmadeusScraper(
            client_id=settings.amadeus_client_id,
            client_secret=settings.amadeus_client_secret,
            base_url=settings.amadeus_base_url,
        ))

    monitor = MonitorService(scrapers, route_repo, snapshot_repo)
    return monitor, route_repo, snapshot_repo


# ---------------------------------------------------------------------------
# Comando: add-route
# ---------------------------------------------------------------------------


@app.command("add-route")
def add_route(
    origin: Annotated[str, typer.Option("--origin", "-o", help="Código IATA de origem (ex: GRU)")],
    destination: Annotated[str, typer.Option("--destination", "-d", help="Código IATA de destino (ex: LHR)")],
    month: Annotated[str, typer.Option("--month", "-m", help="Mês alvo no formato YYYY-MM (ex: 2025-10)")],
    duration: Annotated[int, typer.Option("--duration", help="Duração alvo em dias")] = 14,
    currency: Annotated[str, typer.Option("--currency", help="Moeda (BRL, USD, EUR, GBP)")] = "BRL",
    stops: Annotated[int, typer.Option("--stops", help="Máximo de escalas (0=direto)")] = 1,
) -> None:
    """
    [bold cyan]Adiciona uma nova rota para monitoramento.[/bold cyan]

    Exemplos:
      flight-analyst add-route -o GRU -d LHR -m 2025-10
      flight-analyst add-route -o GRU -d CDG -m 2025-12 --duration 21 --stops 0
    """
    from flight_analyst.domain.models import RouteCreate, Currency

    try:
        travel_month = date.fromisoformat(month + "-01")
    except ValueError:
        console.print(f"[red]Formato de mês inválido: {month}. Use YYYY-MM (ex: 2025-10)[/red]")
        raise typer.Exit(1)

    try:
        curr = Currency(currency.upper())
    except ValueError:
        console.print(f"[red]Moeda inválida: {currency}. Use BRL, USD, EUR ou GBP[/red]")
        raise typer.Exit(1)

    route_create = RouteCreate(
        origin=origin.upper(),
        destination=destination.upper(),
        travel_month=travel_month,
        target_duration_days=duration,
        currency=curr,
        max_stops=stops,
    )

    async def _run() -> None:
        from flight_analyst.infra.db.supabase_client import db
        from flight_analyst.infra.db.repositories.route_repo import RouteRepository

        await db.connect()
        repo = RouteRepository(db)

        # Verificar duplicata
        exists = await repo.exists(route_create.origin, route_create.destination, month)
        if exists:
            console.print(
                f"[yellow]⚠ Rota {route_create.origin}→{route_create.destination} "
                f"para {month} já existe.[/yellow]"
            )
            raise typer.Exit(0)

        route = await repo.create(route_create)

        console.print(Panel(
            f"[green]✓ Rota criada com sucesso![/green]\n\n"
            f"  ID:       [bold]{route.id}[/bold]\n"
            f"  Rota:     [bold cyan]{route.label}[/bold cyan]\n"
            f"  Duração:  {route.target_duration_days} dias\n"
            f"  Moeda:    {route.currency.value}\n"
            f"  Escalas:  até {route.max_stops}\n\n"
            f"[dim]Próximo passo: flight-analyst run --route-id {route.id}[/dim]",
            title="✈  Nova Rota",
            border_style="cyan",
        ))

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Comando: list-routes
# ---------------------------------------------------------------------------


@app.command("list-routes")
def list_routes(
    all_routes: Annotated[bool, typer.Option("--all", help="Incluir rotas inativas")] = False,
) -> None:
    """Lista todas as rotas monitoradas."""

    async def _run() -> None:
        from flight_analyst.infra.db.supabase_client import db
        from flight_analyst.infra.db.repositories.route_repo import RouteRepository

        await db.connect()
        repo = RouteRepository(db)
        routes = await repo.get_all() if all_routes else await repo.get_all_active()

        if not routes:
            console.print("[yellow]Nenhuma rota encontrada. Use 'add-route' para criar.[/yellow]")
            return

        table = Table(title="✈  Rotas Monitoradas", box=box.ROUNDED, border_style="cyan")
        table.add_column("Rota", style="bold cyan")
        table.add_column("Duração", justify="center")
        table.add_column("Moeda", justify="center")
        table.add_column("Escalas", justify="center")
        table.add_column("Poll", justify="center")
        table.add_column("Status", justify="center")
        table.add_column("ID", style="dim")

        for r in routes:
            status = "[green]ativo[/green]" if r.is_active else "[red]inativo[/red]"
            table.add_row(
                r.label,
                f"{r.target_duration_days}d",
                r.currency.value,
                str(r.max_stops),
                f"{r.poll_interval_minutes}min",
                status,
                str(r.id)[:8] + "...",
            )

        console.print(table)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Comando: run
# ---------------------------------------------------------------------------


@app.command("run")
def run_collect(
    route_id: Annotated[str, typer.Option("--route-id", "-r", help="UUID da rota (omitir para todas)")] = "",
    month_year: Annotated[str, typer.Option("--month", help="Buscar mês específico YYYY-MM")] = "",
) -> None:
    """
    [bold cyan]Executa uma coleta de preços.[/bold cyan]

    Exemplos:
      flight-analyst run                          # coleta todas as rotas ativas
      flight-analyst run --route-id <UUID>        # coleta rota específica
      flight-analyst run -r <UUID> --month 2025-10  # coleta mês inteiro
    """

    async def _run() -> None:
        monitor, route_repo, snapshot_repo = await _build_monitor()

        if route_id:
            route = await route_repo.get_by_id(UUID(route_id))
            if not route:
                console.print(f"[red]Rota não encontrada: {route_id}[/red]")
                raise typer.Exit(1)

            if month_year:
                year, month = map(int, month_year.split("-"))
                console.print(f"[cyan]Coletando mês {month:02d}/{year} para {route.label}...[/cyan]")
                result = await monitor.collect_month_history(route, year, month)
            else:
                console.print(f"[cyan]Coletando preços para {route.label}...[/cyan]")
                result = await monitor.collect_for_route(route)

            if result and result.success:
                prices = [float(s.price) for s in result.snapshots]
                console.print(Panel(
                    f"[green]✓ Coleta concluída![/green]\n\n"
                    f"  Snapshots:    [bold]{len(result.snapshots)}[/bold]\n"
                    f"  Fonte:        {result.source.value}\n"
                    f"  Menor preço:  [bold green]{min(prices):,.0f} {result.snapshots[0].currency.value}[/bold green]\n"
                    f"  Maior preço:  {max(prices):,.0f}\n"
                    f"  Tempo:        {result.search_duration_seconds:.1f}s",
                    title=f"✈  {route.label}",
                    border_style="green",
                ))
            else:
                console.print("[red]✗ Nenhum preço coletado. Verifique os logs.[/red]")
        else:
            console.print("[cyan]Coletando todas as rotas ativas...[/cyan]")
            results = await monitor.collect_all_routes()
            success_count = sum(1 for r in results.values() if r and r.success)
            console.print(f"\n[green]✓ {success_count}/{len(results)} rotas coletadas com sucesso.[/green]")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Comando: status
# ---------------------------------------------------------------------------


@app.command("status")
def show_status(
    route_id: Annotated[str, typer.Option("--route-id", "-r", help="UUID da rota")] = "",
    limit: Annotated[int, typer.Option("--limit", "-n", help="Número de snapshots")] = 10,
) -> None:
    """Exibe os últimos snapshots de preço de uma rota."""

    async def _run() -> None:
        from flight_analyst.infra.db.supabase_client import db
        from flight_analyst.infra.db.repositories.route_repo import RouteRepository
        from flight_analyst.infra.db.repositories.snapshot_repo import SnapshotRepository

        await db.connect()
        route_repo = RouteRepository(db)
        snapshot_repo = SnapshotRepository(db)

        if route_id:
            routes = []
            route = await route_repo.get_by_id(UUID(route_id))
            if route:
                routes = [route]
        else:
            routes = await route_repo.get_all_active()

        if not routes:
            console.print("[yellow]Nenhuma rota encontrada.[/yellow]")
            return

        for route in routes:
            snapshots = await snapshot_repo.get_latest(route.id, limit=limit)
            total = await snapshot_repo.count(route.id)

            if not snapshots:
                console.print(f"[yellow]{route.label}: sem snapshots ainda. Execute 'run' para coletar.[/yellow]")
                continue

            table = Table(
                title=f"✈  {route.label} — últimos {len(snapshots)} de {total} snapshots",
                box=box.SIMPLE_HEAVY,
                border_style="blue",
            )
            table.add_column("Data Coleta", style="dim")
            table.add_column("Partida")
            table.add_column("Retorno")
            table.add_column("Preço", justify="right", style="bold green")
            table.add_column("Airline")
            table.add_column("Escalas", justify="center")
            table.add_column("Fonte", style="dim")

            for s in snapshots:
                table.add_row(
                    s.scraped_at.strftime("%d/%m %H:%M"),
                    s.departure_date.strftime("%d/%m/%Y"),
                    s.return_date.strftime("%d/%m/%Y") if s.return_date else "—",
                    f"{s.price:,.0f} {s.currency.value}",
                    s.airline or "—",
                    str(s.stops),
                    s.source.value,
                )

            console.print(table)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Comando: alert-test
# ---------------------------------------------------------------------------


@app.command("alert-test")
def test_alert(
    message: Annotated[str, typer.Argument(help="Mensagem para enviar no teste")] = "Este é um teste do sistema de notificações do Flight Analyst.",
) -> None:
    """Testa o envio de notificações (Telegram/Ntfy)."""
    
    async def _run() -> None:
        from flight_analyst.application.notification_service import NotificationService
        
        console.print("[cyan]Testando envio de notificação...[/cyan]")
        notifier = NotificationService()
        
        success = await notifier.send_test_message(message)
        if success:
            console.print("[green][OK] Mensagem de teste enviada com sucesso para o Telegram![/green]")
        else:
            console.print("[red][ERRO] Falha ao enviar mensagem. Verifique suas credenciais no .env[/red]")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Comando: worker
# ---------------------------------------------------------------------------


@app.command("worker")
def run_worker() -> None:
    """Inicia o agendador local (APScheduler) em background."""
    
    async def _run() -> None:
        from flight_analyst.jobs.scheduler_local import start_local_worker
        console.print("[cyan]Iniciando Flight Analyst Worker local...[/cyan]")
        console.print("[dim]Pressione Ctrl+C para parar.[/dim]")
        await start_local_worker()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Comando: dashboard
# ---------------------------------------------------------------------------


@app.command("dashboard")
def run_dashboard() -> None:
    """Inicia o Dashboard Interativo (Streamlit)."""
    import subprocess
    import os
    from pathlib import Path
    
    app_path = Path(__file__).parent / "dashboard" / "app.py"
    console.print(f"[cyan]Iniciando o Dashboard: {app_path}[/cyan]")
    
    # Roda o Streamlit como subprocesso
    env = os.environ.copy()
    subprocess.run(["streamlit", "run", str(app_path)], env=env)


@app.command("api")
def run_api() -> None:
    """Inicia o servidor FastAPI localmente."""
    import uvicorn
    from flight_analyst.config import settings
    
    console.print(f"[cyan]Iniciando API Flight Analyst na porta {settings.api_port}...[/cyan]")
    uvicorn.run("flight_analyst.api.main:app", host="127.0.0.1", port=settings.api_port, reload=True)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    from flight_analyst.config import settings
    from flight_analyst.sentry_setup import init_sentry
    import structlog

    # Configurar logging estruturado
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if not settings.is_production
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(__import__("logging"), settings.log_level.value)
        ),
    )

    # Inicializar Sentry
    init_sentry(
        dsn=settings.sentry_dsn,
        environment=settings.app_env.value,
    )

    app()


if __name__ == "__main__":
    main()
