#!/bin/bash
# ══════════════════════════════════════════════════════════════
# BoateBus Central — Script de Deploy
# Executar no servidor: bash scripts/deploy.sh
# ══════════════════════════════════════════════════════════════

set -e

CENTRAL_PATH="/home/your-project"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "BoateBus Central — Deploy"
echo "=================================================="

# ── 1. Cria diretório ──────────────────────────────────────────
echo "[1/5] Criando diretório $CENTRAL_PATH..."
mkdir -p $CENTRAL_PATH

# ── 2. Copia arquivos do dashboard ────────────────────────────
echo "[2/5] Copiando dashboard HTML..."
cp $REPO_DIR/src/index.html $CENTRAL_PATH/
cp $REPO_DIR/src/chatwoot.json $CENTRAL_PATH/ 2>/dev/null || true
cp $REPO_DIR/src/scores.json $CENTRAL_PATH/ 2>/dev/null || true

if [ ! -f "$CENTRAL_PATH/data.json" ]; then
  cp $REPO_DIR/src/data.json $CENTRAL_PATH/
  echo "  -> data.json copiado (sample)"
else
  echo "  -> data.json ja existe, mantendo dados reais"
fi

# ── 3. Schema PostgreSQL ──────────────────────────────────────
echo "[3/5] Aplicando schema PostgreSQL..."
PGCONTAINER=$(docker ps --filter "name=postgres" --format "{{.ID}}" | head -1)

if [ -z "$PGCONTAINER" ]; then
  echo "  AVISO: Container PostgreSQL nao encontrado, pulando schema"
else
  docker cp $REPO_DIR/sql/schema.sql $PGCONTAINER:/tmp/schema.sql
  docker exec -i $PGCONTAINER psql -U postgres -d n8n_queue -f /tmp/schema.sql
  echo "  -> Schema conversascore aplicado"
fi

# ── 4. Build das imagens Docker ───────────────────────────────
echo "[4/5] Build das imagens Docker..."

echo "  Building boatebus-webhook:latest..."
docker build -t boatebus-webhook:latest -f $REPO_DIR/Dockerfile.webhook $REPO_DIR

echo "  Building boatebus-cron:latest..."
docker build -t boatebus-cron:latest -f $REPO_DIR/Dockerfile.cron $REPO_DIR

# ── 5. Deploy do stack ────────────────────────────────────────
echo "[5/5] Deploying stack central..."
cp $REPO_DIR/docker-compose.yml $CENTRAL_PATH/
docker stack deploy -c $CENTRAL_PATH/docker-compose.yml central

echo ""
echo "=================================================="
echo "Deploy concluido!"
echo ""
echo "Dashboard:  https://dashboard.yourdomain.com"
echo "Webhook:    https://webhook.yourdomain.com/conversascore-webhook"
echo ""
echo "Configurar webhook no Chatwoot:"
echo "  Settings -> Integrations -> Webhooks -> Add"
echo "  URL: https://webhook.yourdomain.com/conversascore-webhook"
echo "  Eventos: message_created, conversation_status_changed"
echo ""

sleep 5
echo "=== Servicos central ==="
docker service ls | grep central
