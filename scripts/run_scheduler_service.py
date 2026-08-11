"""Serviço de agendamento do runner diário (container dedicado).

Mantém o processo vivo e dispara `run_daily_collection.py` no horário
`COLETA_HORARIO` (HH:MM), todos os dias. Reaproveita TODA a lógica do runner
(retry paralelo + known_failure) — este wrapper só agenda.

A coleta roda em SUBPROCESS com timeout duro (watchdog). Motivo: em 24/07/2026
uma coleta travou dentro do Playwright e nunca retornou; com max_instances=1,
o APScheduler pulou TODAS as rodadas seguintes por 18 dias, em silêncio. O
subprocess garante que o job sempre retorna: estourou o tempo → mata o grupo
de processos inteiro (Chrome incluso), pinga o dead-man's switch em /fail e
alerta por e-mail (best-effort).

Variáveis de ambiente:
    COLETA_HORARIO           Horário diário da coleta no formato HH:MM (ex: "06:00").
    RUN_ON_START             Se "true", roda uma coleta imediatamente no boot.
    COLETA_TIMEOUT_MINUTES   Watchdog: teto em minutos por rodada (default: 420).

Comportamento:
    - RUN_ON_START=true            → coleta imediata no boot.
    - COLETA_HORARIO definido      → agenda diariamente e bloqueia.
    - Nenhum dos dois              → loga aviso e encerra (nada a fazer).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

# raiz do projeto + diretório scripts/ no path (para importar run_daily_collection)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_scheduler_service")

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

_RAIZ = Path(__file__).resolve().parents[1]
_SCRIPT_COLETA = Path(__file__).resolve().parent / "run_daily_collection.py"
_TIMEOUT_DEFAULT_MIN = 420.0  # 7h — rodada normal leva ~3h; margem p/ retries de 60min


def _bool_env(key: str) -> bool:
    return os.getenv(key, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _timeout_minutos() -> float:
    raw = os.getenv("COLETA_TIMEOUT_MINUTES", "").strip()
    if not raw:
        return _TIMEOUT_DEFAULT_MIN
    try:
        valor = float(raw)
        if valor <= 0:
            raise ValueError(raw)
        return valor
    except ValueError:
        logger.warning(
            "COLETA_TIMEOUT_MINUTES inválido: %r — usando default %.0f.",
            raw, _TIMEOUT_DEFAULT_MIN,
        )
        return _TIMEOUT_DEFAULT_MIN


def _matar_arvore(proc: subprocess.Popen) -> None:
    """Mata o subprocess e todos os filhos (Chrome/Playwright) via process group."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        return
    except Exception:
        logger.exception("[SchedulerService] Falha ao matar o processo da coleta.")
    try:
        proc.wait(timeout=30)
    except Exception:
        logger.warning("[SchedulerService] Processo da coleta não confirmou encerramento.")


def _alertar_travamento(minutos: float) -> None:
    """Best-effort: ping /fail no dead-man's switch + e-mail de alerta. Nunca lança."""
    try:
        from app.services import healthcheck
        healthcheck.pingar(sucesso=False)
    except Exception:
        logger.exception("[SchedulerService] Falha ao pingar o healthcheck.")

    try:
        import smtplib
        from email.mime.text import MIMEText

        host = os.getenv("SMTP_HOST", "").strip()
        destinatarios = [
            d.strip() for d in os.getenv("NOTIFICACAO_DESTINATARIOS", "").split(",") if d.strip()
        ]
        if not host or not destinatarios:
            return
        usuario = os.getenv("SMTP_USER", "")
        msg = MIMEText(
            f"A coleta diária excedeu o teto de {minutos:.0f} minutos e foi ABORTADA "
            "pelo watchdog do scheduler.\n\n"
            "O agendamento segue ativo — a próxima rodada dispara normalmente no "
            "horário de COLETA_HORARIO. Verifique os logs do runner para identificar "
            "onde a coleta travou:\n"
            "    docker logs monitor-cortes-runner-1\n",
            "plain",
            "utf-8",
        )
        msg["Subject"] = "[Monitor Datas de Corte] ALERTA: coleta abortada por timeout"
        msg["From"] = usuario
        msg["To"] = ", ".join(destinatarios)
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as smtp:
            smtp.ehlo()
            if os.getenv("SMTP_USE_TLS", "true").strip().lower() not in {"false", "0", "no"}:
                smtp.starttls()
                smtp.ehlo()
            smtp.login(usuario, os.getenv("SMTP_PASSWORD", ""))
            smtp.sendmail(usuario, destinatarios, msg.as_string())
        logger.info("[SchedulerService] E-mail de alerta de timeout enviado.")
    except Exception:
        logger.exception("[SchedulerService] Falha ao enviar e-mail de alerta de timeout.")


def _parse_horario(horario: str) -> tuple[int, int]:
    partes = horario.strip().split(":")
    if len(partes) != 2:
        raise ValueError(horario)
    hora, minuto = int(partes[0]), int(partes[1])
    if not (0 <= hora <= 23 and 0 <= minuto <= 59):
        raise ValueError(horario)
    return hora, minuto


def _coletar() -> None:
    timeout_s = _timeout_minutos() * 60
    logger.info(
        "[SchedulerService] Disparando coleta diária (watchdog: %.0f min)...", timeout_s / 60
    )
    kwargs: dict = {}
    if os.name == "posix":
        # Sessão própria → o subprocess vira líder de process group; o kill
        # atinge também os filhos (Chrome) e não sobra zumbi segurando a rodada.
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen([sys.executable, str(_SCRIPT_COLETA)], cwd=str(_RAIZ), **kwargs)
    except Exception:
        logger.exception("[SchedulerService] Falha ao iniciar o processo da coleta.")
        return
    try:
        codigo = proc.wait(timeout=timeout_s)
        logger.info("[SchedulerService] Coleta finalizada (exit=%s).", codigo)
    except subprocess.TimeoutExpired:
        logger.error(
            "[SchedulerService] Coleta excedeu %.0f min — abortando para liberar a próxima rodada.",
            timeout_s / 60,
        )
        _matar_arvore(proc)
        _alertar_travamento(timeout_s / 60)
    except Exception:
        logger.exception("[SchedulerService] Coleta falhou com exceção não tratada.")


def main() -> int:
    horario = os.getenv("COLETA_HORARIO", "").strip()
    run_on_start = _bool_env("RUN_ON_START")

    if run_on_start:
        logger.info("[SchedulerService] RUN_ON_START ativo — coleta imediata.")
        _coletar()

    if not horario:
        if run_on_start:
            logger.info("[SchedulerService] COLETA_HORARIO vazio — encerrando após coleta única.")
            return 0
        logger.warning(
            "[SchedulerService] COLETA_HORARIO vazio e RUN_ON_START desligado — nada a agendar. Encerrando."
        )
        return 0

    try:
        hora, minuto = _parse_horario(horario)
    except ValueError:
        logger.error("[SchedulerService] COLETA_HORARIO inválido: %r. Use HH:MM.", horario)
        return 1

    scheduler = BlockingScheduler()
    scheduler.add_job(
        _coletar,
        trigger=CronTrigger(hour=hora, minute=minuto),
        id="coleta_diaria",
        replace_existing=True,
        # Disparo atrasado (processo ocupado no horário exato) ainda roda em até
        # 1h, em vez de ser descartado em silêncio. Com o watchdog, o job sempre
        # retorna — max_instances=1 nunca mais bloqueia indefinidamente.
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info("[SchedulerService] Agendado: coleta diária às %02d:%02d. Aguardando...", hora, minuto)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("[SchedulerService] Encerrando.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
