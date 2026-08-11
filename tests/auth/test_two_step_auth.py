"""Valida a digitação humana do TwoStepAuthStrategy SEM tocar portal/conta real.

Camada A (sempre roda): Page falsa que grava a sequência de chamadas — prova
que não há ``fill`` instantâneo, que usa ``press_sequentially`` + Tab/blur,
espera o botão habilitar e dá uma pausa (reCAPTCHA) antes de cada clique, na
ordem certa das 2 etapas.

Camada B (integração, pula se o Chromium não iniciar): roda o fluxo real contra
uma página HTML LOCAL que simula o portal de 2 etapas (botão desabilitado até o
blur). Nenhum acesso externo.
"""
from __future__ import annotations

import pytest

from app.auth.two_step_auth import TwoStepAuthStrategy

# Mesmos seletores do ConsigNet (app/core/processadoras.json).
SELECTORS = {
    "step1_username": {"type": "css", "value": "#login-username"},
    "step1_submit": {"type": "css", "value": "#btn-continue"},
    "step2_password": {"type": "css", "value": "#login-password"},
    "step2_submit": {"type": "css", "value": "[id='btn-log in']"},
}


# ─────────────────────────── Camada A: Page falsa ───────────────────────────

class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple] = []


class _FakeLocator:
    def __init__(self, rec: _Rec, selector: str) -> None:
        self._rec = rec
        self._selector = selector

    def wait_for(self, state=None, timeout=None):
        self._rec.events.append(("wait_for", self._selector, state))

    def click(self, **kw):
        self._rec.events.append(("click", self._selector))

    def press_sequentially(self, text, delay=None):
        self._rec.events.append(("press_sequentially", self._selector, text, delay))

    def press(self, key):
        self._rec.events.append(("press", self._selector, key))

    def fill(self, *a, **k):  # pragma: no cover - não deve ser chamado
        self._rec.events.append(("fill", self._selector))
        raise AssertionError("fill() não deve ser usado — digitação robótica")


class _FakePage:
    def __init__(self, rec: _Rec) -> None:
        self._rec = rec
        self.url = "https://portal.local/auth/login"

    def goto(self, url, wait_until=None, timeout=None):
        self._rec.events.append(("goto", url))

    def locator(self, value):
        return _FakeLocator(self._rec, value)

    def get_by_role(self, role, name=None):
        return _FakeLocator(self._rec, f"role={role}:{name}")

    def wait_for_timeout(self, ms):
        self._rec.events.append(("wait_for_timeout", ms))

    def wait_for_load_state(self, state, timeout=None):
        self._rec.events.append(("wait_for_load_state", state))


class _FakeAssertions:
    def __init__(self, rec: _Rec, selector: str) -> None:
        self._rec = rec
        self._selector = selector

    def to_be_enabled(self, timeout=None):
        self._rec.events.append(("to_be_enabled", self._selector))


def _fake_expect_factory(rec: _Rec):
    def _expect(locator):
        return _FakeAssertions(rec, locator._selector)
    return _expect


def _rodar(monkeypatch, **kwargs):
    rec = _Rec()
    page = _FakePage(rec)
    monkeypatch.setattr("app.auth.two_step_auth.expect", _fake_expect_factory(rec))
    strat = TwoStepAuthStrategy(
        "user123", "secret456", SELECTORS,
        key_delay_ms=1, blur_settle_ms=7, recaptcha_settle_ms=11, enable_timeout_ms=4000,
        **kwargs,
    )
    strat.authenticate(page, "https://portal.local/auth/login", timeout=5000)
    return rec.events


def test_nunca_usa_fill(monkeypatch):
    events = _rodar(monkeypatch)
    assert "fill" not in [e[0] for e in events]


def test_digita_tecla_a_tecla_username_e_senha(monkeypatch):
    events = _rodar(monkeypatch)
    seqs = [(e[1], e[2], e[3]) for e in events if e[0] == "press_sequentially"]
    assert ("#login-username", "user123", 1) in seqs
    assert ("#login-password", "secret456", 1) in seqs


