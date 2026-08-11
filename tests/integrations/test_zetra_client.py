"""Testes do cliente SOAP Zetra — envelope, parse, retry e erros tipados.

Nenhum teste chama a API real: requests.post é sempre mockado.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from app.integrations.processors.base.exceptions import ApiError, ConfigurationError
from app.integrations.processors.zetra import client as zetra_client
from app.integrations.processors.zetra.client import (
    ZetraClient,
    ZetraCredencialError,
    ZetraIpBloqueadoError,
    montar_envelope,
    normalizar_periodo,
)
from app.integrations.processors.zetra.config import ZetraConfig


def _config() -> ZetraConfig:
    return ZetraConfig(
        endpoint="https://exemplo.invalid/service",
        cliente="PIX_CARD",
        usuario="pix_card_xml",
        senha="segredo",
        timeout_s=5.0,
        pausa_s=0.0,
    )


def _resposta_xml(
    sucesso: str = "true",
    cod: str | None = "000",
    mensagem: str = "",
    dia_corte: str | None = "15",
    periodo: str | None = "2026-09-01-03:00",
    svc: str | None = "CARTAO BENEFICIO",
) -> bytes:
    def tag(nome: str, valor: str | None) -> str:
        return f"<{nome}>{valor}</{nome}>" if valor is not None else ""

    corpo = (
        f"{tag('sucesso', sucesso)}{tag('codRetorno', cod)}{tag('mensagem', mensagem)}"
        f"<parametroSet>{tag('diaCorte', dia_corte)}{tag('periodoAtual', periodo)}"
        f"{tag('svcDescricao', svc)}</parametroSet>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<soap:Body><ns1:consultarParametrosResponse xmlns:ns1=\"HostaHostService\">"
        f"<retorno>{corpo}</retorno>"
        "</ns1:consultarParametrosResponse></soap:Body></soap:Envelope>"
    ).encode("utf-8")


class _RespostaFake:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code


# ── Envelope ──────────────────────────────────────────────────────────────────

def test_envelope_respeita_ordem_do_wsdl():
    envelope = montar_envelope(
        "consultarParametros",
        {
            "servicoCodigo": "044",       # fora de ordem de propósito
            "senha": "s3nh4",
            "cliente": "PIX_CARD",
            "usuario": "user",
            "convenio": "PIX_CARD-NOVALIMA",
        },
    )
    posicoes = [
        envelope.index("<tns:cliente>"),
        envelope.index("<tns:convenio>"),
        envelope.index("<tns:usuario>"),
        envelope.index("<tns:senha>"),
        envelope.index("<tns:servicoCodigo>"),
    ]
    assert posicoes == sorted(posicoes), "campos devem seguir a ordem do WSDL"


def test_envelope_omite_opcionais_ausentes_e_vazios():
    envelope = montar_envelope(
        "consultarParametros",
        {"cliente": "A", "convenio": "B", "usuario": "C", "senha": "D",
         "servicoCodigo": None, "codVerba": ""},
    )
    assert "servicoCodigo" not in envelope
    assert "codVerba" not in envelope


def test_envelope_usa_namespace_literal_sem_normalizar():
    envelope = montar_envelope(
        "consultarParametros",
        {"cliente": "A", "convenio": "B", "usuario": "C", "senha": "D"},
    )
    assert 'xmlns:tns="HostaHostService"' in envelope


def test_envelope_escapa_caracteres_xml():
    envelope = montar_envelope(
        "consultarParametros",
        {"cliente": "A", "convenio": "B", "usuario": "C", "senha": "a<b&c"},
    )
    assert "<tns:senha>a&lt;b&amp;c</tns:senha>" in envelope


# ── Normalização de período ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("2026-09-01-03:00", "2026-09-01"),
        ("2026-09-01", "2026-09-01"),
        ("2026-09-01+00:00", "2026-09-01"),
        ("", None),
        (None, None),
        ("setembro", None),
    ],
)
def test_normalizar_periodo(bruto, esperado):
    assert normalizar_periodo(bruto) == esperado


# ── consultar_parametros: parse, retry e erros tipados ────────────────────────

def test_consulta_ok_parseia_resposta(monkeypatch):
    chamadas = []

    def fake_post(url, data, headers, timeout):
        chamadas.append({"url": url, "headers": headers})
        return _RespostaFake(_resposta_xml())

    monkeypatch.setattr(zetra_client.requests, "post", fake_post)
    resultado = ZetraClient(_config()).consultar_parametros("PIX_CARD-SERRA")

    assert resultado == {
        "sucesso": True,
        "cod_retorno": "000",
        "mensagem": None,
        "dia_corte": 15,
        "periodo_atual": "2026-09-01",
        "svc_descricao": "CARTAO BENEFICIO",
    }
    assert chamadas[0]["headers"]["SOAPAction"] == "urn:consultarParametros"


def test_cod_001_levanta_erro_de_credencial(monkeypatch):
    monkeypatch.setattr(
        zetra_client.requests, "post",
        lambda *a, **k: _RespostaFake(_resposta_xml(sucesso="false", cod="001")),
    )
    with pytest.raises(ZetraCredencialError):
        ZetraClient(_config()).consultar_parametros("PIX_CARD-SERRA")


@pytest.mark.parametrize("cod", ["362", "002"])
def test_cod_de_ip_bloqueado_levanta_erro_tipado(monkeypatch, cod):
    # 362 = documentado; 002 = observado na prática ("IP INVALIDO PARA O USUARIO").
    monkeypatch.setattr(
        zetra_client.requests, "post",
        lambda *a, **k: _RespostaFake(_resposta_xml(sucesso="false", cod=cod)),
    )
    with pytest.raises(ZetraIpBloqueadoError):
        ZetraClient(_config()).consultar_parametros("PIX_CARD-SERRA")


def test_cod_retryavel_faz_retry_e_recupera(monkeypatch):
    monkeypatch.setattr(zetra_client, "_BACKOFF_BASE_S", 0.0)
    respostas = [
        _RespostaFake(_resposta_xml(sucesso="false", cod="904")),
        _RespostaFake(_resposta_xml(sucesso="false", cod="905")),
        _RespostaFake(_resposta_xml()),
    ]
    monkeypatch.setattr(
        zetra_client.requests, "post", lambda *a, **k: respostas.pop(0)
    )
    resultado = ZetraClient(_config()).consultar_parametros("PIX_CARD-SERRA")
    assert resultado["sucesso"] is True
    assert not respostas, "deveria ter consumido as 3 tentativas"


def test_retries_esgotados_viram_apierror(monkeypatch):
    monkeypatch.setattr(zetra_client, "_BACKOFF_BASE_S", 0.0)
    tentativas = []
    monkeypatch.setattr(
        zetra_client.requests, "post",
        lambda *a, **k: tentativas.append(1) or _RespostaFake(
            _resposta_xml(sucesso="false", cod="903")
        ),
    )
    with pytest.raises(ApiError):
        ZetraClient(_config()).consultar_parametros("PIX_CARD-SERRA")
    assert len(tentativas) == 3


def test_cod_nao_retryavel_nao_faz_retry(monkeypatch):
    tentativas = []
    monkeypatch.setattr(
        zetra_client.requests, "post",
        lambda *a, **k: tentativas.append(1) or _RespostaFake(
            _resposta_xml(sucesso="false", cod="243", mensagem="mais de um serviço")
        ),
    )
    resultado = ZetraClient(_config()).consultar_parametros("PIX_CARD-CURITIBA")
    assert resultado["sucesso"] is False
    assert resultado["cod_retorno"] == "243"
    assert len(tentativas) == 1


def test_config_sem_senha_falha_claro(monkeypatch):
    for var in ("ZETRA_CLIENTE", "ZETRA_USUARIO", "ZETRA_SENHA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ZETRA_CLIENTE", "PIX_CARD")
    monkeypatch.setenv("ZETRA_USUARIO", "user")
    with pytest.raises(ConfigurationError, match="ZETRA_SENHA"):
        ZetraConfig.from_env()
