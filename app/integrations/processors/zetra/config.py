"""Configuração da integração Zetra/eConsig — lida do .env.

Credencial única para todos os convênios (nível processadora): o Centralizador
autentica por cliente/usuario/senha e recebe o convênio como parâmetro da
consulta.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from app.integrations.processors.base.exceptions import ConfigurationError

_ENDPOINT_DEFAULT = "https://api.econsig.com.br/central/services/HostaHostService"


@dataclass(frozen=True)
class ZetraConfig:
    endpoint: str
    cliente: str
    usuario: str
    senha: str
    timeout_s: float
    pausa_s: float

    @classmethod
    def from_env(cls) -> "ZetraConfig":
        cliente = os.getenv("ZETRA_CLIENTE", "").strip()
        usuario = os.getenv("ZETRA_USUARIO", "").strip()
        senha = os.getenv("ZETRA_SENHA", "").strip()
        faltando = [
            nome
            for nome, valor in (
                ("ZETRA_CLIENTE", cliente),
                ("ZETRA_USUARIO", usuario),
                ("ZETRA_SENHA", senha),
            )
            if not valor
        ]
        if faltando:
            raise ConfigurationError(
                f"Variáveis de ambiente da Zetra ausentes ou vazias: {', '.join(faltando)}. "
                "Configure no .env antes de coletar."
            )
        return cls(
            endpoint=os.getenv("ZETRA_ENDPOINT", "").strip() or _ENDPOINT_DEFAULT,
            cliente=cliente,
            usuario=usuario,
            senha=senha,
            timeout_s=float(os.getenv("ZETRA_TIMEOUT", "30")),
            pausa_s=float(os.getenv("ZETRA_PAUSA_ENTRE_CHAMADAS", "1.5")),
        )
