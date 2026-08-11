"""Regressão do incidente de 24/07/2026: coleta travada penhorava o scheduler.

Uma thread de retry pendurou dentro do Playwright e o `t.join()` sem timeout
nunca retornou; com max_instances=1, o APScheduler pulou todas as rodadas
seguintes por 18 dias em silêncio. Estes testes garantem as duas camadas de
proteção:

1. `_aguardar_retries` — join com deadline abandona threads travadas.
2. `run_scheduler_service._coletar` — subprocess com timeout duro sempre retorna.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_daily_collection
import run_scheduler_service


# ── Camada 1: join com deadline no runner ─────────────────────────────────────

def test_aguardar_retries_abandona_thread_travada():
    """Thread que nunca termina não pode travar o fechamento do dia."""
    trava = threading.Event()  # nunca setado — simula o Playwright pendurado
    t = threading.Thread(target=trava.wait, name="retry-travada", daemon=True)
    t.start()

    inicio = time.monotonic()
    abandonadas = run_daily_collection._aguardar_retries([t], timeout_s=0.2)
    duracao = time.monotonic() - inicio

    assert abandonadas == ["retry-travada"]
    assert duracao < 5, "join deveria desistir no deadline, não esperar para sempre"


def test_aguardar_retries_normal_retorna_vazio():
    threads = [
        threading.Thread(target=lambda: None, name=f"retry-ok-{i}", daemon=True)
        for i in range(3)
    ]
    for t in threads:
        t.start()

    assert run_daily_collection._aguardar_retries(threads, timeout_s=5.0) == []


def test_aguardar_retries_deadline_e_global_nao_por_thread():
    """3 threads travadas não podem custar 3× o deadline."""
    trava = threading.Event()
    threads = [
        threading.Thread(target=trava.wait, name=f"retry-travada-{i}", daemon=True)
        for i in range(3)
    ]
    for t in threads:
        t.start()

    inicio = time.monotonic()
    abandonadas = run_daily_collection._aguardar_retries(threads, timeout_s=0.3)
    duracao = time.monotonic() - inicio

    assert len(abandonadas) == 3
    assert duracao < 1.0, "deadline é compartilhado entre as threads, não somado"


# ── Camada 2: watchdog de subprocess no scheduler ─────────────────────────────

def test_coletar_mata_subprocess_que_excede_timeout(tmp_path, monkeypatch):
    """O job do APScheduler SEMPRE retorna — mesmo com a coleta pendurada."""
    script_travado = tmp_path / "coleta_travada.py"
    script_travado.write_text("import time; time.sleep(600)", encoding="utf-8")

    monkeypatch.setattr(run_scheduler_service, "_SCRIPT_COLETA", script_travado)
    monkeypatch.setenv("COLETA_TIMEOUT_MINUTES", "0.03")  # ~1.8s
    # Alerta best-effort vira no-op: sem SMTP nem destinatários configurados.
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("NOTIFICACAO_DESTINATARIOS", "")
    monkeypatch.setenv("HEALTHCHECK_URL", "")

    inicio = time.monotonic()
    run_scheduler_service._coletar()  # não pode lançar nem bloquear
    duracao = time.monotonic() - inicio

    assert duracao < 60, "watchdog deveria abortar em ~2s, não esperar os 600s"


def test_coletar_conclui_subprocess_rapido_sem_matar(tmp_path, monkeypatch, caplog):
    script_ok = tmp_path / "coleta_ok.py"
    script_ok.write_text("print('ok')", encoding="utf-8")

    monkeypatch.setattr(run_scheduler_service, "_SCRIPT_COLETA", script_ok)
    monkeypatch.setenv("COLETA_TIMEOUT_MINUTES", "1")

    with caplog.at_level("INFO", logger="run_scheduler_service"):
        run_scheduler_service._coletar()

    assert any("Coleta finalizada (exit=0)" in r.message for r in caplog.records)


def test_timeout_invalido_usa_default(monkeypatch):
    monkeypatch.setenv("COLETA_TIMEOUT_MINUTES", "banana")
    assert run_scheduler_service._timeout_minutos() == run_scheduler_service._TIMEOUT_DEFAULT_MIN

    monkeypatch.setenv("COLETA_TIMEOUT_MINUTES", "-5")
    assert run_scheduler_service._timeout_minutos() == run_scheduler_service._TIMEOUT_DEFAULT_MIN

    monkeypatch.setenv("COLETA_TIMEOUT_MINUTES", "90")
    assert run_scheduler_service._timeout_minutos() == 90.0
