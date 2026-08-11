"""Estratégia de autenticação em 2 etapas — com digitação humana.

Passo 1: digita o username tecla-a-tecla + Tab/blur → espera o botão habilitar
         → dá um tempo ao reCAPTCHA → CONTINUE
Passo 2: digita a senha tecla-a-tecla + Tab/blur → espera o botão habilitar
         → dá um tempo ao reCAPTCHA → LOG IN → networkidle

Motivação: o ``fill`` instantâneo + clique imediato era pontuado como robô pelo
reCAPTCHA v3 do ConsigNet. Aqui imitamos o ritmo humano — ``press_sequentially``
com atraso por tecla, evento de blur, espera ativa do botão habilitar e uma
pausa para o reCAPTCHA pontuar o comportamento antes do clique. O fluxo de duas
etapas (user → CONTINUE → senha → LOG IN) é preservado.

Usado por: ConsigLog (Login.aspx → LoginSegundaEtapa.aspx)
           ConsigNet (username → CONTINUE → password → LOG IN)
"""
import logging

from playwright.sync_api import Page, expect

from app.auth.base_auth_strategy import BaseAuthStrategy

logger = logging.getLogger(__name__)

# Ritmo "humano" (ms) — afasta a automação do padrão robótico que o reCAPTCHA v3
# pontua como bot. Sobrescrevíveis no construtor (testes usam valores baixos).
_KEY_DELAY_MS = 90           # atraso entre teclas no press_sequentially
_BLUR_SETTLE_MS = 600        # após o Tab (deixa o JS de validação rodar)
_RECAPTCHA_SETTLE_MS = 2000  # pausa antes do clique p/ o reCAPTCHA pontuar
_ENABLE_TIMEOUT_MS = 15_000  # teto p/ esperar o botão de submit habilitar


class TwoStepAuthStrategy(BaseAuthStrategy):
    def __init__(
        self,
        username: str,
        password: str,
        selectors: dict,
        *,
        key_delay_ms: int = _KEY_DELAY_MS,
        blur_settle_ms: int = _BLUR_SETTLE_MS,
        recaptcha_settle_ms: int = _RECAPTCHA_SETTLE_MS,
        enable_timeout_ms: int = _ENABLE_TIMEOUT_MS,
    ) -> None:
        self.username = username
        self.password = password
        self.selectors = selectors
        self.key_delay_ms = key_delay_ms
        self.blur_settle_ms = blur_settle_ms
        self.recaptcha_settle_ms = recaptcha_settle_ms
        self.enable_timeout_ms = enable_timeout_ms

    def _locator(self, page: Page, key: str):
        sel = self.selectors[key]
        if sel["type"] == "css":
            return page.locator(sel["value"])
        if sel["type"] == "role":
            return page.get_by_role(sel["role"], name=sel.get("name"))
        raise ValueError(f"Tipo de seletor inválido: {sel}")

    def _digitar_humano(self, page: Page, field, valor: str) -> None:
        """Digita tecla-a-tecla (sem ``fill`` instantâneo) e dispara blur via Tab."""
        field.click()
        field.press_sequentially(valor, delay=self.key_delay_ms)
        field.press("Tab")  # blur → dispara validação / habilita o botão
        page.wait_for_timeout(self.blur_settle_ms)

    def _clicar_quando_habilitar(self, page: Page, submit, etapa: str, timeout: int) -> None:
        """Espera o botão habilitar e dá tempo ao reCAPTCHA antes do clique."""
        enable_timeout = min(timeout, self.enable_timeout_ms)
        try:
            expect(submit).to_be_enabled(timeout=enable_timeout)
        except AssertionError as e:
            raise RuntimeError(
                f"[TwoStepAuth] Botão de submit ({etapa}) não habilitou em {enable_timeout}ms"
            ) from e
        # Deixa o reCAPTCHA v3 pontuar o comportamento humano antes do clique.
        page.wait_for_timeout(self.recaptcha_settle_ms)
        submit.click()

    def _fechar_popup_se_visivel(self, page: Page, momento: str, timeout_ms: int = 2500) -> None:
        """Fecha um modal de aviso do portal, se configurado e visível.

        Opt-in via selector `popup_dismiss` (ex.: aviso institucional do
        ConsigLog/ConsigX que cobre a tela e intercepta os cliques do login —
        sem fechar, o submit não chega no botão e o portal conta tentativa
        errada). Sem o selector configurado, é no-op.
        """
        if "popup_dismiss" not in self.selectors:
            return
        btn = self._locator(page, "popup_dismiss")
        try:
            btn.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            return  # aviso não apareceu — fluxo normal
        logger.info("[TwoStepAuth] Aviso do portal visível (%s) — fechando.", momento)
        try:
            btn.click()
            page.wait_for_timeout(500)
        except Exception:
            logger.warning("[TwoStepAuth] Não conseguiu fechar o aviso (%s) — seguindo.", momento)

    def authenticate(self, page: Page, target_url: str, timeout: int) -> None:
        page.goto(target_url, wait_until="domcontentloaded", timeout=timeout)
        self._fechar_popup_se_visivel(page, "pré-login")

        # Etapa 1: username → CONTINUE
        username_field = self._locator(page, "step1_username")
        username_field.wait_for(state="visible", timeout=timeout)
        self._digitar_humano(page, username_field, self.username)

        submit1 = self._locator(page, "step1_submit")
        self._clicar_quando_habilitar(page, submit1, "etapa 1", timeout)

        # O aviso pode reaparecer na troca de tela e cobrir o campo de senha.
        self._fechar_popup_se_visivel(page, "entre etapas")

        # Aguarda a segunda tela (campo de senha) aparecer.
        password_field = self._locator(page, "step2_password")
        password_field.wait_for(state="visible", timeout=timeout)

        # Etapa 2: password → LOG IN
        self._digitar_humano(page, password_field, self.password)

        submit2 = self._locator(page, "step2_submit")
        self._clicar_quando_habilitar(page, submit2, "etapa 2", timeout)

        # networkidle é MELHOR-ESFORÇO: o portal pode nunca ficar ocioso (o
        # ConsigNet pós-deploy de 07/2026 carrega chat Freshworks + GTM que
        # fazem tráfego contínuo) e estourar o timeout cheio JÁ LOGADO. Cap
        # curto e segue — o pós-login é validado pelos callers
        # (wait_for_url/validate_access), mesmo idiom do user_pass_auth.
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout, 30_000))
        except Exception:
            logger.debug("[TwoStepAuth] networkidle não atingido — seguindo. URL: %s", page.url)
        logger.info("[TwoStepAuth] Autenticação concluída. URL: %s", page.url)