def test_tab_blur_apos_cada_campo(monkeypatch):
    events = _rodar(monkeypatch)
    tabs = [(e[1], e[2]) for e in events if e[0] == "press"]
    assert ("#login-username", "Tab") in tabs
    assert ("#login-password", "Tab") in tabs


def test_espera_botao_habilitar_nas_duas_etapas(monkeypatch):
    events = _rodar(monkeypatch)
    habilitou = [e[1] for e in events if e[0] == "to_be_enabled"]
    assert "#btn-continue" in habilitou
    assert "[id='btn-log in']" in habilitou


def test_pausa_recaptcha_entre_habilitar_e_clicar(monkeypatch):
    events = _rodar(monkeypatch)
    # Para cada clique de submit, deve haver to_be_enabled e, logo antes do
    # clique, uma pausa de reCAPTCHA (wait_for_timeout == recaptcha_settle_ms).
    for botao in ("#btn-continue", "[id='btn-log in']"):
        i_enabled = next(i for i, e in enumerate(events)
                         if e[0] == "to_be_enabled" and e[1] == botao)
        i_click = next(i for i, e in enumerate(events)
                       if e[0] == "click" and e[1] == botao)
        pausas = [e for e in events[i_enabled:i_click] if e == ("wait_for_timeout", 11)]
        assert pausas, f"sem pausa de reCAPTCHA antes do clique em {botao}"
        assert i_enabled < i_click


def test_ordem_completa_das_etapas(monkeypatch):
    events = _rodar(monkeypatch)

    def idx(pred):
        return next(i for i, e in enumerate(events) if pred(e))

    i_user_type = idx(lambda e: e[0] == "press_sequentially" and e[1] == "#login-username")
    i_user_tab = idx(lambda e: e[0] == "press" and e[1] == "#login-username")
    i_cont_enabled = idx(lambda e: e[0] == "to_be_enabled" and e[1] == "#btn-continue")
    i_cont_click = idx(lambda e: e[0] == "click" and e[1] == "#btn-continue")
    i_pwd_visible = idx(lambda e: e[0] == "wait_for" and e[1] == "#login-password")
    i_pwd_type = idx(lambda e: e[0] == "press_sequentially" and e[1] == "#login-password")
    i_login_enabled = idx(lambda e: e[0] == "to_be_enabled" and e[1] == "[id='btn-log in']")
    i_login_click = idx(lambda e: e[0] == "click" and e[1] == "[id='btn-log in']")

    assert (i_user_type < i_user_tab < i_cont_enabled < i_cont_click
            < i_pwd_visible < i_pwd_type < i_login_enabled < i_login_click)


def test_networkidle_que_nunca_ocorre_nao_derruba_o_login(monkeypatch):
    """Portal com widgets de chat/analytics nunca fica ocioso (ConsigNet pós-
    deploy 07/2026): o timeout do networkidle deve ser engolido — o login já
    aconteceu e quem valida o pós-login são os callers."""
    from playwright.sync_api import TimeoutError as PWTimeout

    rec = _Rec()
    page = _FakePage(rec)

    def _networkidle_estoura(state, timeout=None):
        rec.events.append(("wait_for_load_state", state, timeout))
        raise PWTimeout("Timeout 30000ms exceeded.")

    page.wait_for_load_state = _networkidle_estoura
    monkeypatch.setattr("app.auth.two_step_auth.expect", _fake_expect_factory(rec))
    strat = TwoStepAuthStrategy(
        "user123", "secret456", SELECTORS,
        key_delay_ms=1, blur_settle_ms=7, recaptcha_settle_ms=11, enable_timeout_ms=4000,
    )
    strat.authenticate(page, "https://portal.local/auth/login", timeout=180_000)  # não levanta

    # E o cap: mesmo com timeout global de 180s, networkidle espera no máx. 30s.
    chamada = next(e for e in rec.events if e[0] == "wait_for_load_state")
    assert chamada[2] == 30_000


# ───────────────── popup_dismiss opcional (aviso do portal) ──────────────────

SELECTORS_COM_POPUP = {
    **SELECTORS,
    "popup_dismiss": {"type": "css", "value": "#btn-fechar-aviso"},
}


