# Papel & Identidade da Aplicação

Você é um engenheiro de software sênior especializado em sistemas de inteligência
de dados, análise preditiva e automação de decisão. Construa uma aplicação que
atue como um analista pessoal de passagens aéreas — não um rastreador de preços,
mas um sistema que aprende padrões históricos, entende sazonalidade, compara
cenários e recomenda ativamente o melhor momento, duração e janela de compra.

A aplicação responde perguntas como:
  → "Outubro é um bom mês para voar GRU→LHR este ano?"
  → "14 ou 21 dias sai mais barato em outubro?"
  → "Qual semana de outubro historicamente tem preços menores?"
  → "Estou comprando caro ou barato comparado ao ano passado?"
  → "Quando devo comprar para pegar o preço ideal?"
  → "Qual mês é mais barato para ir a Londres?"

Premissas:
- Single-user, uso pessoal
- Pode usar qualquer ferramenta, framework ou serviço de hospedagem
  desde que exista pacote gratuito suficiente para uso pessoal
- Arquitetura cloud-native com free tiers onde possível
- Fallback local (SQLite + APScheduler) se serviços externos falharem
- Todo o stack deve funcionar com zero custo para volume individual

---

# Stack Definitiva — Free Tier

LINGUAGEM: Python 3.11+
  - asyncio + mypy strict + Pydantic v2
  - Único runtime para scraping, análise, API e scheduler

HOSPEDAGEM: Railway (free tier — 500h/mês)
  - Deploy direto do GitHub via Railway CLI ou GitHub App
  - Serviços: worker (scheduler), api (FastAPI), dashboard
  - Alternativa: Render.com (750h/mês grátis)

BANCO DE DADOS: Supabase (PostgreSQL gerenciado — free 500MB)
  - Substitui SQLite local — dados persistem na nuvem
  - ORM: SQLModel (Pydantic v2 + SQLAlchemy async)
  - Tabelas: routes, price_snapshots, historical_stats,
             duration_matrix, recommendations, alerts
  - Fallback: SQLite local se Supabase offline
  - Habilitar pg_cron para job diário de agregação

COLETA DE DADOS:
  Primário: SerpApi (Google Flights JSON) — 100 req/mês grátis
    - API limpa, sem necessidade de browser
    - engine=google_flights, departure_id, arrival_id, currency, hl=pt
  Secundário: Playwright + playwright-stealth (ilimitado, headless)
    - Ativa automaticamente quando SerpApi esgota quota
    - Google Flights + Kayak
  Alternativa: Amadeus for Developers (2.000 req/mês grátis)
    - Flight Inspiration Search para dados históricos de sazonalidade

SCHEDULER: Inngest (event-driven, free 50k eventos/mês)
  - Retry automático, cron nativo, observabilidade de jobs
  - Jobs: collect_prices (*/60), aggregate_stats (0 3 *),
          gen_recommendations (0 */6), send_alerts (por evento)
  - Fallback: APScheduler local

DASHBOARD: Streamlit Cloud (free — 1 app privado)
  - Deploy do GitHub, acesso via URL com autenticação
  - Páginas: Painel, Histórico YoY, Duração, Calendário, Compra, Ask

NOTIFICAÇÕES: Telegram Bot API (gratuito e ilimitado)
  - python-telegram-bot v20 (asyncio)
  - Mensagens com score, comparativo e botão "Ver no Dashboard"
  - Fallback: ntfy.sh

MONITORAMENTO: Sentry (free — 5k erros/mês)
  - Captura exceptions de scrapers e jobs em tempo real
  - Alerta por e-mail em falha crítica

CI/CD: GitHub Actions (free — 2000 min/mês)
  - Push → lint (ruff) → mypy → pytest → deploy Railway

SECRETS: Railway env vars ou Doppler free tier

---

# Arquitetura

flight_analyst/
├── domain/
│   ├── models.py
│   ├── rules.py
│   ├── price_analyzer.py
│   ├── seasonality.py
│   └── recommendation_engine.py
├── infra/
│   ├── db/ (supabase_client.py + repositories/)
│   ├── scrapers/ (serpapi.py, playwright.py, amadeus.py)
│   ├── scheduler/ (inngest_functions.py)
│   └── notifiers/ (telegram.py, ntfy.py)
├── application/
│   ├── monitor_service.py
│   ├── history_service.py
│   └── insight_service.py
├── api/
│   └── main.py (FastAPI)
├── ui/
│   └── dashboard.py (Streamlit)
├── config.py
├── cli.py
└── main.py

---

# Motor de Inteligência (8 módulos — implemente integralmente)

## 1. Análise YoY (Year over Year)
Acumular snapshots e construir historical_stats por:
  mês, semana do mês, dia da semana, antecedência de compra
Calcular: price_min, avg, max, p25, p75, yoy_delta_pct, is_high_season
Fonte extra: Amadeus Flight Inspiration Search para histórico inicial
Resposta: "Outubro 2025 está X% mais caro que outubro 2024"

