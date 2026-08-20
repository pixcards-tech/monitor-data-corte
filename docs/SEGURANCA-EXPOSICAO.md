# Segurança — expor o Monitor num subdomínio

Guia para publicar a API/painel com segurança. Fecha os achados do audit `/cso`
(relatório em `.gstack/security-reports/`). **Não exponha sem seguir este checklist.**

## O que o código já força (defesa em profundidade)

Aplicado no repositório, ativo por configuração:

- **Fail-closed de auth** — com `AUTH_REQUIRED=True`, a API **recusa subir** se
  `PANEL_PASSWORD` estiver vazia (nada de subir aberto por esquecimento). Guard em
  `app/api/main.py:_validar_config_seguranca`.
- **Rate limit por IP** — dois baldes (leitura folgado, auth+mutações apertado),
  em memória, bounded, thread-safe. `429 + Retry-After` ao estourar. `/health` é isento.
  `app/api/security.py`.
- **Cabeçalhos de segurança** — `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Cross-Origin-Opener-Policy`, `Content-Security-Policy` e
  `Strict-Transport-Security` (quando `COOKIE_SECURE`).
- **CORS travado** — sem wildcard. Nenhuma origem cross-site por padrão (o painel é
  same-origin). Libere domínios pontuais em `CORS_ORIGINS`.
- **Postgres nunca exposto** — no `docker-compose.yml` o banco e a API sobem com
  bind `127.0.0.1` (só o host alcança). Publicação na internet é só via proxy reverso.
- **Senha do Postgres obrigatória** — o compose não tem mais default `monitor`;
  falha se `POSTGRES_PASSWORD` não estiver setada.
- **`/health` enxuto** — não vaza `cwd`/caminhos absolutos (a menos de `HEALTH_VERBOSE`).

## Checklist antes de expor

1. **`.env` da VM** (perfil de produção):
   ```env
   AUTH_REQUIRED=True
   PANEL_USER=admin
   PANEL_PASSWORD=<gerar: python -c "import secrets;print(secrets.token_urlsafe(24))">
   COOKIE_SECURE=True
   TRUST_PROXY=True
   POSTGRES_PASSWORD=<senha forte>
   RATE_LIMIT_ENABLED=True
   ```
2. **Proxy reverso com TLS** na frente (nginx/Caddy). A API escuta só em
   `127.0.0.1:8000`; o proxy termina o HTTPS e repassa.
3. **Firewall**: abra só 80/443 (e 22/SSH). **Nunca** 8000 nem 5432.
   Lembre: o Docker fura o UFW — por isso o bind já é `127.0.0.1`, não confie só no firewall.
4. **Rotacionar a credencial do ConsigUp/muaná** (exposta no histórico antes da limpeza
   BFG) e confirmar que o repo no GitHub é **privado**.
5. Subir e validar: `curl -I https://SEU_SUBDOMINIO/health` deve trazer os headers de
   segurança; sem credencial, os endpoints de dados devem responder `401`.

## Exemplo — Caddy (TLS automático)

```caddy
monitor.suaempresa.com.br {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
```

Com `TRUST_PROXY=True`, o rate limit passa a usar o IP real do `X-Forwarded-For`
(o Caddy/nginx injeta). Sem proxy confiável na frente, deixe `TRUST_PROXY=False`.

## Exemplo — nginx

```nginx
server {
    listen 443 ssl http2;
    server_name monitor.suaempresa.com.br;
    ssl_certificate     /etc/letsencrypt/live/monitor.suaempresa.com.br/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/monitor.suaempresa.com.br/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Variáveis de segurança (referência)

| Variável | Default | Produção exposta | Papel |
|---|---|---|---|
| `AUTH_REQUIRED` | `False` | **`True`** | Fail-closed: sem `PANEL_PASSWORD` não sobe |
| `PANEL_PASSWORD` | vazio | **forte** | Liga o HTTP Basic em toda a API |
| `COOKIE_SECURE` | `False` | **`True`** | Cookie de sessão só sob HTTPS + HSTS |
| `TRUST_PROXY` | `False` | **`True`** | Usa X-Forwarded-For (IP real) no rate limit |
| `POSTGRES_PASSWORD` | — (obrigatório) | **forte** | Sem default fraco |
| `CORS_ORIGINS` | vazio | domínios específicos | Nenhuma origem cross-site por padrão |
| `RATE_LIMIT_ENABLED` | `True` | `True` | Rate limit por IP |
| `RATE_LIMIT_GERAL` / `RATE_LIMIT_SENSIVEL` | `120` / `20` | ajuste | Limites por janela (60s) |
| `SECURITY_HEADERS` | `True` | `True` | Cabeçalhos de segurança |
| `CONTENT_SECURITY_POLICY` | (default) | ajuste | Vazie se o painel quebrar |
| `HEALTH_VERBOSE` | `False` | `False` | `/health` não vaza paths |
