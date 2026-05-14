-- =============================================================================
-- Flight Analyst — Schema Inicial do Banco de Dados
-- Supabase (PostgreSQL 15+)
-- Migration: 001_initial_schema.sql
-- =============================================================================

-- Habilitar extensões necessárias
CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()
-- CREATE EXTENSION IF NOT EXISTS "pg_cron";  -- Habilitar via Dashboard do Supabase se necessário

-- =============================================================================
-- TABELA: routes
-- Rotas monitoradas — configuração de origem/destino e parâmetros de coleta
-- =============================================================================
CREATE TABLE IF NOT EXISTS routes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin          CHAR(3)     NOT NULL CHECK (origin ~ '^[A-Z]{3}$'),
    destination     CHAR(3)     NOT NULL CHECK (destination ~ '^[A-Z]{3}$'),
    travel_month    DATE        NOT NULL,  -- Primeiro dia do mês alvo
    target_duration_days  INT  NOT NULL DEFAULT 14 CHECK (target_duration_days BETWEEN 1 AND 90),
    flexibility_days      INT  NOT NULL DEFAULT 3  CHECK (flexibility_days BETWEEN 0 AND 14),
    max_stops             INT  NOT NULL DEFAULT 1  CHECK (max_stops BETWEEN 0 AND 3),
    currency        CHAR(3)     NOT NULL DEFAULT 'BRL',
    poll_interval_minutes       INT NOT NULL DEFAULT 60,
    aggressive_poll_minutes     INT NOT NULL DEFAULT 15,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (origin, destination, travel_month)
);

-- Trigger para atualizar updated_at automaticamente
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER routes_updated_at
    BEFORE UPDATE ON routes
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- =============================================================================
-- TABELA: price_snapshots
-- Snapshots de preço capturados pelos scrapers
-- =============================================================================
CREATE TABLE IF NOT EXISTS price_snapshots (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id        UUID        NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    scraped_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    departure_date  DATE        NOT NULL,
    return_date     DATE,
    duration_days   INT,         -- Calculado na aplicação
    price           NUMERIC(12, 2) NOT NULL CHECK (price >= 0),
    currency        CHAR(3)     NOT NULL,
    airline         TEXT,
    stops           INT         NOT NULL DEFAULT 0 CHECK (stops >= 0),
    source          TEXT        NOT NULL CHECK (source IN ('playwright', 'serpapi', 'amadeus')),
    raw_data        JSONB,
    advance_days    INT          -- Calculado na aplicação
);

-- Índices para consultas frequentes
CREATE INDEX IF NOT EXISTS idx_snapshots_route_scraped
    ON price_snapshots (route_id, scraped_at DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_route_departure
    ON price_snapshots (route_id, departure_date);

CREATE INDEX IF NOT EXISTS idx_snapshots_advance_days
    ON price_snapshots (route_id, advance_days, scraped_at DESC);

-- =============================================================================
-- TABELA: historical_stats
-- Estatísticas agregadas por rota, período e antecedência
-- =============================================================================
CREATE TABLE IF NOT EXISTS historical_stats (
    id                  UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id            UUID    NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    stat_year           INT     NOT NULL,
    stat_month          INT     NOT NULL CHECK (stat_month BETWEEN 1 AND 12),
    stat_week_of_month  INT     CHECK (stat_week_of_month BETWEEN 1 AND 5),
    stat_day_of_week    INT     CHECK (stat_day_of_week BETWEEN 0 AND 6),  -- 0=Seg
    duration_days       INT,
    advance_days        INT,
    price_min           NUMERIC(12, 2),
    price_avg           NUMERIC(12, 2),
    price_max           NUMERIC(12, 2),
    price_p25           NUMERIC(12, 2),
    price_p75           NUMERIC(12, 2),
    sample_count        INT     NOT NULL DEFAULT 0,
    yoy_delta_pct       NUMERIC(8, 4),  -- % vs. mesmo período do ano anterior
    is_high_season      BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (route_id, stat_year, stat_month, duration_days, advance_days)
);

CREATE INDEX IF NOT EXISTS idx_historical_route_period
    ON historical_stats (route_id, stat_year, stat_month);

-- =============================================================================
-- TABELA: duration_matrix
-- Comparativo de preços por duração de viagem para um mês alvo
-- =============================================================================
CREATE TABLE IF NOT EXISTS duration_matrix (
    id                  UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id            UUID    NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    travel_month        DATE    NOT NULL,
    duration_days       INT     NOT NULL,
    price_avg           NUMERIC(12, 2),
    price_min           NUMERIC(12, 2),
    delta_vs_base_pct   NUMERIC(8, 4),  -- % em relação à duração base
    best_departure_date DATE,
    best_return_date    DATE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (route_id, travel_month, duration_days)
);

-- =============================================================================
-- TABELA: recommendations
-- Recomendações geradas pelo motor de inteligência
-- =============================================================================
CREATE TABLE IF NOT EXISTS recommendations (
    id                      UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id                UUID    NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    generated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    opportunity_score       NUMERIC(5, 2) NOT NULL CHECK (opportunity_score BETWEEN 0 AND 100),
    current_price           NUMERIC(12, 2),
    avg_price_30d           NUMERIC(12, 2),
    avg_price_historical    NUMERIC(12, 2),
    yoy_delta_pct           NUMERIC(8, 4),
    trend_slope             NUMERIC(10, 6),
    days_to_departure       INT,
    recommendation_text     TEXT,
    is_error_fare           BOOLEAN NOT NULL DEFAULT FALSE,
    metadata                JSONB
);

CREATE INDEX IF NOT EXISTS idx_recommendations_route_generated
    ON recommendations (route_id, generated_at DESC);

-- =============================================================================
-- TABELA: alerts
-- Alertas disparados ao usuário
-- =============================================================================
CREATE TABLE IF NOT EXISTS alerts (
    id                  UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    route_id            UUID    NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    alert_type          TEXT    NOT NULL CHECK (
                            alert_type IN ('opportunity', 'error_fare', 'yoy_cheaper', 'best_advance', 'price_drop')
                        ),
    triggered_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    opportunity_score   NUMERIC(5, 2),
    current_price       NUMERIC(12, 2),
    message             TEXT    NOT NULL,
    sent_via            TEXT[], -- ex: {'telegram', 'ntfy'}
    is_sent             BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_alerts_route_triggered
    ON alerts (route_id, triggered_at DESC);

-- =============================================================================
-- ROW LEVEL SECURITY (Supabase)
-- Single-user: habilitar RLS mas permitir service_role irrestrito
-- =============================================================================
ALTER TABLE routes             ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_snapshots    ENABLE ROW LEVEL SECURITY;
ALTER TABLE historical_stats   ENABLE ROW LEVEL SECURITY;
ALTER TABLE duration_matrix    ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts             ENABLE ROW LEVEL SECURITY;

-- Política: service role tem acesso total (usado pela aplicação)
CREATE POLICY "service_role_all" ON routes
    USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON price_snapshots
    USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON historical_stats
    USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON duration_matrix
    USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON recommendations
    USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all" ON alerts
    USING (true) WITH CHECK (true);

-- =============================================================================
-- pg_cron: job de agregação diária às 3h UTC
-- (Executar manualmente no SQL Editor do Supabase após habilitar pg_cron)
-- =============================================================================
-- SELECT cron.schedule(
--     'aggregate-daily-stats',
--     '0 3 * * *',
--     $$
--     SELECT net.http_post(
--         url := 'https://your-api.railway.app/internal/aggregate',
--         headers := '{"Authorization": "Bearer YOUR_INTERNAL_KEY"}'::jsonb
--     );
--     $$
-- );
