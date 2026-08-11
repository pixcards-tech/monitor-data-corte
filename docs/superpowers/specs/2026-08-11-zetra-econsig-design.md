# Zetra/eConsig — Integração via API SOAP (Centralizador)

Data: 2026-08-11
Status: proposta — aguardando aprovação para implementar
Origem: prompt externo (melhorador) adaptado à arquitetura existente

## Por que esta spec existe

Um prompt gerado externamente propôs um sistema standalone (SQLite próprio,
`.env` próprio, árvore `src/` própria, detecção de mudança própria). Isso
duplicaria o que o monitor já faz: storage Postgres, ComparadorService,
e-mail digest, runner diário com retry + watchdog, dead-man's switch, painel.

Esta spec preserva a inteligência de API do prompt original (envelope, códigos
de retorno, convênios) e reencaixa a implementação no pipeline existente,
seguindo o precedente da SafeConsig (`integration_type: "api"`).

## O que já existe e será reaproveitado (não construir de novo)

| Prompt original propunha | Já existe no monitor |
|---|---|
| SQLite `data/data_corte.db` + repositorio.py | Storage Postgres via orchestrator (`execucoes`, `dados_corte`, `eventos`) |
| Detecção de mudança + log WARNING | `ComparadorService` → evento `data_corte_alterada` → e-mail digest |
| Resumo final no stdout | Resumo diário do runner + e-mail agregado |
| Agendamento diário | `run_scheduler_service.py` (09:00, watchdog, healthcheck) |
| Retry com backoff | Retry por processadora no runner + retry fino no client (ver abaixo) |
| `config/convenios.yaml` | `app/core/processadoras.json` (seções `processadoras` e `convenios`) |
| `.env` novo | `.env` existente — só adicionar as chaves `ZETRA_*` |
| `python -m src` | 12ª processadora coletada automaticamente na rodada diária |

## Inteligência de API (preservada do prompt original)

- Endpoint: `https://api.econsig.com.br/central/services/HostaHostService`
- Operação: `consultarParametros` / SOAPAction `urn:consultarParametros`
- SOAP 1.1, `Content-Type: text/xml; charset=UTF-8`
- Namespace do envelope: `xmlns:tns="HostaHostService"` — **string literal, não
  URI. Não normalizar.** Montar o envelope como string; **não** usar zeep/proxy
  gerado (o namespace inválido quebra a geração).
- Ordem dos campos é obrigatória (WSDL): `cliente`, `convenio`, `usuario`,
  `senha`, `codVerba`, `servicoCodigo`, `orgaoCodigo`, `estabelecimentoCodigo`.
  Os 4 primeiros obrigatórios; opcionais só entram quando presentes.
- Resposta, dentro de `parametroSet`: `diaCorte` (int, dia do mês),
  `periodoAtual` (formato `2026-09-01-03:00` — normalizar descartando offset),
  `svcDescricao`. Fora dele: `sucesso`, `codRetorno`, `mensagem`.
- Script exploratório validado: `scripts/test_consulta_zetra.py` (referência do
  envelope que funciona; commitar como referência, a implementação o substitui).

### Códigos de retorno

| Grupo | Códigos | Tratamento |
|---|---|---|
| Retry (até 3x, backoff exponencial, no client) | `355`, `418`, `903`, `904`, `905` | Indisponibilidade temporária |
| Sem retry, só registro | `201`, `242`, `243`, `299` | Convênio não encontrado / parâmetro faltando ou ambíguo / limite excedido |
| **Aborta o lote inteiro** | `001` (credencial), `362` (IP não autorizado) | Insistir nos demais só gasta chamada e arrisca bloqueio. Erro tipado `credencial` → detecção proativa existente alerta no digest |

## Implementação

### 1. `app/integrations/processors/zetra/client.py`
- Monta envelope como string (ordem fixa, opcionais condicionais).
- POST via `requests` com `ZETRA_TIMEOUT`; parse com `xml.etree.ElementTree`.
- Retry interno para os códigos retryáveis; backoff exponencial.
- Exceções tipadas: `ZetraCredencialError` (001), `ZetraIpBloqueadoError` (362).
- `senha` jamais aparece em log, mesmo DEBUG (padrão SafeConsig).

