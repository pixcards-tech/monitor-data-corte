"""Runner diário de coleta de datas de corte.

Executa todas as processadoras configuradas em sequência, com intervalo
entre elas e retry automático para as que falharem completamente. Delega
todo o trabalho real ao ColetaOrchestrator — não duplica lógica de
coleta, comparação, storage nem e-mail.

Uso:
    python scripts/run_daily_collection.py

Variáveis de ambiente:
    DAILY_COLLECTION_INTERVAL_MINUTES    Pausa entre processadoras (default: 1)
    DAILY_COLLECTION_MAX_RETRIES         Nº de retries por processadora (default: 2)
    DAILY_COLLECTION_RETRY_DELAY_MINUTES Pausa antes de cada retry (default: 60)
    DAILY_COLLECTION_RETRY_JOIN_TIMEOUT_MINUTES
                                         Teto de espera pelos retries antes de
                                         abandonar threads travadas (default: 240)
    DAILY_COLLECTION_PROCESSADORA_TIMEOUT_MINUTES
                                         Teto por processadora: estourou → mata a
                                         árvore de processos do scraper (Chrome/
                                         driver), a chamada síncrona do Playwright
                                         levanta exceção e a rodada segue para a
                                         próxima (default: 60)

Critério de retry:
    - status == "error" (todos os convênios falharam) → retenta
    - status == "ok" ou "partial_success" → considera bem-sucedido
    - Exceção inesperada no orchestrator → retenta

Retry não-bloqueante:
    Cada processadora que falha ganha seu próprio ciclo de retry em uma
    thread dedicada. As esperas de RETRY_DELAY correm em paralelo — 3 falhas
    aguardam 60min concorrentemente, não 180min em fila. A EXECUÇÃO real,
    porém, é serializada por um lock global: nunca há dois scrapers/escritas
    no storage rodando ao mesmo tempo (Playwright e o storage JSON não são
    seguros para concorrência). A rodada principal termina sem travar
    esperando os retries; o processo só encerra após todos concluírem.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_daily_collection")

from app.core.enums import CollectionStatus
from app.core.loader import load_processadoras_config
from app.core.settings import settings
from app.services import healthcheck
from app.services.orchestrator import ColetaOrchestrator
from app.services.orchestrator_factory import build_orchestrator


# ── Configuração lida do ambiente ─────────────────────────────────────────────

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        logger.warning("Variável %s inválida — usando default %d", key, default)
        return default


INTERVAL_MINUTES: int = _env_int("DAILY_COLLECTION_INTERVAL_MINUTES", 1)
MAX_RETRIES: int = _env_int("DAILY_COLLECTION_MAX_RETRIES", 2)
RETRY_DELAY_MINUTES: int = _env_int("DAILY_COLLECTION_RETRY_DELAY_MINUTES", 60)
# Teto de espera pelas threads de retry. Ciclo legítimo máximo: 2 retries ×
# (60min de delay + execução) ≈ 3h. Estourou → a thread travou (ex.: Playwright
# pendurado, incidente de 24/07/2026) e é abandonada para o dia ainda fechar
# com resumo + e-mail.
RETRY_JOIN_TIMEOUT_MINUTES: int = _env_int("DAILY_COLLECTION_RETRY_JOIN_TIMEOUT_MINUTES", 240)
# Teto por processadora. A rodada inteira leva ~3h para 12 processadoras; a mais
# longa (ConsigFácil, 10 convênios + retry técnico de lote) fica bem abaixo de
# 60min. Estourou → o scraper pendurou no Playwright (incidentes de 24/07 e
# 15-16/08/2026): converte o travamento infinito em UMA falha registrada.
PROCESSADORA_TIMEOUT_MINUTES: int = _env_int("DAILY_COLLECTION_PROCESSADORA_TIMEOUT_MINUTES", 60)

# Serializa a execução real de scrapers/storage entre threads de retry.
# As esperas (time.sleep do delay) ficam de fora do lock e correm em paralelo;
# apenas a chamada ao orchestrator é serializada.
_exec_lock = threading.Lock()


def _aguardar_retries(threads: list[threading.Thread], timeout_s: float) -> list[str]:
    """Join com deadline global compartilhado. Retorna os nomes das ainda vivas."""
    deadline = time.monotonic() + timeout_s
    for t in threads:
        restante = deadline - time.monotonic()
        if restante <= 0:
            break
        t.join(timeout=restante)
    return [t.name for t in threads if t.is_alive()]


# ── Rastreamento de resultado por processadora ────────────────────────────────

class _ResultadoProcessadora:
    def __init__(
        self,
        processadora: str,
        known_failure: bool = False,
        known_failure_reason: str | None = None,
    ) -> None:
        self.processadora = processadora
        self.status: str = "pendente"
        self.tentativas: int = 0
        self.erro: str | None = None
        self.known_failure = known_failure
        self.known_failure_reason = known_failure_reason
        self.bundle = None  # último ResultadoColeta (para o e-mail agregado)

    @property
    def falhou(self) -> bool:
        return self.status in ("erro", "pendente")

    def registrar_sucesso(self, status_orchestrator: str) -> None:
        self.tentativas += 1
        self.status = "ok"
        self.erro = None
        if status_orchestrator == CollectionStatus.PARTIAL_SUCCESS:
            self.status = "partial_success"

    def registrar_erro(self, erro: str) -> None:
        self.tentativas += 1
        self.status = "erro"
        self.erro = erro

    def to_dict(self) -> dict:
        return {
            "processadora": self.processadora,
            "status": self.status,
            "tentativas": self.tentativas,
            "erro": self.erro,
            "known_failure": self.known_failure,
            "known_failure_reason": self.known_failure_reason,
        }


# ── Watchdog por processadora ─────────────────────────────────────────────────

def _ppid_de_stat(raw: str) -> int | None:
    """Extrai o ppid de uma linha de /proc/<pid>/stat.

    Formato: "pid (comm) state ppid ..." — comm pode conter espaço e até
    parêntese; o rsplit no ÚLTIMO ')' isola os campos com segurança.
    """
    try:
        return int(raw.rsplit(")", 1)[1].split()[1])
    except (IndexError, ValueError):
        return None


def _mapa_ppid() -> dict[int, int]:
    """pid→ppid de todos os processos visíveis em /proc (POSIX)."""
    mapa: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "r") as fh:
                raw = fh.read()
        except OSError:
            continue  # processo morreu entre o listdir e o open
        ppid = _ppid_de_stat(raw)
        if ppid is not None:
            mapa[int(entry)] = ppid
    return mapa


def _descendentes(pid_raiz: int, ppid_por_pid: dict[int, int]) -> list[int]:
    """Descendentes (diretos e indiretos) de pid_raiz segundo o mapa pid→ppid."""
    filhos: dict[int, list[int]] = {}
    for pid, ppid in ppid_por_pid.items():
        filhos.setdefault(ppid, []).append(pid)
    resultado: list[int] = []
    fila = [pid_raiz]
    while fila:
        for filho in filhos.get(fila.pop(), []):
            resultado.append(filho)
            fila.append(filho)
    return resultado


def _matar_scraper_travado(processadora: str, timeout_min: int) -> None:
    """Mata a árvore de processos da coleta atual para desbloquear a rodada.

    Com o driver Node/Chrome mortos, a chamada síncrona do Playwright levanta
    exceção (transport fechado), a thread presa desbloqueia e o runner registra
    a falha e segue para a próxima processadora — em vez de pendurar a rodada
    inteira até o watchdog global do scheduler. Como a execução é serializada
    por _exec_lock, todo descendente deste processo pertence ao scraper atual.
    """
    if os.name != "posix":
        logger.error(
            "[Runner] ⏱ %s excedeu %d min — sem suporte a kill de árvore fora do "
            "POSIX; a rodada segue presa até o watchdog global.",
            processadora, timeout_min,
        )
        return
    try:
        alvos = _descendentes(os.getpid(), _mapa_ppid())
    except OSError:
        logger.exception("[Runner] ⏱ %s — falha ao mapear processos para o kill.", processadora)
        return
    mortos = 0
    for pid in alvos:
        try:
            # getattr: SIGKILL não existe no Windows (este ramo é POSIX-only,
            # mas o fallback evita AttributeError em importações/testes).
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            mortos += 1
        except (ProcessLookupError, PermissionError):
            continue
    logger.error(
        "[Runner] ⏱ %s excedeu %d min — %d processo(s) do scraper eliminado(s) "
        "para desbloquear a rodada.",
        processadora, timeout_min, mortos,
    )


# ── Execução individual ───────────────────────────────────────────────────────

def _executar_processadora(
    orchestrator: ColetaOrchestrator,
    resultado: _ResultadoProcessadora,
) -> bool:
    """Chama orchestrator.executar() e registra o resultado. Retorna True se não falhou completamente.

    A execução real roda sob _exec_lock para garantir que nunca haja dois
    scrapers/escritas no storage simultâneos, mesmo com retries paralelos.
    """
    tentativa = resultado.tentativas + 1
    with _exec_lock:
        logger.info(
            "[Runner] → %s (tentativa %d)", resultado.processadora, tentativa
        )
        # Watchdog local: um scraper pendurado no Playwright não pode segurar a
        # rodada até o teto global de 420min (foi o que abortou 15-16/08/2026).
        watchdog = threading.Timer(
            PROCESSADORA_TIMEOUT_MINUTES * 60,
            _matar_scraper_travado,
            args=(resultado.processadora, PROCESSADORA_TIMEOUT_MINUTES),
        )
        watchdog.daemon = True
        watchdog.start()
        try:
            # coletar() persiste tudo mas NÃO envia e-mail — o resumo diário é
            # enviado UMA vez ao final (agregado), não por processadora.
            # Caminho agendado → ativa retentativa rápida de lote em erro técnico
            # (transitório); o retry com RETRY_DELAY de 60min deste runner segue
            # cobrindo falhas sustentadas, em cima disso.
            bundle = orchestrator.coletar(resultado.processadora, retentar_tecnico=True)
            resultado.bundle = bundle
            execucao = bundle.execucao

            if execucao.status == CollectionStatus.ERROR:
                resultado.registrar_erro(
                    f"Todos os convênios falharam — erros: "
                    + ", ".join(
                        f"{e.get('convenio_key')}: {e.get('erro', '')}"
                        for e in (execucao.erros or [])
                    )[:200]
                )
                logger.warning(
                    "[Runner] ✗ %s — status=error (%d/%d convênios com falha)",
                    resultado.processadora,
                    execucao.error_count,
                    execucao.total_convenios,
                )
                return False

            resultado.registrar_sucesso(execucao.status)
            logger.info(
                "[Runner] ✓ %s — status=%s sucesso=%d/%d",
                resultado.processadora,
                execucao.status,
                execucao.success_count,
                execucao.total_convenios,
            )
            return True

        except Exception as exc:
            resultado.registrar_erro(str(exc)[:300])
            logger.error("[Runner] ✗ %s — exceção: %s", resultado.processadora, exc)
            return False

        finally:
            watchdog.cancel()


# ── Rodada de execução (principal ou retry) ───────────────────────────────────

def _executar_rodada(
    orchestrator: ColetaOrchestrator,
    resultados: dict[str, _ResultadoProcessadora],
    processadoras: list[str],
    intervalo_segundos: int,
    label: str,
) -> list[str]:
    """Executa uma lista de processadoras com intervalo entre elas.

    Retorna as chaves das que falharam completamente (para eventual retry).
    """
    falhas: list[str] = []
    total = len(processadoras)

    logger.info("[Runner] ┌── %s (%d processadoras) ──", label, total)

    for i, processadora_key in enumerate(processadoras):
        sucesso = _executar_processadora(orchestrator, resultados[processadora_key])
        if not sucesso:
            falhas.append(processadora_key)

        if i < total - 1 and intervalo_segundos > 0:
            logger.info(
                "[Runner] │   aguardando %ds antes da próxima...", intervalo_segundos
            )
            time.sleep(intervalo_segundos)

    logger.info(
        "[Runner] └── %s concluída — ok: %d | falha: %d",
        label, total - len(falhas), len(falhas),
    )
    return falhas


# ── Retry não-bloqueante (uma thread por processadora que falhou) ──────────────

def _retry_loop_processadora(
    orchestrator: ColetaOrchestrator,
    resultado: _ResultadoProcessadora,
    delay_segundos: int,
    max_retries: int,
) -> None:
    """Ciclo de retry de UMA processadora, rodando em sua própria thread.

    Espera `delay_segundos` (fora do lock, em paralelo com outras threads),
    então retenta sob _exec_lock. Repete até ter sucesso ou esgotar max_retries.
    """
    for retry_num in range(1, max_retries + 1):
        logger.info(
            "[Runner] ⏳ %s — aguardando %ds para retry %d/%d (em paralelo)...",
            resultado.processadora, delay_segundos, retry_num, max_retries,
        )
        time.sleep(delay_segundos)

        logger.info(
            "[Runner] ↻ %s — iniciando retry %d/%d", resultado.processadora, retry_num, max_retries
        )
        if _executar_processadora(orchestrator, resultado):
            logger.info(
                "[Runner] ✓ %s — recuperado no retry %d/%d",
                resultado.processadora, retry_num, max_retries,
            )
            return

    logger.warning(
        "[Runner] ✗ %s — falha persistente após %d retry(ies)",
        resultado.processadora, max_retries,
    )


# ── Resumo ────────────────────────────────────────────────────────────────────

def _salvar_resumo(
    inicio: datetime,
    fim: datetime,
    resultados: dict[str, _ResultadoProcessadora],
    retries_executados: int,
) -> Path:
    data_str = inicio.strftime("%Y-%m-%d")
    duracao = round((fim - inicio).total_seconds() / 60, 1)

    total = len(resultados)
    sucessos = sum(1 for r in resultados.values() if not r.falhou)
    falhas_persistentes = sum(1 for r in resultados.values() if r.falhou)

    resumo = {
        "data": data_str,
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "duracao_minutos": duracao,
        "total_processadoras": total,
        "sucesso": sucessos,
        "falha_persistente": falhas_persistentes,
        "max_retries_configurado": MAX_RETRIES,
        "retries_executados": retries_executados,
        "intervalo_minutos": INTERVAL_MINUTES,
        "retry_delay_minutos": RETRY_DELAY_MINUTES,
        "processadoras": [r.to_dict() for r in resultados.values()],
    }

    runs_dir = Path(settings.STORAGE_PATH) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    caminho = runs_dir / f"{data_str}.json"
    caminho.write_text(
        json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return caminho


def _imprimir_resumo(
    resultados: dict[str, _ResultadoProcessadora],
    caminho: Path,
    inicio: datetime,
    fim: datetime,
) -> None:
    total = len(resultados)
    sucessos = sum(1 for r in resultados.values() if not r.falhou)
    falhas = total - sucessos
    duracao = round((fim - inicio).total_seconds() / 60, 1)

    print()
    print("=" * 65)
    print("  RESUMO DA COLETA DIÁRIA")
    print("=" * 65)
    print(f"  Início:  {inicio.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  Fim:     {fim.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  Duração: {duracao} min")
    print()
    print(f"  Total:         {total} processadoras")
    print(f"  Sucesso:       {sucessos}")
    print(f"  Falha persist: {falhas}")
    print()

    for r in resultados.values():
        if r.falhou and r.known_failure:
            icon = "⊘"
        elif r.falhou:
            icon = "✗"
        else:
            icon = "✓"
        extra = f" (tentativas: {r.tentativas})" if r.tentativas > 1 else ""
        conhecida = " [falha conhecida — retry pulado]" if (r.falhou and r.known_failure) else ""
        status_label = f"[{r.status}]" if r.status != "ok" else ""
        print(f"  {icon} {r.processadora:<22} {status_label}{extra}{conhecida}")
        if r.erro:
            print(f"      └ {r.erro[:90]}")

    print()
    print(f"  Resumo salvo em: {caminho}")
    print("=" * 65)
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    inicio = datetime.now(timezone.utc)

    config = load_processadoras_config()
    processadoras_keys = list(config["processadoras"].keys())

    logger.info(
        "[Runner] Iniciando coleta diária — %d processadoras | "
        "intervalo=%dmin | max_retries=%d | retry_delay=%dmin",
        len(processadoras_keys),
        INTERVAL_MINUTES,
        MAX_RETRIES,
        RETRY_DELAY_MINUTES,
    )

    # Fail-fast: com Postgres, garante DB acessível + schema na head ANTES de
    # iniciar a coleta — senão a falha só apareceria na 1ª query, podendo
    # derrubar silenciosamente a rodada agendada.
    if settings.STORAGE_BACKEND.strip().lower() == "postgres":
        from app.storage import db
        db.assert_ready()

    orchestrator = build_orchestrator()
    intervalo_segundos = INTERVAL_MINUTES * 60
    resultados = {
        key: _ResultadoProcessadora(
            key,
            known_failure=bool(cfg.get("known_failure")),
            known_failure_reason=cfg.get("known_failure_reason"),
        )
        for key, cfg in config["processadoras"].items()
    }

    # Rodada principal
    falhas = _executar_rodada(
        orchestrator,
        resultados,
        processadoras_keys,
        intervalo_segundos,
        "Rodada principal",
    )

    # Retry não-bloqueante: cada falha ganha sua própria thread, que espera
    # RETRY_DELAY (em paralelo) e retenta sob _exec_lock. A rodada principal
    # não fica travada por um sleep monolítico; o processo só encerra após
    # todas as threads de retry concluírem.
    #
    # Processadoras com falha CONHECIDA (known_failure) são puladas: retentar
    # não adianta (erro externo permanente) e ainda desperdiça tempo/martela o
    # portal. Elas continuam contando como falha conhecida no resumo, mas não
    # entram no ciclo de retry.
    falhas_conhecidas = [k for k in falhas if resultados[k].known_failure]
    falhas_para_retry = [k for k in falhas if not resultados[k].known_failure]

    for k in falhas_conhecidas:
        logger.info(
            "[Runner] ⊘ %s — falha conhecida, retry pulado (%s)",
            k, resultados[k].known_failure_reason or "sem motivo registrado",
        )

    retry_delay_segundos = RETRY_DELAY_MINUTES * 60
    threads_retry: list[threading.Thread] = []
    if falhas_para_retry:
        logger.info(
            "[Runner] %d falha(s) — disparando retries em paralelo "
            "(delay=%dmin, max_retries=%d): %s",
            len(falhas_para_retry), RETRY_DELAY_MINUTES, MAX_RETRIES, falhas_para_retry,
        )
        for processadora_key in falhas_para_retry:
            t = threading.Thread(
                target=_retry_loop_processadora,
                args=(orchestrator, resultados[processadora_key], retry_delay_segundos, MAX_RETRIES),
                name=f"retry-{processadora_key}",
                # Daemon: uma thread travada (scraper pendurado) não pode impedir
                # o processo de encerrar depois que o dia é fechado.
                daemon=True,
            )
            threads_retry.append(t)
            t.start()

        abandonadas = _aguardar_retries(threads_retry, RETRY_JOIN_TIMEOUT_MINUTES * 60)
        if abandonadas:
            logger.error(
                "[Runner] %d thread(s) de retry travada(s) após %d min — abandonando: %s. "
                "Prosseguindo com o resumo e o e-mail do dia.",
                len(abandonadas), RETRY_JOIN_TIMEOUT_MINUTES, ", ".join(abandonadas),
            )

    # E-mail diário ÚNICO consolidando TODAS as processadoras desta rodada
    # (inclui o resultado final de cada uma após os retries).
    bundles = [r.bundle for r in resultados.values() if r.bundle is not None]
    try:
        orchestrator.notificar_agregado(bundles)
        logger.info("[Runner] Resumo diário agregado enviado (%d processadoras).", len(bundles))
    except Exception:
        logger.exception("[Runner] Falha ao enviar resumo diário agregado")

    # Nº total de re-execuções efetivamente realizadas (tentativas além da 1ª).
    retries_executados = sum(max(0, r.tentativas - 1) for r in resultados.values())

    fim = datetime.now(timezone.utc)
    caminho = _salvar_resumo(inicio, fim, resultados, retries_executados)
    _imprimir_resumo(resultados, caminho, inicio, fim)

    # Falhas que continuam após os retries, separadas em conhecidas vs inesperadas.
    falhas_conhecidas_final = [k for k, r in resultados.items() if r.falhou and r.known_failure]
    falhas_inesperadas = [k for k, r in resultados.items() if r.falhou and not r.known_failure]

    if falhas_conhecidas_final:
        logger.info(
            "[Runner] %d falha(s) conhecida(s) (esperadas, não acionáveis): %s",
            len(falhas_conhecidas_final),
            falhas_conhecidas_final,
        )

    # Só falhas INESPERADAS sinalizam erro (exit 1) — evita alarme diário por
    # erros externos já conhecidos e documentados.
    if falhas_inesperadas:
        logger.warning(
            "[Runner] Coleta concluída com %d falha(s) inesperada(s): %s",
            len(falhas_inesperadas),
            falhas_inesperadas,
        )
        healthcheck.pingar(sucesso=False)   # dead-man's switch: rodou, mas com falha
        return 1

    logger.info("[Runner] Coleta diária concluída sem falhas inesperadas.")
    healthcheck.pingar(sucesso=True)        # dead-man's switch: ciclo concluído ok
    return 0


if __name__ == "__main__":
    sys.exit(main())
