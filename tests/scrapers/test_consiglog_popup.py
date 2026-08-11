"""Modais do ConsigLog/ConsigX que bloqueiam a coleta.

1. "Usuário já logado" (`ucAjaxModalPopupConfirmacao1`): esperar o modal AJAX
   renderizar antes de decidir que não há popup — ``is_visible()`` não espera.
2. AVISO institucional (`ucAjaxModalPopup1`, ConsigX 08/2026): cobre a tela e
   impede a tabela de prazos de ficar visível. Deve ser fechado no pós-login,
   na seleção de órgão e a cada tentativa de extração.

Page falsa mapeia seletor → botão, para cada modal ter visibilidade própria.
"""
from unittest.mock import MagicMock

from app.scrapers.consiglog.scraper import (
    ConsiglogScraper,
    _BTN_AVISO,
    _BTN_SESSAO_ANTERIOR,
)


class _FakeBtn:
    def __init__(self, *, visivel_apos_espera: bool) -> None:
        self._apos_espera = visivel_apos_espera
        self.clicked = False

    def is_visible(self, timeout=None) -> bool:
        return False  # imediato, sem esperar — modelo do bug antigo

    def wait_for(self, state=None, timeout=None) -> None:
        if not self._apos_espera:
            raise TimeoutError("popup não apareceu no prazo")

    def click(self, **kwargs) -> None:
        self.clicked = True


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakePage:
    def __init__(self, botoes: dict) -> None:
        self._botoes = botoes
        self._ausente = _FakeBtn(visivel_apos_espera=False)
        self.url = "https://saec.consigx.com.br/LoginSegundaEtapa.aspx"

    def locator(self, selector: str) -> _FakeBtn:
        return self._botoes.get(selector, self._ausente)

    def expect_navigation(self, **kwargs) -> _Ctx:
        return _Ctx()

    def wait_for_load_state(self, *args, **kwargs) -> None:
        pass

    def wait_for_timeout(self, ms) -> None:
        pass


def _scraper(botoes: dict) -> ConsiglogScraper:
    s = ConsiglogScraper(
        processadora_config={},
        convenio_config={"processadora": "consiglog", "base_url": "http://x"},
        auth_strategy=MagicMock(),  # super().authenticate() vira no-op
    )
    s.page = _FakePage(botoes)
    return s


# ── Modal "Usuário já logado" ─────────────────────────────────────────────────

def test_popup_de_sessao_que_renderiza_tarde_e_confirmado():
    btn = _FakeBtn(visivel_apos_espera=True)
    _scraper({_BTN_SESSAO_ANTERIOR: btn}).authenticate()
    assert btn.clicked is True


def test_sem_popups_nao_clica_e_nao_levanta():
    sessao = _FakeBtn(visivel_apos_espera=False)
    aviso = _FakeBtn(visivel_apos_espera=False)
    _scraper({_BTN_SESSAO_ANTERIOR: sessao, _BTN_AVISO: aviso}).authenticate()
    assert sessao.clicked is False
    assert aviso.clicked is False


# ── Modal de AVISO institucional (ConsigX) ────────────────────────────────────

def test_aviso_no_pos_login_e_fechado():
    aviso = _FakeBtn(visivel_apos_espera=True)
    _scraper({_BTN_AVISO: aviso}).authenticate()
    assert aviso.clicked is True


def test_aviso_e_sessao_juntos_fecha_os_dois():
    aviso = _FakeBtn(visivel_apos_espera=True)
    sessao = _FakeBtn(visivel_apos_espera=True)
    _scraper({_BTN_AVISO: aviso, _BTN_SESSAO_ANTERIOR: sessao}).authenticate()
    assert aviso.clicked is True
    assert sessao.clicked is True


def test_fechar_aviso_retorna_false_sem_modal_e_true_com_modal():
    sem = _scraper({})
    assert sem._fechar_aviso("teste") is False

    aviso = _FakeBtn(visivel_apos_espera=True)
    com = _scraper({_BTN_AVISO: aviso})
    assert com._fechar_aviso("teste") is True
    assert aviso.clicked is True


def test_falha_ao_clicar_no_aviso_nao_derruba_a_coleta():
    class _BtnQueFalhaNoClique(_FakeBtn):
        def __init__(self):
            super().__init__(visivel_apos_espera=True)

        def click(self, **kwargs):
            raise RuntimeError("elemento interceptado")

    s = _scraper({_BTN_AVISO: _BtnQueFalhaNoClique()})
    assert s._fechar_aviso("teste") is False  # não levanta
