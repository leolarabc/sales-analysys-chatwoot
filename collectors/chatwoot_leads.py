#!/usr/bin/env python3
"""
Coleta conversas do Chatwoot + vendas BoateBus e gera chatwoot.json.
Substitui o workflow n8n "BoateBus - Chatwoot Leads Dashboard".
Agendamento: 8h05, 12h05, 17h05 (via crontab)
"""

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.alerts import send_alert

# ── Configurações ──────────────────────────────────────────────
CHATWOOT_ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID", "YOUR_ACCOUNT_ID")
CHATWOOT_BASE = "app.chatwoot.com"

# Agentes com meta (id → nome)
AGENTES = {
    "100001": "Agent One",
    "100002": "Agent Two",
}

OUTPUT_PATH = "/home/your-project/chatwoot.json"


def fetch_chatwoot_conversations(token: str, inicio_mes_ts: int) -> list:
    """Busca todas as conversas do mês no Chatwoot."""
    headers = {
        "api_access_token": token,
        "Content-Type": "application/json",
    }
    all_conversas = []
    page = 1

    while page <= 50:  # limite de segurança
        params = {
            "page": page,
            "created_at_after": inicio_mes_ts,
        }
        url = f"https://{CHATWOOT_BASE}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations"
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()

        # Chatwoot retorna { data: { payload: [...], meta: {...} } }
        payload = (
            body.get("data", {}).get("payload")
            or body.get("payload")
            or []
        )
        if not payload:
            break
        all_conversas.extend(payload)
        page += 1

    return all_conversas


