"""flight_analyst/api/rate_limiter.py
Rate limiting para os endpoints críticos da API.
Protege contra chamadas excessivas que podem esgotar créditos de SerpApi/Amadeus.
"""

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Limita por IP do cliente
limiter = Limiter(key_func=get_remote_address)

__all__ = ["limiter", "RateLimitExceeded", "_rate_limit_exceeded_handler"]
