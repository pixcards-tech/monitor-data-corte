"""Cliente SOAP do Centralizador eConsig/Zetra.

Particularidades da API (validadas em scripts/test_consulta_zetra.py):
  - SOAP 1.1, envelope montado como STRING. Não usar zeep/proxy gerado: o
    namespace do envelope é a string literal "HostaHostService" (não é URI)
    e quebra a geração de cliente. Não normalizar o namespace.
  - A ordem dos campos é obrigatória e segue o WSDL (_ORDEM_CAMPOS).
    Campos opcionais só entram no envelope quando presentes.
  - `periodoAtual` chega como "2026-09-01-03:00" — o sufixo é offset de fuso,
    descartado na normalização.

Códigos de retorno:
  - 355/418/903/904/905 → indisponibilidade temporária: retry com backoff.
  - 001 (credencial) / 362 (IP não autorizado) → exceção tipada que ABORTA o
    lote inteiro no collector: insistir gasta chamada e arrisca bloqueio.
  - Demais (201/242/243/299/...) → resposta sem sucesso, registrada sem retry.

A senha jamais aparece em log, mesmo em DEBUG — nunca logar o envelope.
"""
from __future__ import annotations

import logging
import re
import time
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests

from app.integrations.processors.base.exceptions import ApiError, AuthenticationError
from app.integrations.processors.zetra.config import ZetraConfig

logger = logging.getLogger(__name__)


class ZetraCredencialError(AuthenticationError):
    """codRetorno 001 — credencial inválida. Abortar o lote inteiro."""


class ZetraIpBloqueadoError(AuthenticationError):
    """codRetorno 362 — IP não autorizado no Centralizador. Abortar o lote."""


_CODS_RETRY = {"355", "418", "903", "904", "905"}
_MAX_TENTATIVAS = 3
_BACKOFF_BASE_S = 2.0  # 2s, 4s — monkeypatchável nos testes

# Ordem obrigatória dos campos no envelope (WSDL).
_ORDEM_CAMPOS = (
    "cliente",
    "convenio",
    "usuario",
    "senha",
    "codVerba",
    "servicoCodigo",
    "orgaoCodigo",
    "estabelecimentoCodigo",
)

_RE_PERIODO = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def montar_envelope(operacao: str, campos: dict[str, str | None]) -> str:
    """Monta o envelope SOAP como string, na ordem obrigatória do WSDL."""
    linhas = []
    for nome in _ORDEM_CAMPOS:
        valor = campos.get(nome)
        if valor is None or str(valor).strip() == "":
            continue
        linhas.append(f"      <tns:{nome}>{escape(str(valor))}</tns:{nome}>")
    corpo = "\n".join(linhas)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:tns="HostaHostService">\n'
        "  <soap:Body>\n"
        f"    <tns:{operacao}>\n"
        f"{corpo}\n"
        f"    </tns:{operacao}>\n"
        "  </soap:Body>\n"
        "</soap:Envelope>"
    )


def normalizar_periodo(bruto: str | None) -> str | None:
    """'2026-09-01-03:00' → '2026-09-01' (descarta o offset de fuso)."""
    if not bruto:
        return None
    m = _RE_PERIODO.match(bruto.strip())
    return m.group(1) if m else None


def _texto(root: ET.Element, tag: str) -> str | None:
    el = root.find(f".//{{*}}{tag}")
    if el is None:
        # Resposta pode vir sem namespace nos elementos filhos.
        el = root.find(f".//{tag}")
    return el.text.strip() if el is not None and el.text else None


class ZetraClient:
    def __init__(self, config: ZetraConfig | None = None) -> None:
        self.config = config or ZetraConfig.from_env()

    def consultar_parametros(
        self, convenio: str, servico_codigo: str | None = None
    ) -> dict:
        """Consulta os parâmetros de folha do convênio (diaCorte/periodoAtual).

        Retorna dict com: sucesso, cod_retorno, mensagem, dia_corte (int|None),
        periodo_atual ('YYYY-MM-DD'|None), svc_descricao.
        Levanta ZetraCredencialError (001) / ZetraIpBloqueadoError (362) /
        ApiError (retries esgotados).
        """
        envelope = montar_envelope(
            "consultarParametros",
            {
                "cliente": self.config.cliente,
                "convenio": convenio,
                "usuario": self.config.usuario,
                "senha": self.config.senha,
                "servicoCodigo": servico_codigo,
            },
        )
        root = self._post("consultarParametros", envelope, convenio)
        return self._interpretar(root, convenio)

    def _post(self, operacao: str, envelope: str, convenio: str) -> ET.Element:
        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": f"urn:{operacao}",
        }
        ultimo_erro: Exception | None = None
        for tentativa in range(1, _MAX_TENTATIVAS + 1):
            try:
                resposta = requests.post(
                    self.config.endpoint,
                    data=envelope.encode("utf-8"),
                    headers=headers,
                    timeout=self.config.timeout_s,
                )
                if resposta.status_code != 200:
                    raise ApiError(
                        f"HTTP {resposta.status_code} do Centralizador",
                        status_code=resposta.status_code,
                    )
                root = ET.fromstring(resposta.content)
                cod = _texto(root, "codRetorno")
                if cod in _CODS_RETRY:
                    raise ApiError(f"codRetorno {cod} — indisponibilidade temporária")
                return root
            except (requests.RequestException, ET.ParseError, ApiError) as exc:
                ultimo_erro = exc
                if tentativa < _MAX_TENTATIVAS:
                    espera = _BACKOFF_BASE_S * (2 ** (tentativa - 1))
                    logger.warning(
                        "[Zetra] %s: tentativa %d/%d falhou (%s) — aguardando %.1fs",
                        convenio, tentativa, _MAX_TENTATIVAS, exc, espera,
                    )
                    time.sleep(espera)
        raise ApiError(
            f"[Zetra] {convenio}: falha após {_MAX_TENTATIVAS} tentativas — {ultimo_erro}"
        )

    def _interpretar(self, root: ET.Element, convenio: str) -> dict:
        cod = _texto(root, "codRetorno")
        mensagem = _texto(root, "mensagem")

        if cod == "001":
            raise ZetraCredencialError(
                f"codRetorno 001 — credencial Zetra inválida ({mensagem or 'sem mensagem'})"
            )
        # 362 é o código documentado para IP não autorizado; 002 ("IP INVALIDO
        # PARA O USUARIO INFORMADO") é o observado na prática (VM, 11/08/2026).
        if cod in ("362", "002"):
            raise ZetraIpBloqueadoError(
                f"codRetorno {cod} — IP não autorizado no Centralizador "
                f"({mensagem or 'sem mensagem'}). Solicitar liberação do IP à Zetra."
            )

        dia_bruto = _texto(root, "diaCorte")
        sucesso = (_texto(root, "sucesso") or "").lower() == "true"
        logger.info(
            "[Zetra] %s → sucesso=%s codRetorno=%s diaCorte=%s",
            convenio, sucesso, cod, dia_bruto,
        )
        return {
            "sucesso": sucesso,
            "cod_retorno": cod,
            "mensagem": mensagem,
            "dia_corte": int(dia_bruto) if dia_bruto and dia_bruto.isdigit() else None,
            "periodo_atual": normalizar_periodo(_texto(root, "periodoAtual")),
            "svc_descricao": _texto(root, "svcDescricao"),
        }
