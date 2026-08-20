"""Fixtures globais dos testes.

Rate limit DESLIGADO por padrão na suíte: a suíte inteira sai do mesmo IP
("testclient") e faria dezenas de POSTs sensíveis dentro da janela, batendo no
limite e derrubando testes não relacionados. Os testes de rate limit religam
localmente (patch.object) e resetam o estado.
"""
from __future__ import annotations

import pytest

from app.api import security
from app.core.settings import settings


@pytest.fixture(autouse=True)
def _rate_limit_desligado_por_padrao():
    original = settings.RATE_LIMIT_ENABLED
    settings.RATE_LIMIT_ENABLED = False
    security._reset_rate_limit_state()
    yield
    settings.RATE_LIMIT_ENABLED = original
    security._reset_rate_limit_state()
