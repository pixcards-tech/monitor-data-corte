# Operação V1 — Monitor de Datas de Corte

Versão: V1 (baseline de produção assistida)
Data de congelamento: 2026-06-03

---

## O que o sistema faz

Coleta automaticamente as datas de corte de consignado de 11 processadoras (28 convênios), compara com a coleta anterior e envia alertas por e-mail quando alguma data muda. Tudo fica registrado em arquivos JSON para auditoria.

---

## Como rodar manualmente

### Pré-requisitos

- Python instalado com o ambiente virtual ativado
- Arquivo `.env` preenchido na raiz do projeto (ver seção de variáveis)
- Chrome instalado (usado pelos scrapers em modo headless)

### Ativar o ambiente virtual

```bat
cd C:\...\monitor-data-corte
.\env\Scripts\activate
```

### Rodar a coleta completa

```bat
python scripts/run_daily_collection.py
```

O script executa todas as 11 processadoras em sequência e exibe um resumo no terminal ao final.

### Rodar sem intervalos e sem retry (modo teste rápido)

No PowerShell:

```powershell
$env:DAILY_COLLECTION_INTERVAL_MINUTES = "0"
$env:DAILY_COLLECTION_MAX_RETRIES = "0"
python scripts/run_daily_collection.py
```

### Validar convênios sem salvar dados (diagnóstico)

```bat
# Todos os 28 convênios (demora ~15 min com intervalo de 30s)
python scripts/validate_all_collectors.py --all --intervalo 30

# Só uma processadora
python scripts/validate_all_collectors.py --processadora consigfacil

# Só um convênio específico
python scripts/validate_all_collectors.py --convenio saojoaodospatos

# Ver o que está mapeado sem executar nada
python scripts/validate_all_collectors.py --dry-run
```

> **Atenção:** `validate_all_collectors.py` **não salva dados e não envia e-mail**. É seguro rodar para diagnóstico a qualquer momento.

---

## Como agendar no Windows Task Scheduler

1. Abrir o **Agendador de Tarefas** (`taskschd.msc`)
2. Clicar em **Criar Tarefa Básica**
3. Configurar:
   - **Nome:** `Monitor Datas de Corte - Coleta Diária`
   - **Gatilho:** Diariamente, no horário desejado (ex: 08:00)
   - **Ação:** Iniciar um programa
     - **Programa/script:** caminho completo para o Python do ambiente virtual
       Ex: `C:\...\monitor-data-corte\env\Scripts\python.exe`
     - **Argumentos:** `scripts/run_daily_collection.py`
     - **Iniciar em:** `C:\...\monitor-data-corte`
4. Em **Propriedades > Configurações**:
   - Marcar "Executar mesmo que o usuário não esteja conectado"
   - Em "Se a tarefa já estiver em execução": selecionar "Não iniciar nova instância"
5. Salvar com as credenciais do usuário Windows

> Para verificar se rodou: abrir o Agendador de Tarefas > aba **Histórico** da tarefa, ou verificar o arquivo `data/runs/{data}.json`.

---

## O que verificar após a execução

### 1. Resumo do dia — `data/runs/{YYYY-MM-DD}.json`

Gerado automaticamente a cada execução. Mostra o resultado geral:

```json
{
  "data": "2026-06-03",
  "duracao_minutos": 45.2,
  "total_processadoras": 11,
  "sucesso": 10,
  "falha_persistente": 1,
  "retries_executados": 2,
  "processadoras": [
    { "processadora": "consigfacil", "status": "ok",      "tentativas": 1, "erro": null },
    { "processadora": "consignet",   "status": "erro",     "tentativas": 3, "erro": "Timeout..." }
  ]
}
```

### 2. Execuções salvas — `data/processadoras/{processadora}/execucoes/`

Um arquivo `.json` por execução de cada processadora. Contém total de convênios, sucessos, falhas e erros por convênio.

### 3. Dados coletados — `data/dados_corte/`

Um arquivo `.json` por execução com os dados de corte normalizados de todos os convênios da processadora.

### 4. Eventos detectados — `data/processadoras/{processadora}/eventos/{YYYY-MM-DD}.jsonl`

Criado quando há mudanças. Cada linha é um evento:


- `registro_novo` — primeiro registro de um convênio
- `data_corte_alterada` — data de corte mudou em relação à coleta anterior
- `registro_nao_encontrado` — convênio desapareceu da coleta

Se esse arquivo existir e tiver eventos do tipo `data_corte_alterada`, um e-mail de alerta foi (ou deveria ter sido) disparado.

---

## Como interpretar os status

### Status das processadoras no `runs/{data}.json`

