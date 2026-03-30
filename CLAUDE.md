# BoateBus Central de Inteligência — Dashboard 2.0

## Visão Geral

Dashboard multi-page HTML para a BoateBus (empresa de festas em ônibus/bus party em Belo Horizonte), com 5 abas: Comercial, Chatwoot Leads, ConversaScore, Meta Ads, Orgânico Social. Servido via nginx no Docker Swarm com Traefik como reverse proxy.

O projeto envolve:
1. Dashboard HTML estático (`src/index.html`) alimentado por arquivos JSON
2. 4 scripts Python que coletam dados e geram os JSONs (rodam via crontab)
3. 1 servidor webhook FastAPI (Docker) que captura mensagens em tempo real
4. Tabelas PostgreSQL para armazenar conversas e scores
5. Integração com APIs: Chatwoot, BoateBus, OpenAI Whisper, Claude API

## Infraestrutura

### Servidor
- **IP**: YOUR_SERVER_IP
- **OS**: Ubuntu (Docker Swarm)
- **Specs**: 8 vCPU, 16 GB RAM, 160 GB Disk
- **SSH**: `ssh root@YOUR_SERVER_IP`

### Rede Docker
- **Rede overlay**: `net` (todas as stacks usam essa rede)
- **NÃO** é `traefik-public`. Usar `net` em todos os docker-compose.

### Serviços existentes (não mexer)
| Stack | Descrição |
|-------|-----------|
| dashboard | Dashboard antigo (nginx em /home/your-legacy-dashboard) - MANTER FUNCIONANDO |
| evolution | Evolution API |
| media | Serviço de mídia |
| n8n | n8n (4 serviços) — não mais usado para este projeto |
| portainer | Portainer |
| postgres | PostgreSQL |
| traefik | Traefik reverse proxy |

### URLs
- **Dashboard**: `dashboard.yourdomain.com`
- **Webhook**: `webhook.yourdomain.com` ← DNS A record apontando para YOUR_SERVER_IP
- **n8n**: `https://n8n.yourdomain.com`
- **Chatwoot**: `https://app.chatwoot.com` (cloud, não self-hosted)

## Credenciais

Todas as credenciais sensíveis estão no arquivo `.env` em `/home/your-project/.env`. **NUNCA commitar o .env no git.**

Variáveis necessárias (ver `.env.example`):
- `BOATEBUS_TOKEN` — Bearer token da API BoateBus
- `CHATWOOT_TOKEN` — API token do Chatwoot
- `CHATWOOT_ACCOUNT_ID=YOUR_ACCOUNT_ID`
- `ANTHROPIC_API_KEY` — Claude API (usada no scoring diário)
- `OPENAI_API_KEY` — Whisper (transcrição de áudios no webhook)
- `PG_HOST`, `PG_PORT`, `PG_DATABASE`, `PG_USER`, `PG_PASSWORD`
- `ALERT_EMAIL_FROM=alerts@yourcompany.com`
- `ALERT_EMAIL_PASS` — Gmail App Password (sem espaços)
- `ALERT_EMAIL_TO=admin@yourcompany.com`

### Agentes com meta no Chatwoot
- ID `100001` → Agent One
- ID `100002` → Agent Two

### Claude API
- **Model**: `claude-sonnet-4-20250514`
- **Usar via HTTP direto** (requests), não SDK

### OpenAI (Whisper)
- **Uso**: Transcrição de áudios do WhatsApp no webhook
- **Model**: `whisper-1`, language: `pt`

## Estrutura de Arquivos no Servidor

