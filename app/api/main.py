from __future__ import annotations

import base64
import logging
import logging.config
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from app.core.enums import EventoTipo
from app.core.loader import load_processadoras_config
from app.core.models import DadoCorte, Execucao
from app.services.confianca import JANELA_DIAS, classificar_confianca, mudou_dia_corte
from app.services import metricas
from app.core.settings import settings
from app.services.notification.smtp import EmailSMTPNotificador
from app.services.orchestrator_factory import build_orchestrator, build_repositories
from app.services.scheduler import SchedulerService
from app.utils.dates import derivar_competencia, normalizar_data_corte


logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        }
    },
    "root": {"level": "INFO", "handlers": ["console"]},
})


logger = logging.getLogger(__name__)

def _validar_config_seguranca() -> None:
    """Fail-closed: recusa subir aberto quando a auth foi declarada obrigatória.

    Chamado no boot (lifespan). Levanta RuntimeError para abortar o start em vez de
    subir a API/painel sem autenticação num deploy exposto.
    """
    if settings.AUTH_REQUIRED and not settings.PANEL_PASSWORD:
        raise RuntimeError(
            "AUTH_REQUIRED=True mas PANEL_PASSWORD está vazia — recuso subir com a "
            "API/painel ABERTOS. Defina PANEL_PASSWORD no .env (ou AUTH_REQUIRED=False "
            "só em dev local)."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validar_config_seguranca()
    if not settings.SMTP_HOST:
        logger.warning("SMTP_HOST não configurado — notificações por e-mail desabilitadas")
    if settings.REMESSAS_ENABLED:
        # Falha ALTO no boot se as migrations não estão na head (módulo transacional).
        from app.storage import db as _db
        _db.assert_ready()
        logger.info("Módulo de remessas HABILITADO (Postgres na head)")
        if not settings.COOKIE_SECURE:
            logger.warning(
                "COOKIE_SECURE desligado — cookies de sessão trafegam em HTTP puro. "
                "Ligue em produção (atrás de TLS).")
    else:
        logger.info("Módulo de remessas desabilitado (STORAGE_BACKEND != postgres)")
    if settings.PANEL_PASSWORD:
        logger.info("Auth do painel/API HABILITADA (HTTP Basic, usuário '%s')", settings.PANEL_USER)
    else:
        logger.warning("Auth do painel/API DESABILITADA (PANEL_PASSWORD não setada) — API/painel ABERTOS")
    scheduler = SchedulerService(
        horario=settings.COLETA_HORARIO,
        orchestrator_factory=build_orchestrator,
    )
    scheduler.iniciar()
    sync_scheduler = None
    if settings.REMESSAS_ENABLED:
        # Sync horário monitor → ciclos de remessa (além do botão manual no painel).
        from apscheduler.schedulers.background import BackgroundScheduler
        from app.services.remessas_sync import job_sync_periodico
        sync_scheduler = BackgroundScheduler()
        sync_scheduler.add_job(job_sync_periodico, "interval", hours=1, id="remessas_sync")
        sync_scheduler.start()
        logger.info("Sync de remessas agendado (a cada 1h)")
    yield
    scheduler.parar()
    if sync_scheduler is not None:
        sync_scheduler.shutdown(wait=False)


app = FastAPI(title="Pipeline Corte API", lifespan=lifespan)


# ── Auth básica (opt-in: ativa só quando PANEL_PASSWORD está setada) ───────────
_AUTH_EXEMPT = {"/health"}  # uptime / dead-man's switch pinga sem credencial
# /auth/*: login é público e o resto valida a própria sessão (evita o dialog Basic
# do navegador na tela de login). /painel: só os estáticos — os DADOS continuam gated.
_AUTH_EXEMPT_PREFIXES = ("/auth", "/painel")


def _rota_isenta(path: str) -> bool:
    return path in _AUTH_EXEMPT or any(
        path == p or path.startswith(p + "/") for p in _AUTH_EXEMPT_PREFIXES
    )


def _sessao_valida(request) -> bool:
    """Cookie de sessão do módulo de remessas vale como alternativa ao Basic
    (humanos logados navegam os endpoints do monitor sem credencial de máquina)."""
    if not settings.REMESSAS_ENABLED:
        return False
    try:
        from app.services import auth_service
        return auth_service.validar_sessao(request.cookies.get("sessao")) is not None
    except Exception:  # noqa: BLE001 — middleware nunca pode derrubar a request
        logger.exception("[auth] falha ao validar sessão no middleware")
        return False


def _credenciais_ok(auth_header: str | None) -> bool:
    if not auth_header or auth_header[:6].lower() != "basic ":
        return False
    try:
        usuario, _, senha = base64.b64decode(auth_header[6:]).decode("utf-8").partition(":")
    except Exception:  # noqa: BLE001 — header malformado = não autenticado
        return False
    # Compara em BYTES (compare_digest rejeita str não-ASCII → senha com acento daria
    # 500/lockout) e os DOIS campos, em tempo constante (sem timing/early-exit por usuário).
    u_ok = secrets.compare_digest(usuario.encode("utf-8"), settings.PANEL_USER.encode("utf-8"))
    s_ok = secrets.compare_digest(senha.encode("utf-8"), settings.PANEL_PASSWORD.encode("utf-8"))
    return u_ok and s_ok


async def _auth_basica(request, call_next):
    if (settings.PANEL_PASSWORD
            and request.method != "OPTIONS"
            and not _rota_isenta(request.url.path)
            and not _credenciais_ok(request.headers.get("Authorization"))
            and not _sessao_valida(request)):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Monitor de Cortes"'})
    return await call_next(request)


# ── Registro dos middlewares (ordem importa) ──────────────────────────────────
# Starlette: o ÚLTIMO add_middleware é o mais EXTERNO. Queremos, na entrada da
# request: SecurityHeaders → RateLimit → CORS → Auth → app. Então adicionamos do
# mais interno (auth) para o mais externo (headers).
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.security import rate_limit_middleware, security_headers_middleware

app.add_middleware(BaseHTTPMiddleware, dispatch=_auth_basica)
app.add_middleware(
    CORSMiddleware,
    # Vazio = nenhuma origem cross-site liberada (painel é same-origin). Preencha
    # CORS_ORIGINS só se um front em outro domínio precisar consumir a API.
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=bool(settings.CORS_ORIGINS),
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)
app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware)
app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers_middleware)