### 2. `app/integrations/processors/zetra/collector.py`
- `ZetraApiCollector.run(convenio_key, convenio_config)` — mesmo contrato de
  retorno do `SafeConsigApiCollector.run` (dict com `convenio_key`,
  `convenio_nome`, `status`, `records_count`, `erro`, `dados`).
- Mapeamento: `data_corte` = dia `diaCorte` na competência de `periodoAtual`;
  `mes_atual` = `periodoAtual` como `MM/YYYY`; `folha` = `svcDescricao`.
- Pausa `ZETRA_PAUSA_ENTRE_CHAMADAS` (default 1.5s) entre convênios do lote.
- `001`/`362`: marca os convênios restantes como falha sem chamar a API.

### 3. `app/services/coleta_service.py` — dispatch genérico
`_run_api_collector` hoje é hardcoded SafeConsig. Passa a despachar pelo campo
`api_collector` da processadora (`"safeconsig"` default para retrocompat,
`"zetra"` novo). É o item "ApiCollector genérico" do roadmap.

### 4. `app/core/processadoras.json`
```json
"zetra": {
  "integration_type": "api",
  "api_collector": "zetra",
  "uses_chrome_channel": false
}
```
Convênios ativos (8): `PIX_CARD-EMBUDASARTES` (Embu das Artes-SP),
`PIX_CARD-LINHARES` (Linhares-ES), `PIX_CARD-ES` (Gov. Espírito Santo),
`PIX_CARD-ARACRUZ` (Aracruz-ES), `PIX_CARD-MAUA` (Mauá-SP),
`PIX_CARD-SALTO` (Salto-SP), `PIX_CARD-SERRA` (Serra-ES),
`PIX_CARD-NOVALIMA` (Nova Lima-MG, `servico_codigo: "044"`).

Cada entrada em `convenios`: `nome`, `processadora: "zetra"`,
`zetra_convenio` (código PIX_CARD-*), `servico_codigo` opcional.

Pendentes (retornam 243 "mais de um serviço encontrado" — aguardando
`servico_codigo`): Belo Horizonte-MG, Curitiba-PR, POA-SP, IPREMU-Uberlândia,
Uberlândia-MG. Ficam FORA do json até termos o código; listados aqui para
rastreio.

### 5. `.env` / `.env.example`
```
ZETRA_ENDPOINT=https://api.econsig.com.br/central/services/HostaHostService
ZETRA_CLIENTE=PIX_CARD
ZETRA_USUARIO=pix_card_xml
ZETRA_SENHA=
ZETRA_TIMEOUT=30
ZETRA_PAUSA_ENTRE_CHAMADAS=1.5
```
Credencial única para todos os convênios (nível processadora, não por
convênio). Falha clara na inicialização do collector se `ZETRA_SENHA` vazia.

### 6. Decisão em aberto — `origem` do dado
`coleta_service` marca `integration_type == "api"` como `origem:
"api_estimativa"` (correto para SafeConsig, que estima). O `diaCorte` da Zetra
é **parâmetro oficial do Centralizador**, não estimativa. Proposta: campo
`api_origem` na processadora (`"api_oficial"` para zetra; default
`"api_estimativa"`), refletido na flag de confiança do painel.

### 7. Testes (`tests/integrations/zetra/`)
- Envelope: ordem dos campos, opcionais condicionais, namespace literal.
- Parse: resposta ok, `parametroSet` ausente, `periodoAtual` normalizado.
- Códigos: retry com backoff (mock), 001/362 abortam o lote, 201/242/243/299
  registram sem retry.
- Collector: contrato do dict, mapeamento diaCorte+periodoAtual → data_corte.
- Nenhum teste chama a API real (HTTP mockado).

### 8. Deploy
Fluxo normal: commit → push → VM `git pull` → `docker compose build` →
`up -d`. A Zetra entra como 12ª processadora na rodada das 09:00 seguinte.
Atenção: whitelist de IP da Zetra deve incluir o IP da VM (216.238.125.252) —
código 362 na primeira rodada indica que o IP liberado foi só o da máquina
local.

## Fora de escopo desta fase
- Convênios pendentes de `servico_codigo` (entram quando o código chegar).
- `codVerba` / `orgaoCodigo` / `estabelecimentoCodigo` (suportados no
  envelope, sem uso atual).
