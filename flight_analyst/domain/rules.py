"""flight_analyst/domain/rules.py
Constantes de negócio, thresholds e regras do domínio de análise de preços.
Centralizadas aqui para evitar magic numbers espalhados pelo código.
"""

# ---------------------------------------------------------------------------
# Análise de Duração
# ---------------------------------------------------------------------------

# Durações (em dias) a incluir na matriz comparativa
DURATION_OPTIONS: list[int] = [7, 10, 14, 17, 20, 21, 25, 28]

# ---------------------------------------------------------------------------
# Janela de Antecedência de Compra
# ---------------------------------------------------------------------------

# Antecedências (em dias antes da partida) analisadas para "sweet spot"
ADVANCE_WINDOWS: list[int] = [14, 21, 30, 45, 60, 90, 120]

# ---------------------------------------------------------------------------
# Opportunity Score (0–100)
# ---------------------------------------------------------------------------

# Thresholds de classificação
SCORE_BUY_NOW = 80        # COMPRE AGORA
SCORE_GOOD_DEAL = 60      # BOA OPORTUNIDADE
SCORE_AVERAGE = 40        # PREÇO MÉDIO
SCORE_EXPENSIVE = 20      # CARO
# 0–19 = MUITO CARO

# Pesos para o cálculo do score
SCORE_WEIGHTS: dict[str, float] = {
    "z_score_30d": 0.25,          # Desvio vs. média 30 dias
    "z_score_historical": 0.30,   # Desvio vs. média histórica
    "yoy_delta": 0.20,            # Delta YoY (% vs. mesmo mês do ano anterior)
    "trend_slope": 0.10,          # Tendência de alta/baixa recente
    "days_to_departure": 0.10,    # Antecedência em relação à partida
    "season_factor": 0.05,        # Ajuste por alta/baixa temporada
}

# ---------------------------------------------------------------------------
# Error Fare Detection
# ---------------------------------------------------------------------------

# Queda abaixo desse percentual da média histórica = provável tarifa-erro
ERROR_FARE_THRESHOLD_PCT: float = 0.50  # 50% abaixo da média

# ---------------------------------------------------------------------------
# Sazonalidade
# ---------------------------------------------------------------------------

# Meses de alta temporada (0-indexado: 1=Jan, 12=Dez)
HIGH_SEASON_MONTHS: list[int] = [12, 1, 7]  # Dezembro, Janeiro, Julho

# Mínimo de snapshots para análises estatísticas confiáveis
MIN_SNAPSHOTS_FOR_ANALYSIS: int = 20

# Janela de snapshots "recentes" para cálculo de Z-score 30d
RECENT_WINDOW_DAYS: int = 30

# ---------------------------------------------------------------------------
# Resiliência e Rate Limiting
# ---------------------------------------------------------------------------

# Intervalo mínimo entre polls da mesma rota/fonte (segundos)
MIN_POLL_INTERVAL_SECONDS: int = 45

# Circuit breaker: número de falhas consecutivas antes de pausar
CIRCUIT_BREAKER_MAX_FAILURES: int = 3

# Pausa após circuit breaker abrir (horas)
CIRCUIT_BREAKER_PAUSE_HOURS: int = 1

# Retry: backoff exponencial em acesso externo
RETRY_MAX_ATTEMPTS: int = 3
RETRY_MIN_WAIT_SECONDS: float = 2.0
RETRY_MAX_WAIT_SECONDS: float = 30.0

# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------

# Score mínimo para notificação automática
ALERT_SCORE_THRESHOLD: int = 70

# Queda de preço (%) para alerta de "price drop"
ALERT_PRICE_DROP_PCT: float = 5.0

# Nomes dos canais de notificação disponíveis
NOTIFIER_TELEGRAM = "telegram"
NOTIFIER_NTFY = "ntfy"

# ---------------------------------------------------------------------------
# Fontes de dados (prioridade de cascata)
# ---------------------------------------------------------------------------

SCRAPER_PRIORITY: list[str] = ["playwright", "serpapi", "amadeus"]

# Limite de requisições da SerpApi no tier gratuito (por mês)
SERPAPI_MONTHLY_LIMIT: int = 100

# ---------------------------------------------------------------------------
# Meses do ano em PT-BR (para outputs)
# ---------------------------------------------------------------------------

MONTH_NAMES_PT: dict[int, str] = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
    5: "maio", 6: "junho", 7: "julho", 8: "agosto",
    9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def score_to_level(score: float) -> str:
    """Converte score numérico em label textual."""
    if score >= SCORE_BUY_NOW:
        return "COMPRE AGORA"
    elif score >= SCORE_GOOD_DEAL:
        return "BOA OPORTUNIDADE"
    elif score >= SCORE_AVERAGE:
        return "PREÇO MÉDIO"
    elif score >= SCORE_EXPENSIVE:
        return "CARO"
    return "MUITO CARO"


def is_high_season(month: int) -> bool:
    """Retorna True se o mês é considerado alta temporada."""
    return month in HIGH_SEASON_MONTHS