| Status | Significado | O que fazer |
|---|---|---|
| `ok` | Todos os convênios da processadora coletados com sucesso | Nada |
| `partial_success` | Parte dos convênios coletou, parte falhou | Verificar quais convênios falharam no arquivo de execução; retry automático já aconteceu |
| `erro` | Nenhum convênio da processadora coletou | Verificar o campo `erro`; checar se o portal está acessível manualmente |

### Status geral do runner

| Campo | Significado |
|---|---|
| `sucesso: 11` | Todas as processadoras retornaram `ok` ou `partial_success` |
| `falha_persistente: 0` | Nenhuma processadora ficou com `status: erro` após todos os retries |
| `falha_persistente: 1+` | Ao menos uma processadora falhou completamente — verificar o campo `erro` e investigar |

### Sobre SafeConsig especificamente

O convênio `saojoaodospatos` usa integração via API REST (não scraper). O dado retornado é uma **estimativa de virada de competência**, não uma data de corte oficial confirmada. O e-mail de alerta para esse convênio incluirá uma nota explicando isso.

---

## Falhas conhecidas e ações recomendadas

| Convênio | Processadora | Tipo de falha | Ação recomendada |
|---|---|---|---|
| `belterra` | consigfacil | Certificado digital rejeitado pelo portal — falha permanente | Não há solução por código. Verificar com a ConsigFácil se o certificado do convênio está válido |
| Todos (6) | consignet | **reCAPTCHA v3 bloqueando o login automatizado** (ver diagnóstico abaixo) | Falha externa. Não resolve por ajuste de seletor/intervalo. Decidir estratégia: pedir API/allowlist à Consignet, sessão persistente, ou stealth |
| `muana` | consigup | Portal fora do ar ou timeout de rede ocasional | Esperar e verificar se o portal `sistema.consigup.com.br` está acessível. Retry resolve |
| Qualquer | qualquer | `ERR_CONNECTION_TIMED_OUT` | Portal temporariamente indisponível. Verificar conectividade de rede e acessar o portal manualmente |
| Qualquer | qualquer | `Credenciais inválidas` | A senha do convênio mudou. Atualizar no `.env` e rodar `validate_all_collectors.py --convenio <key>` para confirmar |

---

## Diagnóstico: ConsigNet bloqueado por reCAPTCHA v3 (2026-06-09)

**Status:** falha externa conhecida, não resolvida. Afeta os 6 convênios consignet
(defensoria, maringa, maringa_prev, navegantes, rancharia, vilhena).

**Sintoma:** após submeter usuário + senha, a página permanece em `/auth/login`,
sem nenhuma mensagem de erro. O log mostra `[TwoStepAuth] Autenticação concluída`
(enganoso — o `wait_for_load_state("networkidle")` retorna mesmo sem navegação) e,
em seguida, `[ConsigNet] Autenticação falhou — ainda em /auth/login`.

**Causa raiz:** o portal `www1.consignet.com.br` usa **reCAPTCHA v3 (invisível)**.
O login programático via Playwright recebe um score baixo (comportamento de bot) e
é **rejeitado silenciosamente** pelo backend — sem desafio visível nem mensagem.

**Como foi confirmado** (`scripts/diag_consignet.py`):
- HTML da tela contém `recaptcha/api.js?render=<sitekey>`, `grecaptcha.execute` e
  o `badge` → assinatura do reCAPTCHA v3.
- Texto visível: *"This site is protected by reCAPTCHA..."*.
- Campos de login **corretamente preenchidos** (usuário e senha de 15 chars) →
  descarta erro de credencial ou de seletor.
- Falha **mesmo com um único login isolado** → descarta a antiga hipótese de
  rate-limit por quantidade de logins.
- Evidência salva em `data/diagnostico/consignet_<convenio>_<timestamp>.{png,html}`.

**Por que "funcionava antes":** o score do reCAPTCHA v3 é não-determinístico. A
reputação do IP/comportamento degradou ou o portal endureceu o limiar de aceitação.

**Reproduzir o diagnóstico:**
```powershell
python scripts/diag_consignet.py --convenio defensoria
```

**Estratégias possíveis (a decidir):**
1. **API / allowlist** — solicitar à Consignet/DB1 acesso via API oficial ou
   whitelist do IP (mesmo modelo da SafeConsig). Mais sustentável.
2. **Sessão persistente** — usar `user_data_dir` (já suportado pelo `base_scraper`):
   login manual 1x resolvendo o captcha e reuso dos cookies de sessão.
3. **Stealth/humanização** — `playwright-stealth` + browser não-headless. Frágil.

---

## Variáveis de ambiente obrigatórias

Todas ficam no arquivo `.env` na raiz do projeto. **Nunca commitar o `.env` no repositório.**

### Runner diário

