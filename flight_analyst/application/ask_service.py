"""flight_analyst/application/ask_service.py
Módulo Ask — Integração com Google Gemini para responder a perguntas em linguagem natural.
Fornece contexto de mercado (estatísticas e recomendações) como base para a IA.
"""

from decimal import Decimal
from typing import Any

import google.generativeai as genai
import structlog
from pydantic import BaseModel

from flight_analyst.config import settings
from flight_analyst.domain.models import Recommendation, Route
from flight_analyst.application.ai_tools import AVAILABLE_TOOLS

log = structlog.get_logger(__name__)



class AskResponse(BaseModel):
    """Resposta estruturada e limpa do motor de IA."""
    verdict: str  # Comprar, Esperar, Alerta
    analysis: str # Breve explicação técnica
    recommendation: str # Sugestão final
    tokens_used: int = 0
    model: str = "gemini-2.5-flash"


class AskService:
    """
    Serviço que orquestra perguntas em linguagem natural para o Google Gemini.
    Usa os dados do banco para dar contexto de mercado antes de responder.
    """

    def __init__(self) -> None:
        self._is_ready = False
        if settings.has_gemini:
            genai.configure(api_key=settings.google_api_key)
            # Usando o modelo estável mais recente
            self._model = genai.GenerativeModel("gemini-2.5-flash")
            self._is_ready = True
        else:
            log.warning("gemini_not_configured", reason="GOOGLE_API_KEY ausente")

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    def _build_context(
        self,
        route: Route,
        recommendation: Recommendation | None,
    ) -> str:
        """
        Monta as instruções base para a IA.
        """
        import json
        
        context: dict[str, Any] = {
            "route_info": {
                "id": str(route.id),
                "origin": route.origin,
                "destination": route.destination,
                "target_month": route.travel_month.strftime("%Y-%m"),
                "duration_days": route.target_duration_days,
                "currency": route.currency.value,
            }
        }

        if recommendation:
            context["analysis"] = {
                "opportunity_score": recommendation.opportunity_score,
                "is_error_fare": recommendation.is_error_fare,
                "system_recommendation": recommendation.recommendation_text,
            }

        prompt = f"""
Você é o "Flight Analyst", um especialista financeiro em passagens aéreas.
O usuário está consultando a rota abaixo. Se a pergunta for sobre preços ou histórico, USE SUAS FERRAMENTAS (TOOLS) para consultar o banco de dados dinamicamente usando o ID da rota fornecido.
NÃO INVENTE PREÇOS. Use as ferramentas.

Responda SEMPRE em formato JSON estruturado (sem blocos Markdown em volta) com os seguintes campos:
- "verdict": Uma palavra (Comprar, Esperar ou Alerta).
- "analysis": Uma frase curta explicando o motivo técnico.
- "recommendation": A sua sugestão amigável para o usuário.

DADOS DA ROTA (Use este ID para as ferramentas):
{json.dumps(context, indent=2, ensure_ascii=False)}
"""
        return prompt

    async def ask(
        self,
        question: str,
        route: Route,
        recommendation: Recommendation | None = None,
    ) -> AskResponse:
        """
        Envia uma pergunta ao Gemini contextualizada com os dados do voo.
        A IA usará as ferramentas (tools) para buscar o histórico de preços.
        """
        import json
        
        if not self._is_ready:
            return AskResponse(
                verdict="Erro",
                analysis="Módulo Ask desativado",
                recommendation="Configure a GOOGLE_API_KEY no .env",
                model="offline"
            )

        system_instruction = self._build_context(route, recommendation)
        
        try:
            model = genai.GenerativeModel(
                model_name="gemini-2.5-flash",
                system_instruction=system_instruction,
                tools=AVAILABLE_TOOLS
            )
            
            # Usamos o start_chat para permitir a comunicação multi-turn do Function Calling
            chat = model.start_chat(enable_automatic_function_calling=True)
            
            # Avisamos ao modelo que queremos o resultado final em JSON
            response = await chat.send_message_async(
                question,
            )
            
            # Remove blocos markdown caso a IA os coloque
            raw_text = response.text.strip()
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()
            
            # Parse da resposta JSON do Gemini
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                data = {
                    "verdict": "Alerta",
                    "analysis": "Erro ao processar resposta estruturada",
                    "recommendation": response.text
                }
            
            try:
                tokens = response.usage_metadata.total_token_count
            except Exception:
                tokens = 0
                
            log.info("gemini_asked", route=route.label, tokens=tokens)
            
            return AskResponse(
                verdict=data.get("verdict", "Indefinido"),
                analysis=data.get("analysis", ""),
                recommendation=data.get("recommendation", ""),
                tokens_used=tokens,
                model="gemini-2.5-flash",
            )
            
        except Exception as exc:
            log.error("gemini_error", error=str(exc))
            return AskResponse(
                verdict="Erro",
                analysis="Falha na comunicação com a IA",
                recommendation=str(exc),
                model="error"
            )
