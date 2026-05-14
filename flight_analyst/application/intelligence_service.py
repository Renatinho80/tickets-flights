"""flight_analyst/application/intelligence_service.py
Serviço de Inteligência — processa snapshots para gerar recomendações.
Usa estatística básica (z-score, médias, tendências) via pandas.
Trabalha com Real Brasileiro (BRL) como moeda de referência padrão.
"""

from datetime import datetime
from decimal import Decimal

import pandas as pd
import structlog
from flight_analyst.domain.models import Recommendation, PriceSnapshot, Route

log = structlog.get_logger(__name__)

class IntelligenceService:
    def __init__(self) -> None:
        pass

    def _calculate_trend_slope(self, df: pd.DataFrame) -> float:
        """Calcula a inclinação da tendência linear (preço vs tempo)."""
        if len(df) < 2:
            return 0.0
        
        # Converte datas para dias desde o início
        df = df.copy()
        df["days_since_start"] = (df["scraped_at"] - df["scraped_at"].min()).dt.days
        
        # Regressão linear simples (y = mx + b)
        x = df["days_since_start"]
        y = df["price"]
        
        # Correção para variância zero no x
        if x.var() == 0:
            return 0.0
            
        slope = y.cov(x) / x.var()
        return float(slope)

    def _calculate_opportunity_score(
        self,
        current_price: float,
        price_avg: float,
        price_p25: float,
        price_min: float,
        is_error_fare: bool,
    ) -> float:
        """
        Gera um score de 0 a 100 baseado na atratividade do preço.
        100 = Error fare (imperdível)
        80-99 = Próximo do mínimo histórico
        60-79 = Abaixo da média (próximo do P25)
        40-59 = Na média
        0-39 = Acima da média
        """
        if is_error_fare:
            return 100.0
            
        if current_price <= price_min:
            return 95.0
            
        if current_price <= price_p25:
            # Escala linear entre P25 (60) e Min (94)
            pct = (price_p25 - current_price) / max(1, (price_p25 - price_min))
            return 60.0 + (pct * 34.0)
            
        if current_price <= price_avg:
            # Escala linear entre Avg (40) e P25 (59)
            pct = (price_avg - current_price) / max(1, (price_avg - price_p25))
            return 40.0 + (pct * 19.0)
            
        # Acima da média
        pct = (current_price - price_avg) / max(1, price_avg)
        score = 39.0 - (pct * 100.0)
        return max(0.0, score)

    def generate_recommendation(
        self,
        route: Route,
        snapshots: list[PriceSnapshot],
        historical_stats: dict | None = None,
    ) -> Recommendation | None:
        """
        Analisa o histórico recente e as estatísticas de longo prazo para gerar
        uma recomendação de compra para a rota.
        """
        if not snapshots:
            log.warning("intelligence_no_data", route=route.label)
            return None

        # Converter para DataFrame para facilitar análise estatística
        df = pd.DataFrame([s.model_dump() for s in snapshots])
        df["price"] = df["price"].astype(float)
        df["scraped_at"] = pd.to_datetime(df["scraped_at"])
        df = df.sort_values("scraped_at")

        current_snapshot = snapshots[-1]
        current_price = float(current_snapshot.price)
        
        # Estatísticas descritivas recentes (últimos snapshots passados)
        price_avg_recent = df["price"].mean()
        price_std_recent = df["price"].std()
        price_min_recent = df["price"].min()
        price_p25_recent = df["price"].quantile(0.25)
        
        # Lógica de Error Fare (Z-Score)
        # Consideramos Error Fare se o preço cair > 2.5 desvios padrões abaixo da média
        # ou se o preço absoluto for irrealisticamente baixo (< R$500 para inter)
        is_error_fare = False
        if len(df) >= 5 and pd.notna(price_std_recent) and price_std_recent > 0:
            z_score = (current_price - price_avg_recent) / price_std_recent
            if z_score < -2.5:
                is_error_fare = True
                
        # Proteção extra: se for um voo internacional e custar muito pouco
        # Obs: valores de referência em BRL (Real Brasileiro)
        if route.currency.value == "BRL" and current_price < 800 and route.origin[:2] != route.destination[:2]:
            is_error_fare = True

        trend_slope = self._calculate_trend_slope(df)
        
        # Combinar com histórico longo se disponível
        hist_avg = historical_stats.get("price_avg") if historical_stats else price_avg_recent
        hist_p25 = historical_stats.get("price_p25") if historical_stats else price_p25_recent
        hist_min = historical_stats.get("price_min") if historical_stats else price_min_recent
        
        opportunity_score = self._calculate_opportunity_score(
            current_price=current_price,
            price_avg=float(hist_avg) if hist_avg else price_avg_recent,
            price_p25=float(hist_p25) if hist_p25 else price_p25_recent,
            price_min=float(hist_min) if hist_min else price_min_recent,
            is_error_fare=is_error_fare,
        )

        # Geração de texto simples
        if is_error_fare:
            text = "⚠️ ERROR FARE DETECTADO! O preço está anormalmente baixo. Compre imediatamente antes que a companhia perceba."
        elif opportunity_score >= 80:
            text = f"Excelente oportunidade. O preço está próximo do mínimo histórico de {route.currency.value} {hist_min:.0f}."
        elif opportunity_score >= 60:
            text = f"Preço atrativo, abaixo da média histórica. Boa janela para compra se as datas forem fixas."
        elif opportunity_score >= 40:
            text = f"Preço dentro da média de mercado. Monitore se tiver flexibilidade."
        else:
            text = f"Preço acima da média ({route.currency.value} {hist_avg:.0f}). Sugiro esperar."

        if trend_slope > 10:
            text += " Atenção: Há uma forte tendência de alta nos últimos dias."
        elif trend_slope < -10:
            text += " O preço está em tendência de queda. Pode valer a pena esperar 24h."

        return Recommendation(
            route_id=route.id,
            opportunity_score=opportunity_score,
            current_price=Decimal(str(current_price)),
            avg_price_30d=Decimal(str(price_avg_recent)),
            avg_price_historical=Decimal(str(hist_avg)) if hist_avg else None,
            trend_slope=trend_slope,
            days_to_departure=current_snapshot.advance_days,
            recommendation_text=text,
            is_error_fare=is_error_fare,
        )
