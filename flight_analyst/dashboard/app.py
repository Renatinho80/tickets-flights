"""flight_analyst/dashboard/app.py
Dashboard interativo usando Streamlit e Plotly.
Permite visualizar rotas, histórico de preços e recomendações do motor de IA.
"""

import sys
import os
# Adiciona o diretório raiz do projeto ao sys.path para permitir imports do pacote flight_analyst
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

import asyncio
from datetime import datetime, date
from uuid import UUID
import airportsdata

import pandas as pd
import plotly.express as px
import streamlit as st

# Configuração da Página
st.set_page_config(
    page_title="Flight Analyst",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="expanded",
)

import requests
from flight_analyst.config import settings

API_BASE_URL = settings.effective_api_base_url
HEADERS = {"X-API-Key": settings.app_api_key}

@st.cache_data(ttl=300)
def fetch_routes():
    try:
        response = requests.get(f"{API_BASE_URL}/routes", headers=HEADERS)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        st.error(f"Erro ao buscar rotas da API: {e}")
        return []

@st.cache_data(ttl=300)
def fetch_snapshots(route_id: str):
    try:
        response = requests.get(f"{API_BASE_URL}/routes/{route_id}/snapshots", headers=HEADERS)
        response.raise_for_status()
        return response.json()
    except Exception:
        return []

@st.cache_data(ttl=300)
def fetch_latest_recommendation(route_id: str):
    try:
        response = requests.get(f"{API_BASE_URL}/routes/{route_id}/recommendations", headers=HEADERS)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None

@st.cache_data(ttl=300)
def fetch_all_recommendations(routes: list) -> list:
    """Busca recomendações de todas as rotas para o ranking."""
    results = []
    for route in routes:
        rec = fetch_latest_recommendation(route["id"])
        if rec:
            results.append({
                "Rota": route.get("label", f"{route['origin']}→{route['destination']}"),
                "Preço Atual": rec.get("current_price"),
                "Média 30d": rec.get("avg_price_30d"),
                "Score": rec.get("opportunity_score", 0),
                "Status": rec.get("recommendation_text", "")[:60] + "...",
                "Error Fare": "🚨" if rec.get("is_error_fare") else "✅",
            })
    return sorted(results, key=lambda x: x["Score"], reverse=True)

def best_weekday_analysis(snapshots: list) -> dict | None:
    """Analisa snapshots para identificar o dia da semana com preços historicamente menores."""
    if len(snapshots) < 7:
        return None
    try:
        df = pd.DataFrame([{"price": float(s["price"]), "scraped_at": pd.to_datetime(s["scraped_at"])} for s in snapshots])
        dias = {
            0: "Segunda", 1: "Terça", 2: "Quarta", 
            3: "Quinta", 4: "Sexta", 5: "Sábado", 6: "Domingo"
        }
        df["weekday"] = df["scraped_at"].apply(lambda x: x.weekday()).map(dias)
        avg_by_day = df.groupby("weekday")["price"].mean().sort_values()
        best_day = avg_by_day.index[0]
        worst_day = avg_by_day.index[-1]
        savings_pct = ((avg_by_day.iloc[-1] - avg_by_day.iloc[0]) / avg_by_day.iloc[-1]) * 100
        return {"best_day": best_day, "worst_day": worst_day, "savings_pct": savings_pct, "by_day": avg_by_day.to_dict()}
    except Exception:
        return None