| Variável | Obrigatório | Padrão | Descrição |
|---|---|---|---|
| `DAILY_COLLECTION_INTERVAL_MINUTES` | Não | `1` | Pausa em minutos entre cada processadora na rodada principal |
| `DAILY_COLLECTION_MAX_RETRIES` | Não | `2` | Quantas vezes retentar cada processadora que falhou |
| `DAILY_COLLECTION_RETRY_DELAY_MINUTES` | Não | `60` | Pausa em minutos antes de cada retry |
| `DAILY_COLLECTION_PROCESSADORA_TIMEOUT_MINUTES` | Não | `60` | Teto por processadora: estourou → mata a árvore de processos do scraper (Chrome/driver) e a rodada segue para a próxima, registrando a falha (Linux/container; no Windows apenas loga) |

> **Retry não-bloqueante:** quando uma ou mais processadoras falham completamente,
> cada uma ganha sua própria thread de retry. As esperas de `RETRY_DELAY` correm
> **em paralelo** — 3 falhas aguardam 60min ao mesmo tempo, não 180min em fila — e a
> rodada principal não fica travada. A **execução** real, porém, é serializada por um
> lock: nunca há dois scrapers ou escritas no storage rodando simultaneamente
> (Playwright e o storage JSON não são seguros para concorrência). O processo só
> encerra após todos os ciclos de retry concluírem.

### Notificação por e-mail

| Variável | Obrigatório | Descrição |
|---|---|---|
| `SMTP_HOST` | Sim | Servidor SMTP (ex: `smtp.gmail.com`) |
| `SMTP_PORT` | Sim | Porta SMTP (ex: `587`) |
| `SMTP_USER` | Sim | Usuário/e-mail do remetente |
| `SMTP_PASSWORD` | Sim | Senha do remetente |
| `SMTP_USE_TLS` | Não (padrão: `true`) | Usar TLS na conexão SMTP |
| `NOTIFICACAO_DESTINATARIOS` | Sim | E-mails separados por vírgula que receberão os alertas |

### SafeConsig produção

| Variável | Obrigatório | Descrição |
|---|---|---|
| `SAFECONSIG_PROD_SAOJOAODOSPATOS_BASE_URL` | Sim | URL da API SafeConsig para São João dos Patos |
| `SAFECONSIG_PROD_SAOJOAODOSPATOS_ID_CONVENIO` | Sim | ID do convênio (252) |
| `SAFECONSIG_PROD_SAOJOAODOSPATOS_USERNAME` | Sim | Usuário da API |
| `SAFECONSIG_PROD_SAOJOAODOSPATOS_PASSWORD` | Sim | Senha da API |

### Storage e browser

| Variável | Obrigatório | Padrão | Descrição |
|---|---|---|---|
| `STORAGE_PATH` | Não | `data` | Diretório onde os dados são salvos |
| `HEADLESS` | Não | `false` | `true` para rodar sem abrir janela do browser |
| `TIMEOUT_MS` | Não | `180000` | Timeout dos scrapers em milissegundos |

---

## Não mexer sem necessidade — arquivos críticos da V1

Estes arquivos formam a base da V1. Qualquer alteração pode quebrar o pipeline de coleta, comparação ou notificação. Só modificar com entendimento completo do impacto.

| Arquivo | Por que é crítico |
|---|---|
| `app/core/processadoras.json` | Mapa de todas as processadoras e convênios. Remover ou renomear uma chave quebra a coleta e o histórico de comparação |
| `app/services/orchestrator.py` | Orquestra todo o pipeline: coleta → storage → comparação → e-mail. Alterações afetam todas as processadoras |
| `app/services/coleta_service.py` | Roteia scrapers e API. O branch `integration_type == "api"` é o que faz SafeConsig funcionar |
| `app/services/comparador_service.py` | Detecta mudanças de data de corte. Alterações podem gerar falsos positivos ou suprimir alertas reais |
| `app/storage/file_storage.py` | Implementação do storage em JSON. Alterar pode corromper ou tornar inacessível o histórico existente |
| `app/core/models.py` | Modelos de dados (`Execucao`, `DadoCorte`, `Evento`). Qualquer campo adicionado/removido afeta storage e comparador |
| `data/` | Diretório de dados históricos. Não deletar arquivos manualmente. Não mover sem atualizar `STORAGE_PATH` |
| `.env` | Credenciais de produção. Não commitar, não compartilhar, não logar |

---

## Estrutura de diretórios de dados

```
data/
├── runs/
│   └── 2026-06-03.json          # resumo de cada execução do runner diário
├── dados_corte/
│   └── {execucao_id}.json       # dados coletados por execução
└── processadoras/
    └── {processadora}/
        ├── execucoes/
        │   └── {uuid}.json      # resultado de cada execução por processadora
        └── eventos/
            └── 2026-06-03.jsonl # eventos detectados (mudanças de data de corte)
```
