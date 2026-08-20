"""Auto-abertura da próxima competência quando o monitor acha o 1º corte real dela.

Lógica pura (quais competências abrir) testada com dicts; orquestração (ensure_ciclos
+ sync + skip de já-aberta + gancho no job) testada com dependências mockadas — sem DB.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.services import remessas_sync as rs


def _linha(comp, data_corte, origem):
    return {"competencia": comp, "data_corte": data_corte, "origem": origem}


# ── Lógica pura: competencias_futuras_detectadas ──────────────────────────────

def test_abre_futura_com_corte_automatico_real():
    linhas = [_linha("09/2026", "10/09/2026", "scraper")]
    assert rs.competencias_futuras_detectadas(linhas, "08/2026") == ["09/2026"]


def test_ignora_manual():
    linhas = [_linha("09/2026", "10/09/2026", "manual")]
    assert rs.competencias_futuras_detectadas(linhas, "08/2026") == []


def test_ignora_api_estimativa():
    linhas = [_linha("09/2026", "10/09/2026", "api_estimativa")]
    assert rs.competencias_futuras_detectadas(linhas, "08/2026") == []


def test_ignora_estimativa_mm_yyyy_mesmo_scraper():
    # data_corte é MM/YYYY (estimativa), não DD/MM/YYYY real → não abre
    linhas = [_linha("09/2026", "09/2026", "scraper")]
    assert rs.competencias_futuras_detectadas(linhas, "08/2026") == []


def test_ignora_data_none():
    linhas = [_linha("09/2026", None, "scraper")]
    assert rs.competencias_futuras_detectadas(linhas, "08/2026") == []


def test_nao_abre_corrente_nem_passado():
    linhas = [
        _linha("08/2026", "10/08/2026", "scraper"),  # corrente
        _linha("07/2026", "10/07/2026", "scraper"),  # passado
    ]
    assert rs.competencias_futuras_detectadas(linhas, "08/2026") == []


def test_multiplas_futuras_distintas_e_ordenadas():
    linhas = [
        _linha("10/2026", "05/10/2026", "scraper"),
        _linha("09/2026", "10/09/2026", "scraper"),
        _linha("09/2026", "12/09/2026", "scraper"),   # mesma comp, duplicada
        _linha("09/2026", "10/09/2026", "manual"),    # ignorada
    ]
    assert rs.competencias_futuras_detectadas(linhas, "08/2026") == ["09/2026", "10/2026"]


def test_virada_de_ano():
    linhas = [_linha("01/2027", "05/01/2027", "scraper")]
    assert rs.competencias_futuras_detectadas(linhas, "12/2026") == ["01/2027"]


def test_origem_none_conta_como_automatico():
    # origem None (scrape legado) não é manual nem estimativa → conta
    linhas = [_linha("09/2026", "10/09/2026", None)]
    assert rs.competencias_futuras_detectadas(linhas, "08/2026") == ["09/2026"]


# ── Orquestração: auto_abrir_competencias_detectadas ──────────────────────────

@contextmanager
def _fake_scope(session):
    yield session


def test_auto_abrir_abre_novas_e_pula_ja_abertas():
    linhas = [
        _linha("09/2026", "10/09/2026", "scraper"),   # já aberta → pula
        _linha("10/2026", "05/10/2026", "scraper"),   # nova → abre
    ]
    aberta = {"09/2026": True, "10/2026": False}
    session = MagicMock()
    with patch.object(rs, "montar_dados_convenios", return_value=linhas), \
         patch.object(rs, "competencia_corrente", return_value="08/2026"), \
         patch.object(rs, "session_scope", lambda: _fake_scope(session)), \
         patch.object(rs, "_competencia_aberta", side_effect=lambda s, c: aberta[c]), \
         patch.object(rs, "ensure_ciclos", return_value=3) as m_ensure:
        res = rs.auto_abrir_competencias_detectadas()

    assert res == {"abertas": [{"competencia": "10/2026", "ciclos_criados": 3}]}
    m_ensure.assert_called_once()
    assert m_ensure.call_args.args[1] == "10/2026"       # (session, comp, ...)
    assert m_ensure.call_args.kwargs.get("usuario") is None  # auditoria = "sistema"


def test_auto_abrir_sem_candidatas_nao_faz_nada():
    with patch.object(rs, "montar_dados_convenios", return_value=[]), \
         patch.object(rs, "competencia_corrente", return_value="08/2026"), \
         patch.object(rs, "ensure_ciclos") as m_ensure:
        res = rs.auto_abrir_competencias_detectadas()
    assert res == {"abertas": []}
    m_ensure.assert_not_called()


# ── sincronizar_desde_corrente ────────────────────────────────────────────────

def test_sincronizar_desde_corrente_cobre_todas_abertas():
    session = MagicMock()
    with patch.object(rs, "session_scope", lambda: _fake_scope(session)), \
         patch.object(rs, "_competencias_abertas_desde_corrente",
                      return_value=["08/2026", "09/2026"]), \
         patch.object(rs, "sincronizar_data_site",
                      side_effect=lambda c: {"competencia": c}) as m_sync:
        res = rs.sincronizar_desde_corrente()
    assert [r["competencia"] for r in res] == ["08/2026", "09/2026"]
    assert m_sync.call_count == 2


# ── Gancho no job periódico ───────────────────────────────────────────────────

def test_job_chama_auto_abrir_depois_sync():
    chamadas = []
    with patch.object(rs, "auto_abrir_competencias_detectadas",
                      side_effect=lambda: chamadas.append("auto")), \
         patch.object(rs, "sincronizar_desde_corrente",
                      side_effect=lambda: chamadas.append("sync")):
        rs.job_sync_periodico()
    assert chamadas == ["auto", "sync"]


def test_job_isola_falha_da_auto_abertura():
    # auto-abertura explode → o sync ainda roda (best-effort, não derruba o scheduler)
    with patch.object(rs, "auto_abrir_competencias_detectadas",
                      side_effect=RuntimeError("boom")), \
         patch.object(rs, "sincronizar_desde_corrente") as m_sync:
        rs.job_sync_periodico()   # não levanta
    m_sync.assert_called_once()
