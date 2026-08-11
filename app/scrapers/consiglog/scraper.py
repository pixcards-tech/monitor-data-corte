"""Scraper para o portal ConsigLog / SAEC.

Tecnologia: ASP.NET WebForms (.aspx).
Convênios: Cotia-SP, Duque de Caxias-RJ.

Migração 08/2026: o portal mudou de saec.consiglog.com.br para
saec.consigx.com.br (o DNS antigo morreu) e passou a exibir um modal de AVISO
institucional (`ucAjaxModalPopup1`) que cobre a tela e bloqueia a coleta.
O aviso é fechado em três pontos: no login (via popup_dismiss do TwoStepAuth),
após o login e antes de cada leitura da tabela de prazos.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.exceptions import CollectionError
from app.scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

_SUCCESS_INDICATORS = [
    "table#gvOrgao",          # tela de seleção de convênio (pós-login, WebForms)
    "text=Sair",
    "text=Logout",
    "text=Sair do Sistema",
    "[id*='MenuPrincipal']",
    "[id*='menuPrincipal']",
    "[id*='lnkSair']",
    "[class*='dashboard']",
]

# Campos que existem na(s) tela(s) de login. Persistem no DOM mesmo após logar
# (a seleção de convênio é servida na mesma LoginSegundaEtapa.aspx), então sua
# presença sozinha NÃO indica falha — é preciso uma mensagem de erro real.
_LOGIN_FIELD_SELECTORS = ("#txtLogin", "#txtSenha")
_LOGIN_ERROR_SELECTORS = ("[id*='lblErro']", "[class*='error']", "[class*='alert']", "body")
# Palavras que caracterizam erro REAL de login (não apenas estar numa URL .aspx).
_LOGIN_ERROR_KEYWORDS = ("nválid", "inválid", "xpirad", "ncorret", "tativa", "bloque")

# Botão de confirmação do modal "Usuário já logado" (sessão em outro terminal).
_BTN_SESSAO_ANTERIOR = "#ucAjaxModalPopupConfirmacao1_btnConfirmarPopup"
# Botão de OK do modal de AVISO institucional (ConsigX, 08/2026) — cobre a tela
# e impede a leitura da tabela de prazos enquanto estiver aberto.
_BTN_AVISO = "#ucAjaxModalPopup1_btnConfirmarPopup"


class ConsiglogScraper(BaseScraper):
    def authenticate(self) -> None:
        super().authenticate()
        self._fechar_aviso("pós-login")
        # ConsigLog exibe um modal AJAX "Usuário já logado" quando há sessão
        # aberta em outro terminal. Espera o botão de confirmação RENDERIZAR
        # (timeout curto) antes de decidir que não há popup — is_visible() não
        # espera, e o modal costuma aparecer alguns ms após o login.
        confirm_btn = self.page.locator(_BTN_SESSAO_ANTERIOR)
        try:
            confirm_btn.wait_for(state="visible", timeout=3000)
        except Exception:
            return  # popup não apareceu no prazo → segue o fluxo normal

        logger.info("[ConsigLog] Sessão anterior detectada — confirmando desconexão...")
        try:
            with self.page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                confirm_btn.click()
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        logger.info("[ConsigLog] Desconexão confirmada. URL: %s", self.page.url)

    def _fechar_aviso(self, momento: str, timeout_ms: int = 2000) -> bool:
        """Fecha o modal de AVISO institucional se estiver visível. Nunca lança."""
        btn = self.page.locator(_BTN_AVISO)
        try:
            btn.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            return False
        logger.info("[ConsigLog] Aviso do portal visível (%s) — fechando.", momento)
        try:
            btn.click()
            self.page.wait_for_timeout(500)
            return True
        except Exception:
            logger.warning("[ConsigLog] Não conseguiu fechar o aviso (%s).", momento)
            return False

    def validate_access(self) -> None:
        if self.page is None:
            raise RuntimeError("Page não inicializada.")

        current_url = self.page.url
        logger.info("[ConsigLog] URL após autenticação: %s", current_url)

        # 1) Sucesso por CONTEÚDO, não pela URL. O WebForms mantém
        #    LoginSegundaEtapa.aspx no postback pós-login, exibindo a seleção de
        #    convênio (gvOrgao) — validar pela URL daria falso-negativo.
        for selector in _SUCCESS_INDICATORS:
            if self._tem(selector):
                logger.info("[ConsigLog] Sessão validada via conteúdo: %s", selector)
                return

        # 2) Falha REAL: campos de login na tela + mensagem de erro de verdade.
        #    Os campos de login persistem mesmo logado, então a mensagem é o que
        #    distingue falha real do falso-negativo de URL.
        tem_campos_login = any(self._tem(sel) for sel in _LOGIN_FIELD_SELECTORS)
        error_msg = self._mensagem_erro_login()
        if tem_campos_login and error_msg:
            categoria = "credencial_expirada" if "xpirad" in error_msg.lower() else "auth_falhou"
            raise CollectionError(
                f"[ConsigLog] Autenticação falhou — login recusado. "
                f"Mensagem: {error_msg!r}. URL: {current_url}",
                categoria=categoria,
            )

        # 3) Nem sucesso reconhecível nem erro explícito: não derruba pela URL.
        #    collect() exige a seleção de convênio e falhará claramente se ausente.
        logger.warning(
            "[ConsigLog] Sem indicador de sucesso nem erro de login explícito. "
            "Prosseguindo — collect() depende da seleção de convênio. URL: %s",
            current_url,
        )

    def _tem(self, selector: str) -> bool:
        try:
            return self.page.locator(selector).count() > 0
        except Exception:
            return False

    def _mensagem_erro_login(self) -> str:
        """Retorna a 1ª mensagem de erro REAL de login na tela, ou '' se não houver."""
        for sel in _LOGIN_ERROR_SELECTORS:
            try:
                loc = self.page.locator(sel)
                if loc.count() > 0:
                    text = loc.first.inner_text().strip()
                    if any(kw in text.lower() for kw in _LOGIN_ERROR_KEYWORDS):
                        return text[:200]
            except Exception:
                pass
        return ""

    def collect(self) -> list[dict[str, Any]]:
        if self.page is None:
            raise RuntimeError("Page não inicializada.")

        self._selecionar_orgao()
        return self._extrair_prazos()

    def _selecionar_orgao(self) -> None:
        self._fechar_aviso("seleção de órgão")
        tabela = self.page.locator("table#gvOrgao")
        tabela.wait_for(state="visible", timeout=self.timeout)

        orgao_sigla = self.convenio_config.get("orgao_sigla")
        if orgao_sigla:
            linha = tabela.locator("tr").filter(has_text=orgao_sigla)
            btn = linha.locator("input[type='image']")
        else:
            btn = tabela.locator("input[type='image']").first

        btn.wait_for(state="visible", timeout=10000)
        logger.info("[ConsigLog] Selecionando órgão: sigla=%r", orgao_sigla)

        try:
            with self.page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                btn.click()
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        logger.info("[ConsigLog] Navegou para: %s", self.page.url)

    def _extrair_prazos(self) -> list[dict[str, Any]]:
        ultimo_erro = None
        for tentativa in range(3):
            try:
                # O aviso pode (re)aparecer sobre a tela de prazos — sem fechar,
                # a tabela nunca fica "visible" e o wait_for estoura.
                self._fechar_aviso(f"extração (tentativa {tentativa + 1})")
                tabela = self.page.locator("#body_Prazos_gvPrazos")
                tabela.wait_for(state="visible", timeout=20000)

                linhas = tabela.locator("tbody tr")
                resultados: list[dict[str, Any]] = []

                for i in range(linhas.count()):
                    tds = linhas.nth(i).locator("td")
                    if tds.count() < 6:
                        continue

                    servico          = tds.nth(0).inner_text().strip()
                    data_inicio      = tds.nth(3).inner_text().strip()
                    data_final_corte = tds.nth(4).inner_text().strip()
                    folha            = tds.nth(5).inner_text().strip()

                    if not data_final_corte:
                        continue

                    resultados.append({
                        "servico": servico,
                        "data_corte": data_final_corte,
                        "folha": folha,
                        "mes_atual": folha,
                        "data_inicio_corte": data_inicio,
                    })

                logger.info("[ConsigLog] %d linha(s) extraída(s)", len(resultados))
                return resultados

            except Exception as e:
                ultimo_erro = e
                logger.debug("[ConsigLog] Tentativa %d falhou: %s", tentativa + 1, e)
                self.page.wait_for_timeout(2000)

        raise CollectionError(f"[ConsigLog] Falha ao extrair tabela de prazos após 3 tentativas: {ultimo_erro}", categoria="portal_mudou")