def run_ask(question: str, route_id: str):
    try:
        response = requests.post(
            f"{API_BASE_URL}/routes/{route_id}/ask",
            headers=HEADERS,
            json={"question": question}
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {
            "verdict": "Erro",
            "analysis": "Falha na comunicação",
            "recommendation": str(e),
            "tokens_used": 0,
            "model": "error"
        }

@st.cache_data
def get_airports_list():
    try:
        airports = airportsdata.load('IATA')
        options = []
        mapping = {}
        for code, data in airports.items():
            if len(code) == 3:
                label = f"({code}) {data.get('city', '')}, {data.get('country', '')} - {data.get('name', '')}"
                options.append(label)
                mapping[label] = code
        return sorted(options), mapping
    except Exception as e:
        st.error(f"Erro ao carregar lista de aeroportos: {e}")
        return [], {}

def add_route_api(route_data):
    try:
        response = requests.post(f"{API_BASE_URL}/routes", headers=HEADERS, json=route_data)
        if response.status_code == 201:
            st.sidebar.success("Rota adicionada com sucesso!")
            st.cache_data.clear()
            st.rerun()
        elif response.status_code == 409:
            st.sidebar.error("Esta rota já existe.")
        else:
            st.sidebar.error(f"Erro ao adicionar rota: {response.text}")
    except Exception as e:
        st.sidebar.error(f"Falha de conexão: {e}")

def update_route_api(route_id, update_data):
    try:
        response = requests.patch(f"{API_BASE_URL}/routes/{route_id}", headers=HEADERS, json=update_data)
        if response.status_code == 200:
            st.sidebar.success("Rota atualizada!")
            st.cache_data.clear()
            st.rerun()
        else:
            st.sidebar.error(f"Erro ao atualizar rota: {response.text}")
    except Exception as e:
        st.sidebar.error(f"Falha de conexão: {e}")

def delete_route_api(route_id):
    try:
        response = requests.delete(f"{API_BASE_URL}/routes/{route_id}", headers=HEADERS)
        if response.status_code == 204:
            st.sidebar.success("Rota deletada com sucesso!")
            st.cache_data.clear()
            st.rerun()
        else:
            st.sidebar.error(f"Erro ao deletar rota: {response.text}")
    except Exception as e:
        st.sidebar.error(f"Falha de conexão: {e}")

def collect_prices_api(route_id):
    """
    Dispara coleta em background e faz polling de status.
    Timeout de segurança de 120 segundos para nunca travar a interface.
    """
    import time

    POLL_INTERVAL = 3      # segundos entre cada verificação
    MAX_WAIT = 120         # timeout máximo de segurança

    try:
        # 1. Dispara a coleta (retorna imediatamente com task_id)
        response = requests.post(
            f"{API_BASE_URL}/routes/{route_id}/collect",
            headers=HEADERS,
            timeout=10,
        )
        if response.status_code != 202:
            st.sidebar.error(f"Erro ao iniciar coleta: {response.text}")
            return
        
        task_id = response.json().get("task_id")
        if not task_id:
            st.sidebar.error("Resposta inesperada da API (sem task_id).")
            return

        # 2. Polling de status até conclusão ou timeout
        elapsed = 0
        while elapsed < MAX_WAIT:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            try:
                status_resp = requests.get(
                    f"{API_BASE_URL}/tasks/{task_id}",
                    headers=HEADERS,
                    timeout=10,
                )
                if status_resp.status_code != 200:
                    continue  # Ignora falha transiente e tenta novamente

                data = status_resp.json()
                task_status = data.get("status")

                if task_status == "done":
                    st.sidebar.success(
                        f"✅ Coleta concluída! {data['snapshots_count']} preços "
                        f"via {data['source']} em {data['duration_seconds']:.0f}s."
                    )
                    st.cache_data.clear()
                    st.rerun()
                    return

                if task_status == "failed":
                    st.sidebar.error(f"Falha na coleta: {data.get('error_message', 'Erro desconhecido')}")
                    return

            except requests.exceptions.RequestException:
                continue  # Rede instável — tenta de novo no próximo ciclo

        # 3. Timeout atingido
        st.sidebar.warning("⏱️ Tempo limite excedido. A coleta pode ainda estar em andamento no servidor.")

    except requests.exceptions.ConnectionError:
        st.sidebar.error("API fora do ar. Verifique se o backend está rodando.")
    except Exception as e:
        st.sidebar.error(f"Erro inesperado: {e}")

def collect_all_prices_api():
    """
    Dispara coleta global e faz polling para todas as tasks criadas.
    """
    import time
    
    POLL_INTERVAL = 3
    MAX_WAIT = 300  # Timeout maior para coleta global (5 minutos)
    
    try:
        response = requests.post(f"{API_BASE_URL}/collect-all", headers=HEADERS, timeout=10)
        if response.status_code != 202:
            st.sidebar.error(f"Erro ao iniciar coleta global: {response.text}")
            return
            
        data = response.json()
        tasks = data.get("tasks", [])
        
        if not tasks:
            st.sidebar.info("Nenhuma rota ativa encontrada para coletar.")
            return
            
        pending_tasks = {t["task_id"] for t in tasks}
        success_count = 0
        
        elapsed = 0
        while elapsed < MAX_WAIT and pending_tasks:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            
            # Copiamos o set porque vamos modificá-lo no loop
            for task_id in list(pending_tasks):
                try:
                    status_resp = requests.get(f"{API_BASE_URL}/tasks/{task_id}", headers=HEADERS, timeout=10)
                    if status_resp.status_code != 200:
                        continue
                        
                    task_data = status_resp.json()
                    task_status = task_data.get("status")
                    
                    if task_status == "done":
                        success_count += 1
                        pending_tasks.remove(task_id)
                    elif task_status == "failed":
                        pending_tasks.remove(task_id)
                        
                except requests.exceptions.RequestException:
                    continue
                    
        if not pending_tasks:
            st.sidebar.success(f"✅ Coleta global finalizada! {success_count} rotas atualizadas.")
        else:
            st.sidebar.warning(f"⏱️ Tempo limite excedido. Faltam {len(pending_tasks)} rotas terminarem.")
            
        st.cache_data.clear()
        st.rerun()

    except requests.exceptions.ConnectionError:
        st.sidebar.error("API fora do ar.")
    except Exception as e:
        st.sidebar.error(f"Erro inesperado: {e}")

# -----------------------------------------------------------------------------
# Interface Principal
# -----------------------------------------------------------------------------

def login_screen():
    from flight_analyst.config import settings
    
    st.title("🔒 Acesso Restrito")
    st.markdown("Por favor, insira a senha do dashboard para continuar.")
    
    pwd = st.text_input("Senha", type="password")
    if st.button("Entrar"):
        if pwd == settings.dashboard_password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Senha incorreta.")
            
def main():
    # Sistema de Autenticação Simples
    if not st.session_state.get("authenticated", False):
        login_screen()
        return

    st.title("✈️ Flight Analyst Dashboard")
    st.markdown("Inteligência e monitoramento de passagens aéreas.")
    
    st.sidebar.markdown("---")
    # Adicionando cache clear button na sidebar
    if st.sidebar.button("🔄 Recarregar Visualização", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    if st.sidebar.button("🚀 Coletar Todas as Rotas", type="primary", use_container_width=True, help="Dispara a coleta manual para todas as rotas ativas."):
        with st.spinner("Coletando preços de todas as rotas... Isso pode levar alguns minutos."):
            collect_all_prices_api()
            
    st.sidebar.markdown("---")
    
    routes = fetch_routes()
    
    # Sidebar
    st.sidebar.header("🗺️ Suas Rotas")

    # --- Adicionar Rota ---
    airport_options, airport_mapping = get_airports_list()
    
    with st.sidebar.expander("➕ Adicionar Nova Rota", expanded=False):
        with st.form("add_route_form"):
            origin_label = st.selectbox("Origem", options=airport_options, index=0)
            destination_label = st.selectbox("Destino", options=airport_options, index=0)
            
            travel_date = st.date_input("Mês da Viagem", min_value=date.today())
            duration = st.number_input("Duração (Dias)", min_value=1, max_value=90, value=14)
            poll_interval = st.number_input("Frequência de Busca (Minutos)", min_value=15, max_value=1440, value=60, help="Tempo entre as buscas automatizadas pelo Worker.")
            
            submitted = st.form_submit_button("Adicionar Rota")
            if submitted:
                if origin_label and destination_label:
                    origin_iata = airport_mapping.get(origin_label)
                    dest_iata = airport_mapping.get(destination_label)
                    
                    if origin_iata == dest_iata:
                        st.error("Origem e destino não podem ser iguais.")
                    else:
                        route_data = {
                            "origin": origin_iata,
                            "destination": dest_iata,
                            "travel_month": travel_date.replace(day=1).isoformat(),
                            "target_duration_days": duration,
                            "flexibility_days": 3,
                            "max_stops": 1,
                            "currency": "BRL",
                            "poll_interval_minutes": poll_interval
                        }
                        add_route_api(route_data)
    
    if not routes:
        st.warning("Nenhuma rota ativa encontrada. Utilize o painel ao lado para adicionar sua primeira rota.")
        return
    
    # parse da data para exibir bonito
    route_options = {f"{r['label']} ({r['travel_month'][:7]})": r for r in routes}
    selected_route_key = st.sidebar.selectbox("Selecione a Rota", list(route_options.keys()))
    route = route_options[selected_route_key]
    
    st.sidebar.divider()
    st.sidebar.markdown(f"**Origem:** {route['origin']}")
    st.sidebar.markdown(f"**Destino:** {route['destination']}")
    st.sidebar.markdown(f"**Duração:** {route['target_duration_days']} dias")
    st.sidebar.markdown(f"**Moeda:** {route['currency']}")
    
    # --- Gerenciar Rota ---
    with st.sidebar.expander("⚙️ Gerenciar Rota", expanded=False):
        st.write("Editar Parâmetros:")
        with st.form(f"edit_route_form_{route['id']}"):
            new_duration = st.number_input("Duração (Dias)", min_value=1, max_value=90, value=route.get("target_duration_days", 14))
            new_flexibility = st.number_input("Flexibilidade (Dias)", min_value=0, max_value=14, value=route.get("flexibility_days", 3))
            new_max_stops = st.number_input("Escalas Máximas", min_value=0, max_value=3, value=route.get("max_stops", 1))
            new_poll = st.number_input("Frequência (Minutos)", min_value=15, max_value=1440, value=route.get("poll_interval_minutes", 60))
            new_status = st.checkbox("Rota Ativa", value=route.get("is_active", True))
            
            submitted_edit = st.form_submit_button("Salvar Alterações")
            if submitted_edit:
                update_data = {
                    "target_duration_days": int(new_duration),
                    "flexibility_days": int(new_flexibility),
                    "max_stops": int(new_max_stops),
                    "poll_interval_minutes": int(new_poll),
                    "is_active": bool(new_status)
                }
                update_route_api(route["id"], update_data)
                
        st.write("---")
        st.write("Ações Rápidas:")
        if st.button("🚀 Coletar Preços Agora", type="secondary", use_container_width=True):
            with st.spinner("Acionando scrapers... Aguarde."):
                collect_prices_api(route["id"])

        st.write("---")
        st.write("Excluir Permanentemente:")
        # Uso de um botão simples para deleção (com cor vermelha primária)
        if st.button("❌ Excluir Rota", type="primary", use_container_width=True):
            delete_route_api(route["id"])
    
    # Dados Principais
    snapshots = fetch_snapshots(route["id"])
    recommendation = fetch_latest_recommendation(route["id"])
    
    # ------------------------------------------------
    # Ranking de Oportunidades (todas as rotas)
    # ------------------------------------------------
    all_routes = fetch_routes()
    if len(all_routes) > 1:
        with st.expander("🏆 Ranking de Oportunidades (Todas as Rotas)", expanded=False):
            ranking = fetch_all_recommendations(all_routes)
            if ranking:
                rank_df = pd.DataFrame(ranking)
                rank_df.index = range(1, len(rank_df) + 1)
                st.dataframe(
                    rank_df.style.background_gradient(subset=["Score"], cmap="RdYlGn", vmin=0, vmax=100),
                    use_container_width=True
                )
            else:
                st.info("Nenhuma recomendação disponível ainda. Colete preços primeiro.")
    
    if not snapshots:
        st.info("Nenhum preço coletado para esta rota ainda.")
        return

    # Invertemos para ficar em ordem cronológica pro gráfico
    snapshots = list(reversed(snapshots))
    current_price = float(snapshots[-1]["price"])
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric(label="Preço Atual", value=f"{route['currency']} {current_price:,.0f}")
        
    with col2:
        if recommendation and recommendation.get("avg_price_30d"):
            avg = float(recommendation["avg_price_30d"])
            diff = ((current_price - avg) / avg) * 100
            st.metric(label="Média (30d)", value=f"{route['currency']} {avg:,.0f}", delta=f"{diff:.1f}% vs Média", delta_color="inverse")
        else:
            st.metric(label="Média (30d)", value="Calculando...")
            
    with col3:
        if recommendation:
            score = recommendation.get("opportunity_score", "N/A")
            st.metric(label="Opportunity Score", value=f"{score}/100")
        else:
            st.metric(label="Opportunity Score", value="N/A")
            
    with col4:
        if recommendation and recommendation.get("is_error_fare"):
            st.error("🚨 ERROR FARE DETECTADO")
        else:
            st.success("✅ Mercado Normal")
            
    st.divider()

    # Gráfico de Tendência (Plotly)
    st.subheader("📈 Histórico de Preços")
    
    if len(snapshots) > 1:
        df = pd.DataFrame([{
            "Data": s["scraped_at"],
            "Preço": s["price"],
            "Companhia": s.get("airline", "N/A")
        } for s in snapshots])
        
        fig = px.line(
            df, x="Data", y="Preço",
            title="Evolução de Preços",
            markers=True,
            hover_data=["Companhia"]
        )
        # Linhas de referência (Feature 2)
        if recommendation:
            if avg_30d := recommendation.get("avg_price_30d"):
                fig.add_hline(
                    y=float(avg_30d), line_dash="dash", line_color="orange",
                    annotation_text=f"Média 30d: {route['currency']} {float(avg_30d):,.0f}",
                    annotation_position="bottom right"
                )
            if hist_avg := recommendation.get("avg_price_historical"):
                fig.add_hline(
                    y=float(hist_avg), line_dash="dot", line_color="gray",
                    annotation_text=f"Média Histórica: {route['currency']} {float(hist_avg):,.0f}",
                    annotation_position="top right"
                )
        fig.update_layout(
            xaxis_title="Data da Coleta",
            yaxis_title=f"Preço ({route['currency']})",
            template="plotly_dark"
        )
        st.plotly_chart(fig, use_container_width=True)

        # Análise de Melhor Dia para Comprar (Feature 5)
        weekday_data = best_weekday_analysis(snapshots)
        if weekday_data:
            st.subheader("📅 Melhor Dia para Pesquisar")
            col_a, col_b = st.columns(2)
            with col_a:
                st.success(f"🟢 **Melhor dia:** {weekday_data['best_day']}")
                st.caption(f"Preços tendem a ser até {weekday_data['savings_pct']:.1f}% mais baratos do que no pior dia")
            with col_b:
                st.warning(f"🔴 **Pior dia:** {weekday_data['worst_day']}")
                st.caption("Evite pesquisar/comprar nesse dia")
            with st.expander("Ver média por dia da semana"):
                day_df = pd.DataFrame(list(weekday_data["by_day"].items()), columns=["Dia", f"Média ({route['currency']})"])
                day_df = day_df.sort_values(f"Média ({route['currency']})")
                st.dataframe(day_df, use_container_width=True)
    else:
        st.info("Gráfico requer pelo menos 2 coletas para ser exibido.")
        
    # IA Analyst (Gemini)
    st.divider()
    st.subheader("🤖 Flight Analyst AI")
    
    if recommendation:
        st.info(f"**Última análise do sistema:** {recommendation.get('recommendation_text', '')}")

    
    st.markdown("Faça uma pergunta específica para a inteligência artificial sobre esta rota:")
    
    question = st.text_input("Sua pergunta:", placeholder="Ex: Acha que esse preço vai cair mais na semana que vem?")
    
    if st.button("Perguntar", type="primary"):
        if question:
            with st.spinner("Analisando mercado (buscando dados sob demanda)..."):
                response = run_ask(question, route["id"])
                
                if response.get("verdict") == "Erro":
                    st.error(f"**Erro:** {response.get('recommendation')}")
                else:
                    st.markdown(f"### Veredito: {response.get('verdict')}")
                    st.markdown(f"**Análise Técnica:** {response.get('analysis')}")
                    st.markdown(f"**Recomendação:** {response.get('recommendation')}")
                    st.caption(f"Tokens usados: {response.get('tokens_used')} | Modelo: {response.get('model')}")
        else:
            st.warning("Digite uma pergunta primeiro.")

if __name__ == "__main__":
    main()
