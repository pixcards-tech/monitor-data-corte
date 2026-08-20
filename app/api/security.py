"""Middlewares de segurança: rate limit por IP + cabeçalhos de segurança.

Sem dependências externas — o limiter vive em memória, é bounded e thread-safe (os
handlers sync do FastAPI rodam em threadpool). O serviço é single-process (scheduler
in-process), então estado em memória basta. Se um dia escalar horizontal, trocar o
_JanelaFixa por um backend compartilhado (Redis) mantendo a mesma interface `allow`.

Config vem de settings e é lida a CADA request — dá pra ligar/desligar e reajustar
limites por ambiente (e os testes conseguem patchar) sem recriar o app.
"""
from __future__ import annotations

import threading
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.settings import settings

# Rotas que nunca sofrem rate limit — uptime / dead-man's switch pinga sem parar.
_EXEMPT_PATHS = frozenset({"/health"})
_METODOS_MUTACAO = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class _JanelaFixa:
    """Contador de janela fixa por chave (IP), bounded e protegido por lock.

    `allow(key, agora, limite, janela)` → (permitido, retry_after_s). Ao virar a
    janela, o contador zera. O dict é limitado a `max_keys` entradas (descarta a mais
    antiga) para não virar vetor de DoS de memória via spray de IPs forjados.
    """

    def __init__(self, max_keys: int = 50_000) -> None:
        self._max = max_keys
        self._hits: dict[str, list] = {}  # key -> [contagem, janela_inicio_monotonic]
        self._lock = threading.Lock()

    def allow(self, key: str, agora: float, limite: int, janela: float) -> tuple[bool, int]:
        with self._lock:
            entry = self._hits.get(key)
            if entry is None or agora - entry[1] >= janela:
                if key not in self._hits and len(self._hits) >= self._max:
                    self._hits.pop(next(iter(self._hits)))  # evicta a entrada mais antiga
                self._hits[key] = [1, agora]
                return True, 0
            entry[0] += 1
            if entry[0] > limite:
                retry = janela - (agora - entry[1])
                return False, max(1, int(retry) + 1)
            return True, 0

    def reset(self) -> None:
        """Zera o estado (usado nos testes para isolar cada caso)."""
        with self._lock:
            self._hits.clear()


# Dois baldes com namespaces separados: leitura (folgado) e sensível (auth + mutações).
_geral = _JanelaFixa()
_sensivel = _JanelaFixa()


def _rota_sensivel(request: Request) -> bool:
    return request.url.path.startswith("/auth/") or request.method in _METODOS_MUTACAO


def _client_ip(request: Request) -> str:
    """IP do cliente. Só lê X-Forwarded-For quando TRUST_PROXY — senão o cliente
    forjaria o header e escaparia do limite trocando o IP a cada request."""
    if settings.TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "desconhecido"


async def rate_limit_middleware(request: Request, call_next):
    if (not settings.RATE_LIMIT_ENABLED
            or request.method == "OPTIONS"
            or request.url.path in _EXEMPT_PATHS):
        return await call_next(request)

    sensivel = _rota_sensivel(request)
    store = _sensivel if sensivel else _geral
    limite = settings.RATE_LIMIT_SENSIVEL if sensivel else settings.RATE_LIMIT_GERAL
    ok, retry = store.allow(_client_ip(request), time.monotonic(), limite, settings.RATE_LIMIT_WINDOW_S)
    if not ok:
        return JSONResponse(
            {"detail": "Muitas requisições. Tente novamente em instantes."},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )
    return await call_next(request)


async def security_headers_middleware(request: Request, call_next):
    resp: Response = await call_next(request)
    if not settings.SECURITY_HEADERS:
        return resp
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    if settings.CONTENT_SECURITY_POLICY:
        h.setdefault("Content-Security-Policy", settings.CONTENT_SECURITY_POLICY)
    # HSTS só faz sentido sob HTTPS — COOKIE_SECURE é o nosso sinal de "estou atrás de TLS".
    if settings.COOKIE_SECURE:
        h.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return resp


def _reset_rate_limit_state() -> None:
    """Hook de teste: zera os dois baldes entre casos."""
    _geral.reset()
    _sensivel.reset()
