"""Zetra API Collector — encaixa o Centralizador eConsig no pipeline de coleta.

Mesmo contrato de resultado do SafeConsigApiCollector/BaseScraper.run():
dict com status, dados, erro, erro_categoria.

Diferenças semânticas vs SafeConsig:
  - O `diaCorte` é parâmetro OFICIAL do Centralizador (não estimativa) — a
    processadora declara `api_origem: "api_oficial"` no processadoras.json.
  - `folha` carrega o `svcDescricao` (serviço que a API escolheu).
  - Credencial única no nível da processadora (ZETRA_* no .env), não por
    convênio.

Abort de lote: codRetorno 001 (credencial) e 362 (IP não autorizado) valem
para TODOS os convênios — insistir nos demais gasta chamada e arrisca bloqueio
por excesso de tentativa de login. Na primeira ocorrência o collector marca o
lote como abortado e os convênios seguintes falham SEM chamar a API. Depende
de coleta_service instanciar UM collector por lote (ver _build_api_collector).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.integrations.processors.base.exceptions import ConfigurationError, IntegrationError
from app.integrations.processors.zetra.client import (
    ZetraClient,
    ZetraCredencialError,
    ZetraIpBloqueadoError,
)

logger = logging.getLogger(__name__)


def _erro(mensagem: str, categoria: str | None) -> dict[str, Any]:
    return {"status": "erro", "dados": [], "erro": mensagem, "erro_categoria": categoria}


class ZetraApiCollector:
    def __init__(self, client: ZetraClient | None = None) -> None:
        self._client = client
        self._abort_erro: str | None = None
        self._chamadas = 0

    def run(self, convenio_key: str, convenio_config: dict[str, Any]) -> dict[str, Any]:
        if self._abort_erro:
            return _erro(
                f"[Zetra] Lote abortado sem chamar a API — {self._abort_erro}",
                "auth_falhou",
            )

        try:
            if self._client is None:
                self._client = ZetraClient()
        except ConfigurationError as exc:
            logger.error("[ZetraCollector] Configuração ausente: %s", exc)
            return _erro(str(exc), "auth_falhou")

        zetra_convenio = convenio_config.get("zetra_convenio")
        if not zetra_convenio:
            return _erro(
                f"Campo 'zetra_convenio' ausente na config do convênio '{convenio_key}'.",
                None,
            )

        # Pausa entre chamadas do lote (nunca antes da primeira).
        if self._chamadas and self._client.config.pausa_s > 0:
            time.sleep(self._client.config.pausa_s)
        self._chamadas += 1

        try:
            resposta = self._client.consultar_parametros(
                zetra_convenio, convenio_config.get("servico_codigo")
            )
        except (ZetraCredencialError, ZetraIpBloqueadoError) as exc:
            self._abort_erro = str(exc)
            logger.error(
                "[ZetraCollector] %s: %s — abortando o restante do lote.",
                convenio_key, exc,
            )
            return _erro(str(exc), "auth_falhou")
        except IntegrationError as exc:
            logger.error("[ZetraCollector] Falha (%s): %s", convenio_key, exc)
            return _erro(str(exc), "rede")

        if not resposta["sucesso"]:
            return _erro(
                f"[Zetra] codRetorno {resposta['cod_retorno']}: "
                f"{resposta['mensagem'] or 'sem mensagem'}",
                None,
            )

        dia_corte = resposta["dia_corte"]
        periodo = resposta["periodo_atual"]
        if dia_corte is None or not periodo:
            return _erro(
                f"[Zetra] Resposta ok mas sem diaCorte/periodoAtual "
                f"(diaCorte={resposta['dia_corte']!r}, periodoAtual={periodo!r}).",
                "sem_dado",
            )

        ano, mes, _dia = periodo.split("-")
        registro = {
            "folha": resposta["svc_descricao"] or "zetra",
            "mes_atual": f"{mes}/{ano}",
            "data_corte": f"{dia_corte:02d}/{mes}/{ano}",
        }
        logger.info(
            "[ZetraCollector] %s → data_corte=%s (%s)",
            convenio_key, registro["data_corte"], registro["folha"],
        )
        return {"status": "ok", "dados": [registro], "erro": None, "erro_categoria": None}
