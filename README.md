# ✈️ Flight Analyst v3

O **Flight Analyst** é um sistema profissional de monitoramento e análise de preços de passagens aéreas. Ele combina raspagem de dados multi-fonte, inteligência estatística e agentes de IA (**Google Gemini**) para detectar janelas ideais de compra e *Error Fares*.

## 🚀 Como Rodar o Projeto

O projeto segue uma arquitetura desacoplada e moderna: **Frontend (Streamlit) ➔ API (FastAPI) ➔ DB (Supabase/SQLite)**.

### 1. Pré-requisitos e Instalação
- Python 3.10+
- `.venv` ativado e dependências instaladas (`pip install -e ".[all]"`)
- Playwright instalado (`playwright install chromium`)
- Arquivo `.env` configurado com as chaves necessárias.

### 2. Iniciando os Serviços (Ordem Recomendada)

**Atenção:** Certifique-se de executar os comandos abaixo **dentro do seu ambiente virtual** (ex: ativando o `.venv` ou usando `.venv/Scripts/python` no Windows).

#### **Passo A: Iniciar a API (Backend)**
A API centraliza toda a lógica de negócio, scrapers e acesso ao banco.
```bash
# Se o .venv estiver ativado:
python -m flight_analyst.main api
# Ou no Windows diretamente:
# .\.venv\Scripts\python -m flight_analyst.main api
```
*A API rodará em http://127.0.0.1:8000*

#### **Passo B: Iniciar o Dashboard (Frontend)**
Interface visual para gestão de rotas e análise de preços. **A API deve estar ligada.**
```bash
python -m flight_analyst.main dashboard
```
*Acesse no navegador e use a `DASHBOARD_PASSWORD` definida no seu .env.*

#### **Passo C: Iniciar o Worker (Agendador)**
Opcional para uso manual, mas necessário para monitoramento 24/7.
```bash
python -m flight_analyst.main worker
```

---

## ✨ Funcionalidades Principais (v3)

### 📊 Dashboard Inteligente
- **Gestão de Rotas:** Adicione, edite parâmetros (duração, escalas, flexibilidade) ou exclua rotas diretamente pela interface.
- **Coleta On-Demand:** Botões para **"🚀 Coletar Preços Agora"** (individual) ou **"🚀 Coletar Todas as Rotas"** (global) que disparam os scrapers em tempo real.
- **Arquitetura Assíncrona:** A coleta roda em **Background Tasks** (TaskManager próprio). Você pode continuar navegando enquanto o sistema busca os preços.
- **Gráficos Dinâmicos:** Histórico de preços e recomendações geradas por IA.

### 🤖 IA & Inteligência
- **Context-Aware Ask:** Converse com o Gemini sobre uma rota específica. A IA consulta o banco de dados sob demanda para responder (MCP Pattern).
- **Opportunity Score:** Algoritmo que classifica cada preço como "COMPRE AGORA", "BOA OPORTUNIDADE", etc.

### ⚙️ Engine de Coleta
- **Cascata de Scrapers:** Playwright (Chromium) ➔ SerpApi (Google Flights) ➔ Amadeus API.
- **Circuit Breaker:** Sistema automático que pausa scrapers ou rotas que estão apresentando erros consecutivos para evitar bloqueios.
- **Frequência Configurável:** Defina de quanto em quanto tempo (minutos) cada rota deve ser verificada pelo Worker.

---

## 🛠️ Utilizando a API (Postman / Insomnia)

A API é protegida por `X-API-Key`. Endpoints principais:

- `GET /routes`: Lista rotas ativas.
- `POST /collect-all`: Dispara coleta global para todas as rotas ativas em background.
- `POST /routes/{id}/collect`: Dispara coleta em background (retorna `task_id`).
- `GET /tasks/{task_id}`: Consulta status e resultado de uma coleta.
- `PATCH /routes/{id}`: Atualiza parâmetros da rota.
- `POST /routes/{id}/ask`: Chat com IA sobre a rota.

---

## 🏗️ Estrutura do Projeto
- `flight_analyst/api/`: Servidor FastAPI e TaskManager.
- `flight_analyst/dashboard/`: Interface Streamlit.
- `flight_analyst/domain/`: Modelos Pydantic e regras de negócio.
- `flight_analyst/infra/`: Scrapers, Repositórios e Clientes de BD.
- `flight_analyst/application/`: Serviços de orquestração e IA.

---

## 🧹 Manutenção
O sistema possui uma rotina de **Data Retention** que apaga automaticamente snapshots com mais de **180 dias**, mantendo o banco de dados leve.
