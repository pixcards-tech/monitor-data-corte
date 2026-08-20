"""Sync monitor → ciclos de remessa (snapshot de data_site).

O monitor re-coleta diariamente e a data pode mudar DEPOIS do ciclo criado. O sync
compara o valor vivo do monitor (mesma montagem do /cortes/atuais) com o snapshot do
ciclo e, quando muda, grava o novo valor + marca `data_site_alterada` (o "vermelho"
da planilha) + auditoria `sync`. Multi-folha com datas divergentes na MESMA
competência → não adivinha: reporta conflito e deixa o ciclo intocado.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.services.consulta_service import montar_dados_convenios
from app.services.remessas_service import (
    auditar,
    competencia_corrente,
    ensure_ciclos,
    parse_competencia,
)
from app.storage.db import session_scope
from app.storage.remessas_models import CicloRemessaRow, ConvenioRegistroRow
from app.utils.dates import _data_ddmmyyyy

logger = logging.getLogger(__name__)

# Origem que NÃO auto-abre competência: entrada manual e estimativa via API (não é
# corte confirmado). Só corte automático REAL (data DD/MM/YYYY, origem fora daqui) abre.
_ORIGEM_NAO_ABRE = frozenset({"manual", "api_estimativa"})


def sincronizar_data_site(competencia: str | None = None) -> dict:
    """Aplica os valores do monitor aos ciclos da competência (default: corrente)."""
    comp, _ = parse_competencia(competencia or competencia_corrente())

    # Valores vivos do monitor: monitor_key → datas distintas cuja competência derivada = comp.
    datas_por_key: dict[str, set] = {}
    for r in montar_dados_convenios():
        if r.get("competencia") != comp:
            continue
        d = _data_ddmmyyyy(r.get("data_corte"))
        if d is not None:
            datas_por_key.setdefault(r["convenio_key"], set()).add(d)

    atualizados = alterados = sem_valor = 0
    conflitos: list[dict] = []
    agora = datetime.now(timezone.utc)

    with session_scope() as session:
        pares = session.execute(
            select(CicloRemessaRow, ConvenioRegistroRow)
            .join(ConvenioRegistroRow, ConvenioRegistroRow.id == CicloRemessaRow.registro_id)
            .where(CicloRemessaRow.competencia == comp,
                   ConvenioRegistroRow.monitor_key.is_not(None),
                   ConvenioRegistroRow.ativo.is_(True))
        ).all()

        for ciclo, registro in pares:
            datas = datas_por_key.get(registro.monitor_key)
            if not datas:
                sem_valor += 1
                continue
            if len(datas) > 1:
                conflitos.append({
                    "monitor_key": registro.monitor_key, "cod_empr": registro.cod_empr,
                    "datas": sorted(d.isoformat() for d in datas),
                })
                continue
            nova = next(iter(datas))
            if ciclo.data_site == nova:
                continue
            if ciclo.data_site is not None:
                # Mudança REAL sobre um valor que as equipes já viram → vermelho.
                ciclo.data_site_anterior = ciclo.data_site
                ciclo.data_site_alterada = True
                alterados += 1
            auditar(session, entidade="ciclo", entidade_id=ciclo.id, acao="sync",
                    usuario=None, campo="data_site",
                    valor_anterior=ciclo.data_site, valor_novo=nova)
            ciclo.data_site = nova
            ciclo.data_site_origem = "monitor"
            ciclo.data_site_atualizada_em = agora
            ciclo.atualizado_em = agora
            atualizados += 1

    resultado = {"competencia": comp, "atualizados": atualizados, "alterados": alterados,
                 "sem_valor": sem_valor, "conflitos": conflitos}
    logger.info("[remessas-sync] %s", resultado)
    return resultado


# ── Auto-abertura da próxima competência ──────────────────────────────────────

def competencias_futuras_detectadas(linhas: list[dict], corrente: str) -> list[str]:
    """Competências FUTURAS (> corrente) que já têm ao menos um corte AUTOMÁTICO REAL.

    Corte real = data DD/MM/YYYY de fato (não estimativa MM/YYYY nem default) e
    origem fora de {manual, api_estimativa}. Puro: recebe as linhas do
    montar_dados_convenios e devolve as competências 'MM/YYYY' ordenadas, sem tocar
    banco nem monitor. É aqui que mora toda a regra de "o que dispara a abertura".
    """
    _, corrente_inicio = parse_competencia(corrente)
    futuras: dict[str, "object"] = {}  # comp -> date de início (para ordenar)
    for r in linhas:
        if r.get("origem") in _ORIGEM_NAO_ABRE:
            continue
        if _data_ddmmyyyy(r.get("data_corte")) is None:  # exige data real DD/MM/YYYY
            continue
        comp = r.get("competencia")
        if not comp:
            continue
        try:
            _, inicio = parse_competencia(comp)
        except ValueError:
            continue
        if inicio > corrente_inicio:
            futuras[comp] = inicio
    return [c for c, _ in sorted(futuras.items(), key=lambda kv: kv[1])]


def _competencia_aberta(session, comp: str) -> bool:
    """True se a competência já tem ciclos (foi aberta pela analista ou já auto-aberta)."""
    return session.execute(
        select(CicloRemessaRow.id).where(CicloRemessaRow.competencia == comp).limit(1)
    ).first() is not None


def auto_abrir_competencias_detectadas() -> dict:
    """Abre (ensure_ciclos) as competências futuras que o monitor já detectou.

    Só ABRE — cria os ciclos em branco; o preenchimento das datas fica com o sync.
    Idempotente: competência já aberta é pulada. Auditoria como 'sistema' (usuario=None).
    """
    corrente = competencia_corrente()
    candidatas = competencias_futuras_detectadas(montar_dados_convenios(), corrente)
    abertas: list[dict] = []
    for comp in candidatas:
        with session_scope() as session:
            if _competencia_aberta(session, comp):
                continue
            criados = ensure_ciclos(session, comp, usuario=None)
        abertas.append({"competencia": comp, "ciclos_criados": criados})
        logger.info("[auto-abertura] competência %s aberta pelo sistema (%d ciclos)",
                    comp, criados)
    return {"abertas": abertas}


def _competencias_abertas_desde_corrente(session) -> list[str]:
    """Competências já abertas (com ciclos) da corrente pra frente, ordenadas."""
    _, corrente_inicio = parse_competencia(competencia_corrente())
    rows = session.execute(
        select(CicloRemessaRow.competencia, CicloRemessaRow.competencia_inicio)
        .where(CicloRemessaRow.competencia_inicio >= corrente_inicio)
        .distinct()
    ).all()
    return [c for c, _ in sorted(rows, key=lambda r: r[1])]


def sincronizar_desde_corrente() -> list[dict]:
    """Sincroniza corrente + todas as futuras já abertas (mantém as futuras frescas
    conforme o monitor traz novos cortes nos dias seguintes)."""
    with session_scope() as session:
        comps = _competencias_abertas_desde_corrente(session)
    return [sincronizar_data_site(c) for c in comps]


def job_sync_periodico() -> None:
    """Wrapper do job agendado — best-effort, nunca derruba o scheduler.

    (1) auto-abre competências futuras detectadas pelo monitor;
    (2) sincroniza corrente + as futuras já abertas. As duas etapas são isoladas:
    uma falhar não impede a outra.
    """
    try:
        auto_abrir_competencias_detectadas()
    except Exception:  # noqa: BLE001
        logger.exception("[auto-abertura] job periódico falhou")
    try:
        sincronizar_desde_corrente()
    except Exception:  # noqa: BLE001
        logger.exception("[remessas-sync] job periódico falhou")