# ── Painel estático (mini front React/Vite) ───────────────────────────────────
# Servido em /painel quando o build existir (frontend/dist). Mesma origem da API,
# então o painel consome /cortes/atuais sem precisar de CORS nem configurar URL.
from pathlib import Path as _Path

from fastapi.staticfiles import StaticFiles

_PAINEL_DIST = _Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _PAINEL_DIST.is_dir():
    app.mount("/painel", StaticFiles(directory=str(_PAINEL_DIST), html=True), name="painel")
    logger.info("Painel estático montado em /painel (%s)", _PAINEL_DIST)
else:
    logger.info("frontend/dist não encontrado — /painel desabilitado (rode 'npm run build').")


# ── Módulo de remessas (auth + colaboração) ───────────────────────────────────
from app.api.routers import auth as _auth_router
from app.api.routers import remessas as _remessas_router

app.include_router(_auth_router.router)
app.include_router(_remessas_router.router)


@app.get("/health")
def health() -> dict:
    # Endpoint público (isento de auth, pingado por uptime). Por padrão devolve só
    # o status — os caminhos absolutos/cwd só saem com HEALTH_VERBOSE (debug local),
    # para não vazar layout do filesystem numa API exposta.
    if not settings.HEALTH_VERBOSE:
        return {"status": "ok"}
    import os
    from pathlib import Path
    storage = Path(settings.STORAGE_PATH)
    return {
        "status": "ok",
        "storage_path_config": settings.STORAGE_PATH,
        "storage_path_absolute": str(storage.resolve()),
        "storage_path_exists": storage.exists(),
        "cwd": os.getcwd(),
    }


@app.post("/notification/testar")
def testar_smtp() -> dict:
    """
    POST /notification/testar

    → 422  se SMTP_HOST não estiver configurado
    → 422  se NOTIFICACAO_DESTINATARIOS estiver vazio
    → 500  se a conexão SMTP falhar
    → 200  {"status": "ok", "destinatarios": ["..."]}
    """
    if not settings.SMTP_HOST:
        raise HTTPException(status_code=422, detail="SMTP_HOST não configurado.")
    if not settings.notification_DESTINATARIOS:
        raise HTTPException(status_code=422, detail="notification_DESTINATARIOS não configurado.")
    notificador = EmailSMTPNotificador(
        host=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        user=settings.SMTP_USER, 
        password=settings.SMTP_PASSWORD,
        use_tls=settings.SMTP_USE_TLS,
    )
    try:
        notificador.enviar(
            assunto="[Teste] Monitor Datas de Corte — verificação de SMTP",
            destinatarios=settings.notification_DESTINATARIOS,
            corpo_html="<p>Configuração SMTP funcionando corretamente.</p>",
        )
    except Exception:
        logger.exception("Falha no teste de envio SMTP")
        raise HTTPException(status_code=500, detail="Falha ao enviar e-mail de teste.")
    return {"status": "ok", "destinatarios": settings.notification_DESTINATARIOS}