def _rodar_com_popup(monkeypatch, selectors):
    rec = _Rec()
    page = _FakePage(rec)
    monkeypatch.setattr("app.auth.two_step_auth.expect", _fake_expect_factory(rec))
    strat = TwoStepAuthStrategy(
        "user123", "secret456", selectors,
        key_delay_ms=1, blur_settle_ms=7, recaptcha_settle_ms=11, enable_timeout_ms=4000,
    )
    strat.authenticate(page, "https://portal.local/auth/login", timeout=5000)
    return rec.events


def test_popup_dismiss_configurado_e_visivel_fecha_antes_de_digitar(monkeypatch):
    # _FakeLocator.wait_for sempre sucede → o aviso "está visível" nos 2 pontos.
    events = _rodar_com_popup(monkeypatch, SELECTORS_COM_POPUP)
    cliques_aviso = [i for i, e in enumerate(events)
                     if e[0] == "click" and e[1] == "#btn-fechar-aviso"]
    assert len(cliques_aviso) == 2, "aviso deve ser fechado no pré-login e entre etapas"

    i_type_user = next(i for i, e in enumerate(events)
                       if e[0] == "press_sequentially" and e[1] == "#login-username")
    assert cliques_aviso[0] < i_type_user, "1º fechamento vem antes de digitar o usuário"

    i_click_cont = next(i for i, e in enumerate(events)
                        if e[0] == "click" and e[1] == "#btn-continue")
    i_type_pwd = next(i for i, e in enumerate(events)
                      if e[0] == "press_sequentially" and e[1] == "#login-password")
    assert i_click_cont < cliques_aviso[1] < i_type_pwd, \
        "2º fechamento fica entre o CONTINUE e a digitação da senha"


def test_sem_popup_dismiss_configurado_nada_muda(monkeypatch):
    events = _rodar_com_popup(monkeypatch, SELECTORS)
    assert not any(e[0] == "click" and "aviso" in str(e[1]) for e in events)


# ───────────────────── Camada B: integração HTML local ──────────────────────

_FIXTURE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>fake 2-step</title></head>
<body>
  <div id="step1">
    <input id="login-username" autocomplete="off" />
    <button id="btn-continue" disabled>CONTINUE</button>
  </div>
  <div id="step2" style="display:none">
    <input id="login-password" type="password" autocomplete="off" />
    <button id="btn-log in" disabled>LOG IN</button>
  </div>
  <div id="result"></div>
  <script>
    var u = document.getElementById('login-username');
    var c = document.getElementById('btn-continue');
    // habilita CONTINUE só após digitar e sair do campo (blur) — espelha o portal
    u.addEventListener('blur', function () { if (u.value.length > 0) c.disabled = false; });
    c.addEventListener('click', function () {
      document.getElementById('step1').style.display = 'none';
      document.getElementById('step2').style.display = 'block';
    });
    var p = document.getElementById('login-password');
    var l = document.querySelector("[id='btn-log in']");
    p.addEventListener('blur', function () { if (p.value.length > 0) l.disabled = false; });
    l.addEventListener('click', function () {
      document.getElementById('result').textContent = 'LOGGED_IN';
    });
  </script>
</body></html>
"""


def test_integracao_fluxo_2_etapas_html_local(tmp_path):
    """Fluxo real contra HTML local (sem portal): valida press_sequentially +
    espera-habilitar + as 2 etapas de ponta a ponta."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # pragma: no cover
        pytest.skip(f"playwright indisponível: {e}")

    fixture = tmp_path / "login.html"
    fixture.write_text(_FIXTURE_HTML, encoding="utf-8")
    url = fixture.as_uri()

    strat = TwoStepAuthStrategy(
        "user123", "secret456", SELECTORS,
        key_delay_ms=3, blur_settle_ms=20, recaptcha_settle_ms=20, enable_timeout_ms=4000,
    )

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"Chromium não pôde iniciar neste ambiente: {e}")
        try:
            page = browser.new_context().new_page()
            page.set_default_timeout(5000)
            strat.authenticate(page, url, timeout=5000)
            assert page.locator("#result").inner_text() == "LOGGED_IN"
            assert page.locator("#login-username").input_value() == "user123"
            assert page.locator("#login-password").input_value() == "secret456"
        finally:
            browser.close()