## 2. Decomposição Sazonal
statsmodels seasonal_decompose (modelo multiplicativo)
Separar: trend + seasonal + residual
Identificar semanas mais baratas e mais caras com delta percentual

## 3. Score de Oportunidade (0–100)
opportunity_score = f(
  z_score_vs_30d, z_score_vs_historical, yoy_delta,
  trend_slope, days_to_departure, season_factor
)
80–100 COMPRE AGORA | 60–79 BOA OPORTUNIDADE
40–59 PREÇO MÉDIO   | 20–39 CARO | 0–19 MUITO CARO

## 4. Comparador de Duração
Matriz [7, 10, 14, 17, 20, 21, 25, 28] dias para o mês alvo
Delta percentual vs. duração base + melhor janela de datas
Saída: "14 dias sai 22% mais barato que 20 dias em outubro."

## 5. Calendário de Preços
Score combinação dia_ida × dia_volta para cada duração
Identificar melhor dia para partir e voltar dentro do mês
Saída: "Partir 8/out (ter), voltar 22/out — R$650 abaixo da média"

## 6. Janela Ideal de Compra
Curva preço × antecedência [14d, 21d, 30d, 45d, 60d, 90d, 120d]
Sweet spot: antecedência com menor preço histórico
Alerta quando usuário entra na janela ideal

## 7. Ranking de Meses
Comparar todos os meses do ano para a rota
Rankear por custo + identificar alta temporada
Cruzar com feriados do país de destino (hardcoded)
Saída: "Outubro = 4º mais barato. Março = mais barato. Dez +38%."

## 8. Ask — Resposta em Linguagem Natural
CLI: python main.py ask "query livre"
API: POST /ask {"query": "...", "route_id": "..."}
Template matching + dados do Supabase → resposta PT-BR com números reais
Sem LLM externo — lógica determinística

---

# Configuração (routes.yaml)

routes:
  - id: gru_lhr_oct25
    origin: GRU
    destination: LHR
    travel_month: 2025-10
    target_duration_days: 20
    flexibility_days: 3
    max_stops: 1
    currency: BRL
    poll_interval_minutes: 60
    aggressive_poll_minutes: 15

analysis:
  duration_compare: [7, 10, 14, 17, 20, 21, 25, 28]
  book_advance_range_days: [14, 21, 30, 45, 60, 90, 120]
  yoy_comparison: true
  seasonality_decompose: true
  min_snapshots_for_analysis: 20

alerts:
  opportunity_score_threshold: 70
  historical_low_alert: true
  yoy_cheaper_alert: true
  best_advance_window_alert: true
  price_drop_pct: 5

---

# Implementação em 4 Fases

Fase 1 — Fundação (dados + coleta):
  1.  domain/models.py
  2.  domain/rules.py
  3.  infra/db/supabase_client.py + fallback SQLite
  4.  infra/db/repositories/
  5.  infra/scrapers/serpapi.py (primário)
  6.  infra/scrapers/playwright.py (fallback)
  7.  infra/scrapers/amadeus.py (histórico)
  8.  application/monitor_service.py
  9.  config.py + routes.yaml
  10. sentry_setup.py

Fase 2 — Motor de inteligência:
  11. domain/price_analyzer.py
  12. domain/seasonality.py
  13. domain/recommendation_engine.py
  14. application/history_service.py
  15. application/insight_service.py
  16. api/main.py (FastAPI: /recommendations, /ask, /routes/{id}/stats)

Fase 3 — Scheduler + notificações + CI/CD:
  17. infra/scheduler/inngest_functions.py
  18. infra/notifiers/telegram.py
  19. infra/notifiers/ntfy.py
  20. cli.py (run, add-route, status, ask, history)
  21. .github/workflows/deploy.yml

Fase 4 — Dashboard + polish:
  22. ui/dashboard.py — 6 páginas Streamlit Cloud
  23. README.md — setup, diagrama, exemplos de query

---

# Padrões de Engenharia Obrigatórios

Resiliência:
  - tenacity: retry exponential backoff em scrapers e APIs externas
  - Circuit breaker: 3 falhas → pausa 1h → notifica Telegram
  - Fallback em cascata: SerpApi → Playwright → Amadeus
  - Supabase offline → SQLite local transparente

Qualidade:
  - mypy strict + ruff em todo o projeto
  - pytest com fixtures sintéticos para domain
  - Sentry capturando exceptions com contexto de rota

Restrições duras:
  - Somente free tiers — documentar limite de cada serviço no README
  - Rate limit: 45s mínimo entre polls da mesma fonte
  - Sem LLM externo para análise — lógica determinística local
  - Secrets nunca em código — env vars / Railway / Doppler

---

# Início da Implementação
Implemente a Fase 1 completa. Antes de cada arquivo, escreva uma linha
contextualizando seu papel na arquitetura. Ao concluir, apresente um
checklist do que foi feito e aguarde confirmação para a Fase 2.