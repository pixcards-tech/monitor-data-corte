"""Valida todos os coletores implementados.

Executa cada convênio/processadora e reporta sucesso ou falha.
Por padrão: NÃO salva histórico e NÃO dispara e-mail.

Uso:
    python scripts/validate_all_collectors.py --all
    python scripts/validate_all_collectors.py --all --intervalo 0
    python scripts/validate_all_collectors.py --processadora consigfacil
    python scripts/validate_all_collectors.py --processadora consigfacil --intervalo 5
    python scripts/validate_all_collectors.py --convenio maringa_prev
    python scripts/validate_all_collectors.py --convenio vilhena --intervalo 0
    python scripts/validate_all_collectors.py --dry-run

Opções:
    --all                Executar todos os convênios (obrigatório quando sem --processadora/--convenio)
    --processadora KEY   Filtrar por processadora (ex: safeconsig, consigfacil)
    --convenio KEY       Filtrar por convênio específico (ex: maringa_prev, vilhena)
    --intervalo N        Segundos entre convênios (default: 10)
    --dry-run            Listar o que seria testado sem executar nada
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s — %(message)s")

from app.core.loader import load_processadoras_config


# ── Resultado de uma validação individual ─────────────────────────────────────

class _Resultado:
    def __init__(self, convenio_key: str, convenio_nome: str, processadora: str):
        self.convenio_key = convenio_key
        self.convenio_nome = convenio_nome
        self.processadora = processadora
        self.status: str = "pendente"
        self.detalhe: str = ""

    def ok(self, detalhe: str = "") -> None:
        self.status = "ok"
        self.detalhe = detalhe

    def erro(self, detalhe: str) -> None:
        self.status = "erro"
        self.detalhe = detalhe


# ── Execução de um coletor API (SafeConsig, Zetra, ...) ───────────────────────

def _run_api_collector(convenio_key: str, convenio_config: dict) -> dict:
    from app.core.loader import load_processadoras_config
    from app.services.coleta_service import _build_api_collector

    processadora_config = load_processadoras_config()["processadoras"][
        convenio_config["processadora"]
    ]
    return _build_api_collector(processadora_config).run(convenio_key, convenio_config)


# ── Execução de um scraper ────────────────────────────────────────────────────

def _run_scraper(convenio_key: str) -> dict:
    from app.services.coleta_service import executar_coleta
    return executar_coleta(convenio_key)


# ── Lógica principal ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Valida todos os coletores sem salvar histórico nem disparar e-mail."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_convenios",
        help="Executar todos os convênios (obrigatório quando sem --processadora/--convenio)",
    )
    parser.add_argument(
        "--processadora",
        default=None,
        metavar="KEY",
        help="Filtrar por processadora (ex: safeconsig, consigfacil)",
    )
    parser.add_argument(
        "--convenio",
        default=None,
        metavar="KEY",
        help="Filtrar por convênio específico (ex: maringa_prev, vilhena)",
    )
    parser.add_argument(
        "--intervalo",
        type=int,
        default=10,
        metavar="SEGUNDOS",
        help="Segundos entre convênios (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Listar o que seria testado sem executar nada",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    # Exige ao menos um escopo para evitar execução acidental de todos os convênios
    if not args.all_convenios and not args.processadora and not args.convenio and not args.dry_run:
        print("Especifique um escopo: --all, --processadora KEY, --convenio KEY ou --dry-run.")
        print("Exemplo: python scripts/validate_all_collectors.py --all --intervalo 5")
        return 1

    config = load_processadoras_config()
    processadoras_config = config["processadoras"]
    convenios_config = config["convenios"]

    # Filtra convênios pelo critério (--all ignora demais filtros)
    if args.all_convenios:
        convenios_alvo = dict(convenios_config)
    else:
        convenios_alvo = {
            key: cfg
            for key, cfg in convenios_config.items()
            if (args.processadora is None or cfg["processadora"] == args.processadora)
            and (args.convenio is None or key == args.convenio)
        }

    if not convenios_alvo:
        filtros = []
        if args.processadora:
            filtros.append(f"processadora={args.processadora!r}")
        if args.convenio:
            filtros.append(f"convenio={args.convenio!r}")
        print(f"Nenhum convênio encontrado para {', '.join(filtros)}.")
        return 1

    total = len(convenios_alvo)
    if args.all_convenios:
        filtro_desc = " (todos)"
    elif args.convenio:
        filtro_desc = f" (convenio={args.convenio})"
    elif args.processadora:
        filtro_desc = f" (processadora={args.processadora})"
    else:
        filtro_desc = ""
    print(f"Convênios a validar: {total}{filtro_desc}")
    if args.dry_run:
        print("[dry-run] Nenhuma execução será feita.\n")

    # Cabeçalho da tabela
    print(f"\n{'CONVÊNIO':<22} {'PROCESSADORA':<16} {'STATUS':<8} DETALHE")
    print("-" * 75)

    resultados: list[_Resultado] = []

    for i, (convenio_key, convenio_config) in enumerate(convenios_alvo.items()):
        proc_key = convenio_config["processadora"]
        proc_config = processadoras_config.get(proc_key, {})
        nome = convenio_config.get("nome", convenio_key)
        r = _Resultado(convenio_key, nome, proc_key)
        resultados.append(r)

        if args.dry_run:
            integration = proc_config.get("integration_type", "scraper")
            r.ok(f"[dry-run] tipo={integration}")
            print(f"{convenio_key:<22} {proc_key:<16} {'--':<8} {r.detalhe}")
            continue

        try:
            if proc_config.get("integration_type") == "api":
                resultado = _run_api_collector(convenio_key, convenio_config)
            else:
                resultado = _run_scraper(convenio_key)

            if resultado.get("status") == "ok":
                dados = resultado.get("dados", [])
                detalhe_parts = []
                for d in dados:
                    folha = d.get("folha") or ""
                    dc = d.get("data_corte") or ""
                    if folha == "virada_competencia":
                        detalhe_parts.append(f"estimativa_competencia={dc!r}")
                    elif dc:
                        detalhe_parts.append(f"data_corte={dc!r}")
                r.ok(", ".join(detalhe_parts) if detalhe_parts else "sem dados retornados")
            else:
                r.erro(resultado.get("erro") or "erro desconhecido")

        except Exception as exc:
            r.erro(str(exc)[:120])

        status_display = "✓ ok" if r.status == "ok" else "✗ erro"
        print(f"{convenio_key:<22} {proc_key:<16} {status_display:<8} {r.detalhe}")

        # Intervalo entre convênios (exceto no último)
        if i < total - 1 and args.intervalo > 0:
            time.sleep(args.intervalo)

    # Resumo
    sucessos = sum(1 for r in resultados if r.status == "ok")
    falhas = sum(1 for r in resultados if r.status == "erro")
    pendentes = sum(1 for r in resultados if r.status == "pendente")

    print()
    print("=" * 75)
    if args.dry_run:
        print(f"[dry-run] Total: {total} convênios listados — nenhuma execução realizada.")
    else:
        print(f"Total: {total} | Sucesso: {sucessos} | Falha: {falhas}" + (f" | Pendente: {pendentes}" if pendentes else ""))

    if falhas:
        print("\nConvênios com falha:")
        for r in resultados:
            if r.status == "erro":
                print(f"  {r.convenio_key:<22} ({r.processadora}) — {r.detalhe}")

    return 0 if falhas == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