```
/home/your-project/          ← Projeto central (este repo)
├── index.html                    ← Dashboard multi-page (servido pelo nginx)
├── data.json                     ← Gerado por collectors/comercial.py
├── chatwoot.json                 ← Gerado por collectors/chatwoot_leads.py
├── scores.json                   ← Gerado por collectors/scoring.py
├── collectors/
│   ├── comercial.py              ← Cron 8h/12h/17h
│   ├── chatwoot_leads.py         ← Cron 8h05/12h05/17h05
│   ├── webhook.py                ← FastAPI (Docker, sempre rodando)
│   └── scoring.py                ← Cron 6h diário
├── utils/
│   ├── alerts.py                 ← E-mail de erro automático
│   └── db.py                     ← Conexão PostgreSQL
├── requirements.txt
├── .env                          ← NUNCA commitar
└── docker-compose.yml

/home/your-legacy-dashboard/         ← Dashboard antigo (NÃO MEXER)
```

## Deploy

### Passo 1 — Clonar repo e criar .env
```bash
ssh root@YOUR_SERVER_IP
git clone https://github.com/yourusername/sales-analysys-chatwoot /home/your-project
cd /home/your-project
cp .env.example .env
nano .env  # preencher com valores reais
```

### Passo 2 — Executar deploy
```bash
bash scripts/deploy.sh
```

O script:
1. Instala dependências Python (`pip install -r requirements.txt`)
2. Aplica schema PostgreSQL
3. Faz build da imagem Docker do webhook
4. Faz `docker stack deploy` (nginx + webhook)
5. Instala crontab

### Passo 3 — DNS Cloudflare
Adicionar registro A: `webhook` → `YOUR_SERVER_IP` (proxied)

### Passo 4 — Configurar Chatwoot Webhook
Settings → Integrations → Webhooks → Add:
- **URL**: `https://webhook.yourdomain.com/conversascore-webhook`
- **Eventos**: `message_created`, `conversation_status_changed`

### Passo 5 — Testar
```bash
# Teste manual dos scripts
cd /home/your-project
python3 collectors/comercial.py
python3 collectors/chatwoot_leads.py
python3 collectors/scoring.py

# Teste do webhook
curl -s https://webhook.yourdomain.com/
# Deve retornar: {"status":"ok","service":"boatebus-webhook"}

# Verificar serviços Docker
docker service ls | grep central
```

## Scripts Python (substituem n8n)

### `collectors/comercial.py` — Cron 8h, 12h, 17h
- Busca vendas e eventos da API BoateBus
- Calcula KPIs, comissões, inadimplentes
- Salva em `/home/your-legacy-dashboard/data.json` **e** `/home/your-project/data.json`
- Em caso de erro: envia e-mail de alerta

### `collectors/chatwoot_leads.py` — Cron 8h05, 12h05, 17h05
- Busca conversas do Chatwoot do mês atual
- Cruza com vendas BoateBus para taxa de conversão
- Salva em `/home/your-project/chatwoot.json`

### `collectors/webhook.py` — FastAPI (sempre rodando em Docker)
- Endpoint POST `/conversascore-webhook`
- Classifica eventos: handoff / capturar / finalizar / ignorar
- Transcreve áudios via OpenAI Whisper
- Salva em PostgreSQL (`conversascore.conversas` + `conversascore.mensagens`)
- URL pública: `https://webhook.yourdomain.com`

### `collectors/scoring.py` — Cron 6h diário
- Consulta conversas finalizadas sem score
- Calcula critérios objetivos (velocidade, follow-up)
- Chama Claude API para critérios subjetivos (qualidade, tom)
- Salva em PostgreSQL (`conversascore.scores`)
- Gera `/home/your-project/scores.json`

## Alertas de Erro

Qualquer script que falhe envia e-mail automaticamente:
- **De**: alerts@yourcompany.com (Gmail App Password)
- **Para**: admin@yourcompany.com
- **Assunto**: `[BoateBus ERRO] nome_do_script — DD/MM/YYYY HH:MM`
- **Conteúdo**: nome do script, erro, traceback completo

## Schema PostgreSQL

```bash
# Verificar
PGCONTAINER=$(docker ps --filter "name=postgres" --format "{{.ID}}" | head -1)
docker exec -i $PGCONTAINER psql -U postgres -d n8n_queue -c "\dt conversascore.*"
```

