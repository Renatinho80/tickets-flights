"""flight_analyst/application/notification_service.py
Serviço unificado de notificações.
Trata o envio de mensagens para o Telegram e/ou Ntfy.
"""

import httpx
import structlog
from telegram import Bot
from telegram.constants import ParseMode

from flight_analyst.config import settings
from flight_analyst.domain.models import Route, Recommendation

log = structlog.get_logger(__name__)


class NotificationService:
    def __init__(self) -> None:
        self._telegram_bot: Bot | None = None
        if settings.has_telegram:
            self._telegram_bot = Bot(token=settings.telegram_bot_token)
        else:
            log.warning("telegram_not_configured")

    def _format_telegram_message(self, route: Route, recommendation: Recommendation) -> str:
        """Formata a mensagem usando HTML suportado pelo Telegram."""
        title = "🚨 <b>ERROR FARE DETECTADO!</b>" if recommendation.is_error_fare else "🎯 <b>ALERTA DE PREÇO!</b>"
        
        msg = f"{title}\n\n"
        msg += f"✈️ <b>Rota:</b> {route.label}\n"
        msg += f"🗓️ <b>Mês:</b> {route.travel_month.strftime('%b/%Y')}\n"
        
        if recommendation.current_price:
            msg += f"💰 <b>Preço Atual:</b> {route.currency.value} {recommendation.current_price:.0f}\n"
        
        if recommendation.avg_price_30d:
            msg += f"📊 <b>Média (30d):</b> {route.currency.value} {recommendation.avg_price_30d:.0f}\n"
            
        msg += f"\n💡 <b>Análise:</b> {recommendation.recommendation_text}"
        
        return msg

    async def _send_telegram(self, message: str) -> bool:
        if not self._telegram_bot or not settings.telegram_chat_id:
            return False
            
        try:
            await self._telegram_bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
            )
            return True
        except Exception as exc:
            log.error("telegram_send_failed", error=str(exc))
            return False

    async def _send_ntfy(self, route: Route, recommendation: Recommendation) -> bool:
        if not settings.ntfy_topic:
            return False
            
        url = f"https://ntfy.sh/{settings.ntfy_topic}"
        title = f"Alerta: {route.label}"
        if recommendation.is_error_fare:
            title = f"ERROR FARE: {route.label}"
            
        price = f"{route.currency.value} {recommendation.current_price:.0f}" if recommendation.current_price else "N/A"
        message = f"Preço atual: {price}. {recommendation.recommendation_text}"
        
        headers = {
            "Title": title,
            "Tags": "airplane,rotating_light" if recommendation.is_error_fare else "airplane,moneybag",
        }
        
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, data=message.encode("utf-8"), headers=headers, timeout=10.0)
                resp.raise_for_status()
            return True
        except Exception as exc:
            log.error("ntfy_send_failed", error=str(exc))
            return False

    async def notify_opportunity(self, route: Route, recommendation: Recommendation) -> bool:
        """Envia o alerta usando os canais disponíveis."""
        message = self._format_telegram_message(route, recommendation)
        
        success = False
        
        # Tenta enviar via Telegram
        if self._telegram_bot:
            if await self._send_telegram(message):
                success = True
                
        # Fallback/Duplicado no Ntfy
        if await self._send_ntfy(route, recommendation):
            success = True
            
        return success

    async def send_test_message(self, text: str) -> bool:
        """Usado para o CLI testar a configuração."""
        msg = f"🧪 <b>Teste Flight Analyst</b>\n\n{text}"
        return await self._send_telegram(msg)
