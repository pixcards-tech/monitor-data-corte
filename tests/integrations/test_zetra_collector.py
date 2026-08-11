"""Testes do ZetraApiCollector — contrato, mapeamento e abort de lote."""
from __future__ import annotations

from app.integrations.processors.zetra.client import (
    ZetraCredencialError,
    ZetraIpBloqueadoError,
)
from app.integrations.processors.zetra.collector import ZetraApiCollector
from app.services.coleta_service import _build_api_collector


class _ConfigStub:
    pausa_s = 0.0


class _ClientStub:
    """Simula ZetraClient: devolve respostas em fila ou levanta exceções."""

    def __init__(self, respostas):
        self.config = _ConfigStub()
        self.respostas = list(respostas)
        self.chamadas = []

    def consultar_parametros(self, convenio, servico_codigo=None):
        self.chamadas.append((convenio, servico_codigo))
        item = self.respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resposta_ok(dia=15, periodo="2026-09-01", svc="CARTAO BENEFICIO"):
    return {
        "sucesso": True,
        "cod_retorno": "000",
        "mensagem": None,
        "dia_corte": dia,
        "periodo_atual": periodo,
        "svc_descricao": svc,
    }


_CONVENIO = {"nome": "PREF DE SERRA - ES", "zetra_convenio": "PIX_CARD-SERRA"}


def test_sucesso_mapeia_para_contrato_do_pipeline():
    collector = ZetraApiCollector(client=_ClientStub([_resposta_ok()]))
    resultado = collector.run("serra", _CONVENIO)

    assert resultado["status"] == "ok"
    assert resultado["erro"] is None
    assert resultado["dados"] == [{
        "folha": "CARTAO BENEFICIO",
        "mes_atual": "09/2026",
        "data_corte": "15/09/2026",
    }]


def test_servico_codigo_e_repassado_ao_client():
    client = _ClientStub([_resposta_ok()])
    collector = ZetraApiCollector(client=client)
    collector.run("novalima", {**_CONVENIO, "servico_codigo": "044"})

    assert client.chamadas == [("PIX_CARD-SERRA", "044")]


def test_cod_credencial_aborta_o_restante_do_lote():
    client = _ClientStub([ZetraCredencialError("codRetorno 001 — credencial inválida")])
    collector = ZetraApiCollector(client=client)

    r1 = collector.run("serra", _CONVENIO)
    r2 = collector.run("maua", {"nome": "Mauá", "zetra_convenio": "PIX_CARD-MAUA"})

    assert r1["status"] == "erro"
    assert r1["erro_categoria"] == "auth_falhou"
    assert r2["status"] == "erro"
    assert "abortado" in r2["erro"].lower()
    assert len(client.chamadas) == 1, "segundo convênio não pode chamar a API"


def test_ip_bloqueado_tambem_aborta():
    client = _ClientStub([ZetraIpBloqueadoError("codRetorno 362 — IP não autorizado")])
    collector = ZetraApiCollector(client=client)

    collector.run("serra", _CONVENIO)
    r2 = collector.run("salto", {"nome": "Salto", "zetra_convenio": "PIX_CARD-SALTO"})

    assert r2["status"] == "erro"
    assert len(client.chamadas) == 1


def test_resposta_sem_sucesso_registra_sem_abortar():
    client = _ClientStub([
        {
            "sucesso": False,
            "cod_retorno": "243",
            "mensagem": "mais de um serviço encontrado",
            "dia_corte": None,
            "periodo_atual": None,
            "svc_descricao": None,
        },
        _resposta_ok(),
    ])
    collector = ZetraApiCollector(client=client)

    r1 = collector.run("curitiba", {"nome": "Curitiba", "zetra_convenio": "PIX_CARD-CURITIBA"})
    r2 = collector.run("serra", _CONVENIO)

    assert r1["status"] == "erro"
    assert "243" in r1["erro"]
    assert r2["status"] == "ok", "erro pontual não pode abortar o lote"


def test_resposta_ok_sem_dia_corte_vira_sem_dado():
    collector = ZetraApiCollector(client=_ClientStub([_resposta_ok(dia=None)]))
    resultado = collector.run("serra", _CONVENIO)

    assert resultado["status"] == "erro"
    assert resultado["erro_categoria"] == "sem_dado"


def test_convenio_sem_zetra_convenio_falha_claro():
    collector = ZetraApiCollector(client=_ClientStub([]))
    resultado = collector.run("quebrado", {"nome": "Sem código"})

    assert resultado["status"] == "erro"
    assert "zetra_convenio" in resultado["erro"]


# ── Dispatch em coleta_service ────────────────────────────────────────────────

def test_dispatch_zetra_e_default_safeconsig():
    from app.integrations.processors.safeconsig.collector import SafeConsigApiCollector

    zetra = _build_api_collector({"integration_type": "api", "api_collector": "zetra"})
    default = _build_api_collector({"integration_type": "api"})

    assert isinstance(zetra, ZetraApiCollector)
    assert isinstance(default, SafeConsigApiCollector)


def test_dispatch_desconhecido_falha():
    import pytest

    with pytest.raises(ValueError, match="api_collector"):
        _build_api_collector({"integration_type": "api", "api_collector": "inexistente"})
