"""flight_analyst/application/ai_tools.py
Ferramentas (Tools) que o Gemini pode invocar para consultar dados do banco sob demanda.
Substitui a injeção estática de texto (MCP-like pattern).
"""

from typing import Any
from uuid import UUID

from flight_analyst.infra.db.supabase_client import db
from flight_analyst.infra.db.repositories.snapshot_repo import SnapshotRepository

# Repositório estático para as ferramentas acessarem o banco
_snapshot_repo = SnapshotRepository(db)

async def get_recent_prices(route_id: str, days: int = 7) -> list[dict[str, Any]]:
    """
    Busca o histórico de preços recentes de passagens aéreas para uma rota específica.
    Use esta ferramenta quando precisar saber a evolução de preço nos últimos 'days' dias.
    
    Args:
        route_id: O ID (UUID) da rota que o usuário está perguntando.
        days: Quantos dias de histórico buscar (padrão: 7).
    
    Returns:
        Uma lista de preços com a data de coleta e a companhia aérea.
    """
    try:
        uuid_obj = UUID(route_id)
        # É importante garantir que o DB esteja conectado. 
        # Como o FastAPI ou Streamlit já conectaram, usamos a instância global.
        snapshots = await _snapshot_repo.get_recent(uuid_obj, days=days)
        
        # Filtramos os dados essenciais para economizar tokens
        results = []
        for s in snapshots:
            results.append({
                "date": s.scraped_at.strftime("%Y-%m-%d %H:%M"),
                "price": float(s.price),
                "airline": s.airline,
                "source": s.source.value,
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]


async def get_price_statistics(route_id: str, days: int = 30) -> dict[str, Any]:
    """
    Retorna estatísticas vitais do preço da rota nos últimos dias, como mínimo, máximo e média.
    Use isso para embasar sua recomendação de compra.
    
    Args:
        route_id: O ID (UUID) da rota.
        days: A janela de dias para calcular as métricas.
    """
    try:
        uuid_obj = UUID(route_id)
        snapshots = await _snapshot_repo.get_recent(uuid_obj, days=days)
        
        if not snapshots:
            return {"error": "Nenhum dado encontrado para gerar estatísticas."}
            
        prices = [float(s.price) for s in snapshots]
        return {
            "period_days": days,
            "min_price": min(prices),
            "max_price": max(prices),
            "avg_price": round(sum(prices) / len(prices), 2),
            "total_samples": len(prices),
            "current_price": prices[0] # Assumindo que o primeiro é o mais recente pelo order by do get_recent
        }
    except Exception as e:
        return {"error": str(e)}

import asyncio

def get_recent_prices_sync(route_id: str, days: int = 7) -> list[dict[str, Any]]:
    """Busca o histórico de preços recentes de passagens aéreas para uma rota."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
        return loop.run_until_complete(get_recent_prices(route_id, days))
    return loop.run_until_complete(get_recent_prices(route_id, days))

def get_price_statistics_sync(route_id: str, days: int = 30) -> dict[str, Any]:
    """Retorna estatísticas vitais do preço da rota nos últimos dias, como mínimo, máximo e média."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
        return loop.run_until_complete(get_price_statistics(route_id, days))
    return loop.run_until_complete(get_price_statistics(route_id, days))

# Exportamos as definições para plugar no Gemini
AVAILABLE_TOOLS = [get_recent_prices_sync, get_price_statistics_sync]