Tabelas: `conversascore.conversas`, `conversascore.mensagens`, `conversascore.scores`, `conversascore.insights`

## Crontab no Servidor

```
0 8,12,17 * * * cd /home/your-project && python3 collectors/comercial.py >> /var/log/boatebus-comercial.log 2>&1
5 8,12,17 * * * cd /home/your-project && python3 collectors/chatwoot_leads.py >> /var/log/boatebus-chatwoot.log 2>&1
0 6 * * * cd /home/your-project && python3 collectors/scoring.py >> /var/log/boatebus-scoring.log 2>&1
```

Verificar logs:
```bash
tail -f /var/log/boatebus-comercial.log
tail -f /var/log/boatebus-chatwoot.log
tail -f /var/log/boatebus-scoring.log
```

## Abas do Dashboard

| Aba | Fonte | Status |
|-----|-------|--------|
| Comercial | `data.json` | PRONTA |
| Chatwoot Leads | `chatwoot.json` | PRONTA |
| ConversaScore | `scores.json` | PRONTA |
| Meta Ads | — | PLACEHOLDER (aguardando Meta OAuth) |
| Orgânico Social | — | PLACEHOLDER (aguardando Meta API) |

## Modelo ConversaScore

### 5 Critérios (nota 0-10 cada, ponderado para 0-100)

| # | Critério | Peso | Método |
|---|----------|------|--------|
| 1 | Velocidade de Resposta | 25% | Objetivo (timestamps) |
| 2 | Continuidade / Follow-up | 20% | Objetivo (timestamps) |
| 3 | Qualidade Comercial | 25% | Claude API |
| 4 | Tom e Personalização | 15% | Claude API |
| 5 | Resolução / Resultado | 15% | Heurística |

### Faixas — Velocidade de Resposta
- ≤ 5 min → nota 10
- ≤ 15 min → nota 7
- ≤ 1 hora → nota 4
- > 1 hora → nota 1

### Faixas — Follow-up
- Retoma em ≤ 12h → nota 10
- Retoma em ≤ 24h → nota 7
- Retoma em ≤ 48h → nota 4
- Não retoma (cliente foi o último) → nota 0
- Vendedor foi o último → nota 8

## Mensagens do Chatbot (Filtros)

### Mensagens de handoff (início da captura)
- `"Estou te direcionando para um dos nossos consultores"`
- `"nosso time de consultores seguem com você aqui"`

### Mensagens de recuperação (IGNORAR)
- `"você já viu a gente no insta"`
- `"O que está faltando para reservarmos sua festa"`
- `"Vamos reservar sua festa"`
- `"Ficou alguma dúvida que posso ser útil"`

## Cloudflare DNS

| Type | Name | IPv4 | Proxy |
|------|------|------|-------|
| A | painel | YOUR_SERVER_IP | Proxied |
| A | webhook | YOUR_SERVER_IP | Proxied |

## Troubleshooting

### Dashboard não carrega
```bash
docker service ls | grep central
docker service logs central_central-dashboard --tail 20
```

### Webhook não recebe eventos
```bash
# Verificar serviço
docker service ls | grep central-webhook
docker service logs central_central-webhook --tail 30

# Testar health
curl -s https://webhook.yourdomain.com/

# Testar endpoint manualmente
curl -X POST https://webhook.yourdomain.com/conversascore-webhook \
  -H "Content-Type: application/json" \
  -d '{"event":"message_created","conversation":{},"message":{}}'
```

### data.json não atualiza
```bash
tail -20 /var/log/boatebus-comercial.log
python3 /home/your-project/collectors/comercial.py
```

### Score não gera
```bash
tail -20 /var/log/boatebus-scoring.log
# Verificar conversas pendentes
PGCONTAINER=$(docker ps --filter "name=postgres" --format "{{.ID}}" | head -1)
docker exec -i $PGCONTAINER psql -U postgres -d n8n_queue \
  -c "SELECT COUNT(*) FROM conversascore.conversas WHERE status='finalizada';"
```
