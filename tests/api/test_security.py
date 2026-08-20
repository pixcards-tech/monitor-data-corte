"""Cabeçalhos de segurança, rate limit por IP e o guard fail-closed do boot."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api import security
from app.api.main import _validar_config_seguranca, app
from app.core.settings import settings

client = TestClient(app)


# ── Cabeçalhos de segurança ───────────────────────────────────────────────────

def test_headers_de_seguranca_presentes():
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" in r.headers
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_hsts_so_com_cookie_secure():
    with patch.object(settings, "COOKIE_SECURE", False):
        r = client.get("/health")
    assert "Strict-Transport-Security" not in r.headers
    with patch.object(settings, "COOKIE_SECURE", True):
        r2 = client.get("/health")
    assert r2.headers["Strict-Transport-Security"].startswith("max-age=")


def test_headers_desligaveis():
    with patch.object(settings, "SECURITY_HEADERS", False):
        r = client.get("/health")
    assert "X-Frame-Options" not in r.headers


def test_headers_presentes_mesmo_em_401():
    # Prova a ordem dos middlewares: SecurityHeaders é o mais externo, então carimba
    # até respostas de erro (auth 401) das camadas internas.
    with patch.object(settings, "PANEL_PASSWORD", "segredo"), \
         patch.object(settings, "PANEL_USER", "admin"):
        r = client.get("/convenios")
    assert r.status_code == 401
    assert r.headers["X-Frame-Options"] == "DENY"


def test_headers_presentes_em_429():
    security._reset_rate_limit_state()
    with patch.object(settings, "RATE_LIMIT_ENABLED", True), \
         patch.object(settings, "RATE_LIMIT_GERAL", 1), \
         patch.object(settings, "RATE_LIMIT_WINDOW_S", 60), \
         patch("app.api.main._montar_dados_convenios", return_value=[]):
        client.get("/cortes/atuais")
        bloqueado = client.get("/cortes/atuais")
    assert bloqueado.status_code == 429
    assert bloqueado.headers["X-Content-Type-Options"] == "nosniff"


# ── Rate limit ────────────────────────────────────────────────────────────────

def _hammer(path, n, method="post"):
    fn = getattr(client, method)
    return [fn(path).status_code for _ in range(n)]


def test_rate_limit_sensivel_bloqueia_apos_limite():
    security._reset_rate_limit_state()
    with patch.object(settings, "RATE_LIMIT_ENABLED", True), \
         patch.object(settings, "RATE_LIMIT_SENSIVEL", 3), \
         patch.object(settings, "RATE_LIMIT_WINDOW_S", 60), \
         patch.object(settings, "PANEL_PASSWORD", ""), \
         patch.object(settings, "SMTP_HOST", ""):
        # PANEL_PASSWORD vazio = sem auth, então /notification/testar (POST, bucket
        # sensível) responde 422 rápido; o 4º estoura o limite → 429.
        codigos = _hammer("/notification/testar", 4)
    assert codigos[:3] == [422, 422, 422]
    assert codigos[3] == 429


def test_rate_limit_retorna_retry_after():
    security._reset_rate_limit_state()
    with patch.object(settings, "RATE_LIMIT_ENABLED", True), \
         patch.object(settings, "RATE_LIMIT_SENSIVEL", 1), \
         patch.object(settings, "RATE_LIMIT_WINDOW_S", 60), \
         patch.object(settings, "SMTP_HOST", ""):
        client.post("/notification/testar")
        bloqueado = client.post("/notification/testar")
    assert bloqueado.status_code == 429
    assert int(bloqueado.headers["Retry-After"]) >= 1


def test_health_isento_de_rate_limit():
    security._reset_rate_limit_state()
    with patch.object(settings, "RATE_LIMIT_ENABLED", True), \
         patch.object(settings, "RATE_LIMIT_GERAL", 2), \
         patch.object(settings, "RATE_LIMIT_WINDOW_S", 60):
        codigos = _hammer("/health", 6, method="get")
    assert codigos == [200] * 6


def test_leitura_e_mutacao_usam_baldes_separados():
    security._reset_rate_limit_state()
    with patch.object(settings, "RATE_LIMIT_ENABLED", True), \
         patch.object(settings, "RATE_LIMIT_GERAL", 2), \
         patch.object(settings, "RATE_LIMIT_SENSIVEL", 50), \
         patch.object(settings, "RATE_LIMIT_WINDOW_S", 60), \
         patch.object(settings, "PANEL_PASSWORD", ""), \
         patch.object(settings, "SMTP_HOST", ""), \
         patch("app.api.main._montar_dados_convenios", return_value=[]):
        # Estoura o balde de LEITURA (limite 2) …
        leitura = _hammer("/cortes/atuais", 3, method="get")
        # … e o POST (balde sensível, limite 50) segue passando.
        mut = client.post("/notification/testar").status_code
    assert leitura[2] == 429
    assert mut == 422  # não foi afetado pelo estouro do balde de leitura


# ── Guard fail-closed do boot ─────────────────────────────────────────────────

def test_boot_recusa_auth_required_sem_senha():
    with patch.object(settings, "AUTH_REQUIRED", True), \
         patch.object(settings, "PANEL_PASSWORD", ""):
        with pytest.raises(RuntimeError, match="PANEL_PASSWORD"):
            _validar_config_seguranca()


def test_boot_ok_com_auth_required_e_senha():
    with patch.object(settings, "AUTH_REQUIRED", True), \
         patch.object(settings, "PANEL_PASSWORD", "segredo-forte"):
        _validar_config_seguranca()  # não levanta


def test_boot_ok_sem_auth_required():
    with patch.object(settings, "AUTH_REQUIRED", False), \
         patch.object(settings, "PANEL_PASSWORD", ""):
        _validar_config_seguranca()  # dev local: segue aberto, sem erro