def _resolver_key(key: str, config: dict) -> tuple[str, str | None]:
    """Resolve {key} como processadora ou convênio.

    Returns: (processadora_key, convenio_filter_or_None)
    """
    if key in config["processadoras"]:
        return key, None
    if key in config["convenios"]:
        return config["convenios"][key]["processadora"], key
    raise HTTPException(status_code=404, detail=f"Processadora ou convênio '{key}' não encontrado.")


def _executar_uma_processadora(processadora: str, convenio_filter: str | None = None) -> dict:
    execucao = build_orchestrator().executar(processadora, convenio_filter=convenio_filter)
    return {
        "id": execucao.id,
        "processadora": execucao.processadora,
        "status": execucao.status,
        "executada_em": execucao.executada_em,
        "total_convenios": execucao.total_convenios,
        "success_count": execucao.success_count,
        "error_count": execucao.error_count,
    }


@app.post("/coletas/{key}/executar")
def executar_coleta(key: str):
    """Aceita processadora (ex: consigi) ou convênio (ex: contagem) como {key}."""
    if key == "all":
        config = load_processadoras_config()
        processadoras_ativas = sorted({
            cfg["processadora"] for cfg in config["convenios"].values()
        })

        resultados: list[dict] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(_executar_uma_processadora, proc_key): proc_key
                for proc_key in processadoras_ativas
            }
            for future in as_completed(futures):
                proc_key = futures[future]
                try:
                    resultados.append(future.result())
                except Exception as e:
                    logger.exception("Falha ao executar coleta para %s", proc_key)
                    resultados.append({"processadora": proc_key, "status": "erro", "erro": str(e)})

        return sorted(resultados, key=lambda r: r["processadora"])

    config = load_processadoras_config()

    # Chave é uma processadora conhecida → comportamento original
    if key in config["processadoras"]:
        try:
            return _executar_uma_processadora(key)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception:
            logger.exception("Falha ao executar coleta para %s", key)
            raise HTTPException(status_code=500, detail="Falha interna ao executar coleta.")

    # Chave é um convênio → resolve a processadora e filtra
    if key in config["convenios"]:
        processadora_key = config["convenios"][key]["processadora"]
        try:
            return _executar_uma_processadora(processadora_key, convenio_filter=key)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception:
            logger.exception("Falha ao executar coleta para convênio %s", key)
            raise HTTPException(status_code=500, detail="Falha interna ao executar coleta.")

    raise HTTPException(status_code=404, detail=f"Processadora ou convênio '{key}' não encontrado.")


@app.get("/coletas/{key}/execucoes")
def listar_execucoes(key: str) -> list[dict]:
    config = load_processadoras_config()
    processadora_key, _ = _resolver_key(key, config)
    repo, _dados, _eventos = build_repositories()
    return [asdict(e) for e in repo.listar(processadora_key)]


# Montagem extraída para app/services/consulta_service.py (o sync das remessas usa a MESMA).
from app.services.consulta_service import montar_dados_convenios as _montar_dados_convenios


@app.get("/convenios")
def listar_convenios(
    processadora: Optional[str] = Query(None, description="Filtrar por processadora"),
    sem_dados: bool = Query(False, description="Retornar apenas convênios sem dados coletados"),
) -> list[dict]:
    resultado = _montar_dados_convenios()

    if processadora:
        resultado = [r for r in resultado if r["processadora"] == processadora]

    if sem_dados:
        resultado = [r for r in resultado if r["data_corte"] is None]

    return resultado


@app.get("/cortes/atuais")
def cortes_atuais() -> list[dict]:
    """Dados de corte mais recentes de todos os convênios, ordenados por nome."""
    resultado = _montar_dados_convenios()
    return sorted(resultado, key=lambda r: (r["convenio_nome"] or "").lower())