def fetch_boatebus_vendas(token: str, mes: str, ano: str) -> dict[str, int]:
    """Busca vendas do BoateBus e conta festas por vendedor."""
    headers = {"Authorization": token}
    all_vendas = []
    page = 1
    while True:
        r = requests.get(
            f"https://api.boatebus.com.br/sales/{mes}/{ano}?page={page}",
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        dados = body.get("dados") or []
        all_vendas.extend(dados)
        if page >= int(body.get("totalPages", 1)):
            break
        page += 1

    festas_por_vendedor: dict[str, set] = {}
    for v in all_vendas:
        nome = v.get("vendedor") or "Outros"
        if nome not in festas_por_vendedor:
            festas_por_vendedor[nome] = set()
        if v.get("produto") is not None:
            festas_por_vendedor[nome].add(v.get("evento_id"))

    return {nome: len(s) for nome, s in festas_por_vendedor.items()}


def main():
    chatwoot_token = os.environ["CHATWOOT_TOKEN"]
    boatebus_token = os.environ["BOATEBUS_TOKEN"]

    hoje = date.today()
    mes = str(hoje.month).zfill(2)
    ano = str(hoje.year)

    # Timestamp do início do mês (epoch seconds)
    inicio_mes = datetime(hoje.year, hoje.month, 1, tzinfo=timezone.utc)
    inicio_mes_ts = int(inicio_mes.timestamp())

    print(f"[chatwoot] Buscando conversas desde {inicio_mes.strftime('%d/%m/%Y')}...")
    conversas = fetch_chatwoot_conversations(chatwoot_token, inicio_mes_ts)
    print(f"[chatwoot] {len(conversas)} conversas encontradas")

    print(f"[chatwoot] Buscando vendas {mes}/{ano}...")
    festas_convertidas = fetch_boatebus_vendas(boatebus_token, mes, ano)
    total_festas = sum(festas_convertidas.values())

    # ── Métricas por agente ────────────────────────────────────
    por_agente: dict[str, dict] = {
        aid: {
            "nome": nome,
            "atribuidas": 0,
            "pendentes": 0,
            "nao_atendidas": 0,
            "tempos_resposta": [],
            "convertidas": festas_convertidas.get(nome, 0),
        }
        for aid, nome in AGENTES.items()
    }

    total_conversas = len(conversas)
    pendentes = 0
    nao_atendidos = 0
    status_count: dict[str, int] = {}

    for conv in conversas:
        status = conv.get("status", "unknown")
        status_count[status] = status_count.get(status, 0) + 1

        assignee_id = str(conv.get("meta", {}).get("assignee", {}).get("id", "") or conv.get("assignee_id", "") or "")

        if status == "pending":
            pendentes += 1

        # Não atendido: aberto sem resposta humana
        if status == "open" and (not assignee_id or conv.get("messages_count", 0) <= 1):
            nao_atendidos += 1

        if assignee_id in por_agente:
            ag = por_agente[assignee_id]
            ag["atribuidas"] += 1

            if status == "pending":
                ag["pendentes"] += 1

            if status == "open" and (not assignee_id or conv.get("messages_count", 0) <= 1):
                ag["nao_atendidas"] += 1

            # Tempo de primeira resposta
            first_reply = conv.get("first_reply_created_at")
            created_at = conv.get("created_at")
            if first_reply and created_at:
                diff_min = (first_reply - created_at) / 60
                if 0 < diff_min < 1440:  # entre 0 e 24h
                    ag["tempos_resposta"].append(diff_min)

    # ── Agrega métricas dos agentes ───────────────────────────
    todos_tempos: list[float] = []
    vendedores_arr = []

    for ag in sorted(por_agente.values(), key=lambda x: -x["atribuidas"]):
        todos_tempos.extend(ag["tempos_resposta"])
        tempo_medio = (
            round(sum(ag["tempos_resposta"]) / len(ag["tempos_resposta"]), 1)
            if ag["tempos_resposta"]
            else None
        )
        taxa_conversao = round((ag["convertidas"] / ag["atribuidas"]) * 100, 1) if ag["atribuidas"] > 0 else 0.0
        vendedores_arr.append({
            "nome": ag["nome"],
            "atribuidas": ag["atribuidas"],
            "pendentes": ag["pendentes"],
            "naoAtendidas": ag["nao_atendidas"],
            "tempoMedio": tempo_medio,
            "convertidas": ag["convertidas"],
            "taxaConversao": taxa_conversao,
        })

    tempo_medio_geral = (
        round(sum(todos_tempos) / len(todos_tempos), 1) if todos_tempos else None
    )

    # ── Funil ──────────────────────────────────────────────────
    total_assigned = sum(ag["atribuidas"] for ag in por_agente.values())
    em_negociacao = (
        status_count.get("open", 0)
        + status_count.get("pending", 0)
        + status_count.get("snoozed", 0)
    )
    resolvidas = status_count.get("resolved", 0)

    funil = [
        {"etapa": "Leads recebidos (bot)", "valor": total_conversas},
        {"etapa": "Atribuídos ao vendedor", "valor": total_assigned},
        {"etapa": "Em negociação (aberto/pendente)", "valor": em_negociacao},
        {"etapa": "Resolvidos", "valor": resolvidas},
        {"etapa": "Convertidos (festa fechada)", "valor": total_festas},
    ]

    output = {
        "atualizadoEm": datetime.now().isoformat(),
        "periodo": {"mes": hoje.month, "ano": hoje.year},
        "kpis": {
            "totalConversas": total_conversas,
            "pendentes": pendentes,
            "naoAtendidos": nao_atendidos,
            "tempoMedioPrimeiraResposta": tempo_medio_geral,
            "convertidos": total_festas,
        },
        "porVendedor": vendedores_arr,
        "funil": funil,
        "statusBreakdown": status_count,
    }

    json_str = json.dumps(output, ensure_ascii=False, indent=2)
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(OUTPUT_PATH).write_text(json_str, encoding="utf-8")
    print(f"[chatwoot] Salvo em {OUTPUT_PATH} ({len(json_str)} bytes)")
    print(f"[chatwoot] OK — Conversas: {total_conversas} | Convertidos: {total_festas}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        send_alert("chatwoot_leads.py", e)
        sys.exit(1)
