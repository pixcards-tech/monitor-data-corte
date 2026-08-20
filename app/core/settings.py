import os

from dotenv import load_dotenv

load_dotenv(override=True)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


class Settings:
    HEADLESS: bool = _bool(os.getenv("HEADLESS"), False)
    TIMEOUT_MS: int = int(os.getenv("TIMEOUT_MS", "180000"))
    CHROME_CHANNEL: str = os.getenv("CHROME_CHANNEL", "chrome")
    STORAGE_PATH: str = os.getenv("STORAGE_PATH", "data")

    # Backend de persistência: "file" (JSON/JSONL em STORAGE_PATH) ou "postgres".
    STORAGE_BACKEND: str = os.getenv("STORAGE_BACKEND", "file")
    # Conexão SQLAlchemy quando STORAGE_BACKEND=postgres.
    # Ex: postgresql+psycopg://user:senha@db:5432/monitor
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS: bool = _bool(os.getenv("SMTP_USE_TLS"), True)
    notification_DESTINATARIOS: list[str] = [
        e.strip()
        for e in os.getenv("NOTIFICACAO_DESTINATARIOS", "").split(",")
        if e.strip()
    ]

    # Webhooks de mudança de data de corte (URLs separadas por vírgula). Vazio = desabilitado.
    WEBHOOK_URLS: list[str] = [
        u.strip()
        for u in os.getenv("WEBHOOK_URLS", "").split(",")
        if u.strip()
    ]

    # Agendamento — formato "HH:MM". Vazio = desabilitado.
    COLETA_HORARIO: str = os.getenv("COLETA_HORARIO", "")

    # Auth básica do painel/API (HTTP Basic). PANEL_PASSWORD vazio = auth DESABILITADA
    # (aberto, comportamento atual); setar a senha no .env da VM liga a proteção.
    PANEL_USER: str = os.getenv("PANEL_USER", "admin")
    PANEL_PASSWORD: str = os.getenv("PANEL_PASSWORD", "")

    # Dead-man's switch: URL de um serviço de uptime (ex.: healthchecks.io) pingada ao fim
    # de cada coleta. Se a coleta não rodar, o ping falta e o serviço alerta. Vazio = off.
    HEALTHCHECK_URL: str = os.getenv("HEALTHCHECK_URL", "")

    # Alerta operacional: webhook (ex.: Slack incoming webhook) que recebe alertas acionáveis
    # com severidade ao fim de cada coleta. Vazio = desabilitado.
    ALERT_WEBHOOK_URL: str = os.getenv("ALERT_WEBHOOK_URL", "")

    # ── Módulo de remessas (multiusuário) ─────────────────────────────────────
    # Sessões de login: validade em horas (default 7 dias).
    SESSION_TTL_HORAS: int = int(os.getenv("SESSION_TTL_HORAS", "168"))
    # Cookie Secure (exige HTTPS) — ligar em produção atrás de TLS.
    COOKIE_SECURE: bool = _bool(os.getenv("COOKIE_SECURE"), False)

    # ── Segurança / exposição pública ──────────────────────────────────────────
    # Fail-closed: se True e PANEL_PASSWORD estiver vazia, a API RECUSA subir (em vez
    # de subir aberta). Ligue em qualquer deploy exposto (subdomínio/VM pública).
    AUTH_REQUIRED: bool = _bool(os.getenv("AUTH_REQUIRED"), False)

    # App atrás de proxy reverso (nginx/caddy) que injeta X-Forwarded-For. Só confie
    # no XFF quando isso for verdade — senão o IP do rate-limit é forjável pelo cliente.
    TRUST_PROXY: bool = _bool(os.getenv("TRUST_PROXY"), False)

    # CORS — origens permitidas (separadas por vírgula). Vazio = nenhuma origem
    # cross-site (o painel é same-origin, servido pela própria API em /painel).
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]

    # Rate limit por IP (janela fixa). Dois baldes: leitura (folgado) e sensível
    # (auth + mutações). Vazio de janela/limite usa os defaults abaixo.
    RATE_LIMIT_ENABLED: bool = _bool(os.getenv("RATE_LIMIT_ENABLED"), True)
    RATE_LIMIT_WINDOW_S: int = int(os.getenv("RATE_LIMIT_WINDOW_S", "60"))
    RATE_LIMIT_GERAL: int = int(os.getenv("RATE_LIMIT_GERAL", "120"))
    RATE_LIMIT_SENSIVEL: int = int(os.getenv("RATE_LIMIT_SENSIVEL", "20"))

    # Cabeçalhos de segurança (nosniff, frame-deny, referrer, HSTS, CSP).
    SECURITY_HEADERS: bool = _bool(os.getenv("SECURITY_HEADERS"), True)
    # CSP do painel; vazio = não envia o header (relaxe se o painel quebrar).
    CONTENT_SECURITY_POLICY: str = os.getenv(
        "CONTENT_SECURITY_POLICY",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; script-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
    )

    # /health detalhado vaza cwd e caminhos absolutos — só em debug local.
    HEALTH_VERBOSE: bool = _bool(os.getenv("HEALTH_VERBOSE"), False)

    @property
    def REMESSAS_ENABLED(self) -> bool:
        """Remessas é Postgres-only (CRUD multiusuário + auditoria transacional)."""
        return self.STORAGE_BACKEND.strip().lower() == "postgres"


settings = Settings()