"""Regressão dos incidentes de coleta travada/vazamento de processos.

24/07/2026 — uma thread de retry pendurou dentro do Playwright e o `t.join()`
sem timeout nunca retornou; com max_instances=1, o APScheduler pulou todas as
rodadas seguintes por 18 dias em silêncio.

15-17/08/2026 — o watchdog abortou as rodadas de sáb/dom com killpg, mas o
Chrome sobrevive (Playwright o lança com detached=true, process group próprio);
os órfãos acumularam no container até todo launch falhar com EAGAIN
("Resource temporarily unavailable (11)") na segunda-feira.

Camadas de proteção testadas:

1. `_aguardar_retries` — join com deadline abandona threads travadas.
2. `run_scheduler_service._coletar` — subprocess com timeout duro sempre retorna.
3. `run_scheduler_service._varrer_remanescentes` — pós-rodada, elimina browsers
   órfãos e colhe zumbis; só roda dentro do container.
4. watchdog por processadora do runner — mata a árvore do scraper travado para
   desbloquear a rodada antes do teto global.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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


# ── Camada 3: varredura de processos remanescentes (incidente 15-17/08) ───────

def test_pids_alvo_varredura_preserva_init_e_scheduler():
    pids = [1, 7, 42, 999]
    assert run_scheduler_service._pids_alvo_varredura(pids, pid_proprio=42) == [7, 999]


def test_deve_varrer_nunca_fora_do_posix(monkeypatch):
    monkeypatch.setattr(run_scheduler_service.os, "name", "nt")
    assert run_scheduler_service._deve_varrer() is False


def test_deve_varrer_exige_container(monkeypatch):
    """POSIX mas fora de container (sem /.dockerenv, PID != 1) → não varre."""
    monkeypatch.setattr(run_scheduler_service.os, "name", "posix")

    class _PathSemDockerenv:
        def __init__(self, *_args):
            pass

        def exists(self):
            return False

    monkeypatch.setattr(run_scheduler_service, "Path", _PathSemDockerenv)
    assert run_scheduler_service._deve_varrer() is False


def test_deve_varrer_dentro_do_container(monkeypatch):
    monkeypatch.setattr(run_scheduler_service.os, "name", "posix")

    class _PathComDockerenv:
        def __init__(self, *_args):
            pass

        def exists(self):
            return True

    monkeypatch.setattr(run_scheduler_service, "Path", _PathComDockerenv)
    assert run_scheduler_service._deve_varrer() is True


def test_varrer_remanescentes_e_noop_fora_do_container(monkeypatch):
    """Rodando em dev (fora do container), a varredura não mata NADA."""
    mortos: list[int] = []
    monkeypatch.setattr(run_scheduler_service, "_deve_varrer", lambda: False)
    monkeypatch.setattr(run_scheduler_service.os, "kill", lambda pid, sig: mortos.append(pid))

    run_scheduler_service._varrer_remanescentes()

    assert mortos == []


def test_varrer_remanescentes_mata_todos_menos_init_e_self(monkeypatch):
    """No container: elimina todo PID exceto o init (1) e o próprio scheduler."""
    pid_proprio = 5
    mortos: list[int] = []

    monkeypatch.setattr(run_scheduler_service, "_deve_varrer", lambda: True)
    monkeypatch.setattr(run_scheduler_service.os, "getpid", lambda: pid_proprio)
    monkeypatch.setattr(
        run_scheduler_service.os, "listdir",
        lambda _path: ["1", "5", "77", "312", "self", "net"],
    )

    def _kill(pid, _sig):
        if pid == 312:  # processo já morreu entre o listdir e o kill
            raise ProcessLookupError(pid)
        mortos.append(pid)

    monkeypatch.setattr(run_scheduler_service.os, "kill", _kill)

    def _waitpid(_pid, _flags):
        raise ChildProcessError

    monkeypatch.setattr(run_scheduler_service.os, "waitpid", _waitpid)

    run_scheduler_service._varrer_remanescentes()

    assert mortos == [77]


def test_coletar_varre_apos_rodada_normal(tmp_path, monkeypatch):
    """A varredura roda após TODA rodada — é ela que impede o acúmulo de órfãos."""
    script_ok = tmp_path / "coleta_ok.py"
    script_ok.write_text("print('ok')", encoding="utf-8")

    varridas: list[bool] = []
    monkeypatch.setattr(run_scheduler_service, "_SCRIPT_COLETA", script_ok)
    monkeypatch.setenv("COLETA_TIMEOUT_MINUTES", "1")
    monkeypatch.setattr(
        run_scheduler_service, "_varrer_remanescentes", lambda: varridas.append(True)
    )

    run_scheduler_service._coletar()

    assert varridas, "varredura pós-rodada não foi chamada na conclusão normal"


def test_coletar_varre_apos_abort_do_watchdog(tmp_path, monkeypatch):
    """Regressão direta de 15-17/08: abort do watchdog TEM que varrer os órfãos."""
    script_travado = tmp_path / "coleta_travada.py"
    script_travado.write_text("import time; time.sleep(600)", encoding="utf-8")

    varridas: list[bool] = []
    monkeypatch.setattr(run_scheduler_service, "_SCRIPT_COLETA", script_travado)
    monkeypatch.setenv("COLETA_TIMEOUT_MINUTES", "0.03")  # ~1.8s
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("NOTIFICACAO_DESTINATARIOS", "")
    monkeypatch.setenv("HEALTHCHECK_URL", "")
    monkeypatch.setattr(
        run_scheduler_service, "_varrer_remanescentes", lambda: varridas.append(True)
    )

    run_scheduler_service._coletar()

    assert varridas, "varredura pós-rodada não foi chamada no abort do watchdog"


# ── Camada 4: watchdog por processadora no runner ─────────────────────────────

def test_ppid_de_stat_tolera_comm_com_parenteses():
    assert run_daily_collection._ppid_de_stat("123 (chrome (renderer)) R 45 6 7") == 45
    assert run_daily_collection._ppid_de_stat("9 (python3) S 1 9 9") == 1
    assert run_daily_collection._ppid_de_stat("lixo sem formato") is None


def test_descendentes_diretos_e_indiretos():
    # 1 ── 2 ── 3 ── 5          4 é filho de 2; 10 é de outra árvore
    ppid = {2: 1, 3: 2, 4: 2, 5: 3, 10: 1}
    assert sorted(run_daily_collection._descendentes(2, ppid)) == [3, 4, 5]
    assert run_daily_collection._descendentes(5, ppid) == []


def _bundle_ok():
    return SimpleNamespace(
        execucao=SimpleNamespace(
            status="ok", success_count=1, total_convenios=1, error_count=0, erros=[]
        )
    )


def test_watchdog_processadora_dispara_para_scraper_travado(monkeypatch):
    """Scraper que passa do teto tem sua árvore de processos morta (recorder)."""
    disparos: list[str] = []
    monkeypatch.setattr(run_daily_collection, "PROCESSADORA_TIMEOUT_MINUTES", 0.005)  # 0.3s
    monkeypatch.setattr(
        run_daily_collection, "_matar_scraper_travado",
        lambda processadora, _timeout: disparos.append(processadora),
    )

    class _OrchestratorLento:
        def coletar(self, _key, retentar_tecnico=True):
            time.sleep(0.8)  # além do teto de 0.3s
            return _bundle_ok()

    resultado = run_daily_collection._ResultadoProcessadora("travadora")
    ok = run_daily_collection._executar_processadora(_OrchestratorLento(), resultado)

    assert ok is True
    assert disparos == ["travadora"]


def test_watchdog_processadora_cancelado_em_conclusao_rapida(monkeypatch):
    """Coleta dentro do teto NÃO pode disparar o kill depois de concluída."""
    disparos: list[str] = []
    monkeypatch.setattr(run_daily_collection, "PROCESSADORA_TIMEOUT_MINUTES", 0.005)  # 0.3s
    monkeypatch.setattr(
        run_daily_collection, "_matar_scraper_travado",
        lambda processadora, _timeout: disparos.append(processadora),
    )

    class _OrchestratorRapido:
        def coletar(self, _key, retentar_tecnico=True):
            return _bundle_ok()

    resultado = run_daily_collection._ResultadoProcessadora("rapida")
    ok = run_daily_collection._executar_processadora(_OrchestratorRapido(), resultado)
    time.sleep(0.5)  # deixa o timer (cancelado) vencer o prazo original

    assert ok is True
    assert disparos == []


def test_matar_scraper_travado_fora_do_posix_apenas_loga(caplog):
    """Em dev Windows não há kill de árvore — não pode lançar exceção."""
    if run_daily_collection.os.name == "posix":
        return  # ramo específico do Windows; no Linux o caminho real é testado acima

    with caplog.at_level("ERROR", logger="run_daily_collection"):
        run_daily_collection._matar_scraper_travado("qualquer", 60)

    assert any("sem suporte" in r.message for r in caplog.records)