@app.get("/metricas")
def obter_metricas() -> dict:
    """Taxa de sucesso por processadora (atual/média/tendência) + convênios com falhas."""
    config = load_processadoras_config()
    processadoras = sorted({c["processadora"] for c in config["convenios"].values()})
    execucao_repo, _dados, evento_repo = build_repositories()

    por_processadora = []
    falhas: list[dict] = []
    for proc in processadoras:
        por_processadora.append({"processadora": proc, **metricas.resumo_processadora(execucao_repo.listar(proc))})
        for f in metricas.falhas_por_convenio(evento_repo.listar(proc, dias=metricas.JANELA_DIAS)):
            falhas.append({**f, "processadora": proc})
    falhas.sort(key=lambda x: x["falhas"], reverse=True)
    return {"processadoras": por_processadora, "convenios_com_falha": falhas}


@app.post("/convenios/{key}/data_corte")
def atualizar_data_corte(key: str, body: dict) -> dict:
    """Registra manualmente a data de corte de um convênio sem scraper.

    Body: {"data_corte": "01/07/2026"}
    """
    config = load_processadoras_config()
    if key not in config["convenios"]:
        raise HTTPException(status_code=404, detail=f"Convênio '{key}' não encontrado.")

    data_corte = (body or {}).get("data_corte")
    if not data_corte:
        raise HTTPException(status_code=422, detail="Campo 'data_corte' obrigatório.")

    convenio_cfg = config["convenios"][key]
    processadora_key = convenio_cfg["processadora"]
    nome = convenio_cfg.get("nome", key)
    agora = datetime.now(timezone.utc).isoformat()

    execucao_id = str(uuid.uuid4())
    execucao = Execucao(
        id=execucao_id,
        processadora=processadora_key,
        executada_em=agora,
        status="ok",
        total_convenios=1,
        success_count=1,
        error_count=0,
    )
    dado = DadoCorte(
        id=str(uuid.uuid4()),
        execucao_id=execucao_id,
        convenio_key=key,
        coletado_em=agora,
        convenio_nome=nome,
        data_corte=data_corte,
        origem="manual",
    )

    execucao_repo, dados_repo, _eventos = build_repositories()
    execucao_repo.salvar(execucao)
    dados_repo.salvar_lote([dado])

    logger.info("[manual] %s → data_corte=%r salvo", key, data_corte)
    return {"status": "ok", "convenio_key": key, "data_corte": data_corte}


@app.get("/coletas/{key}/eventos")
def listar_eventos(
    key: str,
    dias: int = Query(30, ge=1, le=365, description="Janela de dias para buscar eventos"),
) -> list[dict]:
    config = load_processadoras_config()
    processadora_key, _ = _resolver_key(key, config)
    _exec, _dados, repo = build_repositories()
    eventos = repo.listar(processadora_key, dias=dias)
    return [asdict(e) for e in eventos]


@app.get("/convenios/{key}/historico")
def historico_convenio(
    key: str,
    dias: int = Query(365, ge=1, le=3650, description="Janela de dias do histórico"),
) -> list[dict]:
    """Linha do tempo de data_corte de um convênio: as mudanças (DATA_CORTE_ALTERADA)
    e o primeiro registro (REGISTRO_NOVO), mais recentes primeiro.
    """
    config = load_processadoras_config()
    processadora_key, convenio_filter = _resolver_key(key, config)
    _exec, _dados, repo = build_repositories()
    eventos = repo.listar(processadora_key, dias=dias, convenio_key=convenio_filter)
    tipos_data = {EventoTipo.DATA_CORTE_ALTERADA.value, EventoTipo.REGISTRO_NOVO.value}
    return [
        {
            "detectado_em": e.detectado_em,
            "tipo": e.tipo,
            "data_corte_anterior": e.data_corte_anterior,
            "data_corte_nova": e.data_corte_nova,
            "folha": e.folha,
            "mes_atual": e.mes_atual,
        }
        for e in eventos
        if e.tipo in tipos_data
    ]


@app.get("/coletas/{key}/dados")
def obter_dados_atuais(key: str) -> list[dict]:
    config = load_processadoras_config()
    processadora_key, convenio_filter = _resolver_key(key, config)
    execucao_repo, dados_repo, _eventos = build_repositories()
    ultima = execucao_repo.buscar_ultima_ok(processadora_key)
    if not ultima:
        return []
    resultado = []
    for d in dados_repo.buscar_por_execucao(ultima.id):
        if convenio_filter and d.convenio_key != convenio_filter:
            continue
        d_dict = asdict(d)
        d_dict["data_corte"] = normalizar_data_corte(d.data_corte, d.mes_atual, d.coletado_em)
        resultado.append(d_dict)
    return resultado
