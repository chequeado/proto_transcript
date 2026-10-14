"""Proxies Webshare (rotating residential) con sticky session.

Usuario `<base>-<país>-<n>`: el mismo n sale siempre por la misma IP mientras dure la sesión
(Proxy Session Timeout en el panel; hoy 8 h, más que las 6 h del manifiesto de YouTube).
Cada corrida de la ingesta usa un n distinto, así que cada reinicio sale por otra IP.
"""

import random
from dataclasses import dataclass

from .config import Settings


@dataclass
class Proxy:
    url: str  # con credenciales: sólo para pasarle a yt-dlp, nunca loguear
    label: str  # para logs, ej. "ar-37"


class ProxyPool:
    def __init__(self, s: Settings):
        self.s = s
        self.mode = s.proxy_mode if s.webshare_user_base and s.webshare_pass else "off"
        self._next = random.randint(1, max(s.webshare_sessions, 1))

    def next(self) -> Proxy:
        n = self._next
        self._next = n % self.s.webshare_sessions + 1
        country = self.s.webshare_country
        user = f"{self.s.webshare_user_base}-{country}-{n}" if country else f"{self.s.webshare_user_base}-{n}"
        url = f"http://{user}:{self.s.webshare_pass}@{self.s.webshare_host}:{self.s.webshare_port}"
        return Proxy(url=url, label=f"{country or 'any'}-{n}")

    def scrub(self, text: str) -> str:
        return text.replace(self.s.webshare_pass, "***") if self.s.webshare_pass else text
