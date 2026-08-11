"""Diagnóstico: captura o aviso que bloqueia a coleta no ConsigLog (Duque de Caxias).

Roda o fluxo real (login → seleção de órgão) e, no ponto em que a tabela de
prazos deveria aparecer, salva screenshot + HTML e lista os elementos visíveis
que parecem modal/aviso. Não salva dados de coleta nem envia e-mail.

Uso:
    python scripts/diag_consiglog_aviso.py [--convenio duque_de_caxias_rj]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger("diag_consiglog_aviso")

from app.core.loader import load_processadoras_config
from app.services.coleta_service import build_auth_strategy, build_scraper

_CANDIDATOS_MODAL = (
    "[id*='Popup']", "[id*='popup']", "[id*='Modal']", "[id*='modal']",
    "[id*='Aviso']", "[id*='aviso']", "[class*='modal']", "[class*='popup']",
    "[class*='aviso']", "[class*='overlay']", "[role='dialog']", "[role='alertdialog']",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--convenio", default="duque_de_caxias_rj")
    args = parser.parse_args()

    config = load_processadoras_config()
    convenio_config = config["convenios"][args.convenio]
    processadora_key = convenio_config["processadora"]
    processadora_config = config["processadoras"][processadora_key]

    destino = Path("data/diagnostico")
    destino.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    scraper = build_scraper(
        processadora_key=processadora_key,
        processadora_config=processadora_config,
        convenio_config=convenio_config,
        auth_strategy=build_auth_strategy(processadora_config, convenio_config),
    )

    try:
        scraper.start()
        scraper.authenticate()
        scraper.validate_access()
        scraper._selecionar_orgao()

        # Dá tempo do aviso renderizar e captura o estado da tela.
        scraper.page.wait_for_timeout(4000)
        png = destino / f"consiglog_{args.convenio}_{stamp}.png"
        html = destino / f"consiglog_{args.convenio}_{stamp}.html"
        scraper.page.screenshot(path=str(png), full_page=True)
        html.write_text(scraper.page.content(), encoding="utf-8")
        print(f"\nEvidências salvas:\n  {png}\n  {html}\n")

        print("Elementos candidatos a modal/aviso VISÍVEIS:")
        vistos = set()
        for sel in _CANDIDATOS_MODAL:
            loc = scraper.page.locator(sel)
            for i in range(min(loc.count(), 10)):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    ident = el.get_attribute("id") or el.get_attribute("class") or sel
                    if ident in vistos:
                        continue
                    vistos.add(ident)
                    texto = " ".join(el.inner_text().split())[:180]
                    print(f"  [{sel}] id/class={ident!r}")
                    print(f"      texto: {texto!r}")
                except Exception:
                    continue
        if not vistos:
            print("  (nenhum — o bloqueio pode não ser um modal)")

        tabela = scraper.page.locator("#body_Prazos_gvPrazos")
        print(f"\nTabela de prazos presente no DOM: {tabela.count() > 0} | visível: "
              f"{tabela.count() > 0 and tabela.first.is_visible()}")
        print(f"URL atual: {scraper.page.url}")
        return 0
    finally:
        scraper.stop()


if __name__ == "__main__":
    sys.exit(main())
