#!/usr/bin/env python3
"""
Coleta dados comerciais da API FiestaHub e gera data.json.
Substitui o workflow n8n "FiestaHub — Dashboard de Vendas".
Agendamento: 8h, 12h, 17h (via crontab)
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.alerts import send_alert

# ── Configurações ──────────────────────────────────────────────
META_MENSAL = 320_000
META_INDIVIDUAL = 160_000
VENDEDORES_COM_META = ["Agent One", "Agent Two"]

PRODUTOS_EXTRAS_IDS = {1, 20, 21, 24, 25, 26, 27, 31, 32}
PRODUTOS_EXTRAS_PATTERNS = [
    re.compile(r"open\s*bar", re.I),
    re.compile(r"combo", re.I),
    re.compile(r"kit\s*de\s*energ", re.I),
    re.compile(r"personaliza[çc][aã]o\s*de\s*copos", re.I),
]

FAIXAS_COMISSAO = [
    (250_000, 0.0300),
    (225_000, 0.0275),
    (200_000, 0.0250),
    (175_000, 0.0225),
    (150_000, 0.0200),
    (125_000, 0.0175),
    (100_000, 0.0150),
    (75_000, 0.0120),
    (50_000, 0.0100),
]

FAIXAS_BONUS = [
    (400_000, 1500),
    (350_000, 1250),
    (300_000, 1000),
    (250_000, 750),
    (200_000, 500),
    (150_000, 250),
    (100_000, 150),
]

OUTPUT_PATHS = [
    "/home/your-legacy-dashboard/data.json",
    "/home/your-project/data.json",
]


def is_produto_extra(produto_id, produto_nome):
    if produto_id in PRODUTOS_EXTRAS_IDS:
        return True
    if produto_nome:
        for p in PRODUTOS_EXTRAS_PATTERNS:
            if p.search(str(produto_nome)):
                return True
    return False


def calc_comissao_individual(pago):
    for min_val, pct in FAIXAS_COMISSAO:
        if pago >= min_val:
            return pct * 100, pago * pct
    return 0.0, 0.0


def calc_bonus_global(pago_total):
    for min_val, bonus in FAIXAS_BONUS:
        if pago_total >= min_val:
            return bonus
    return 0


def fetch_all_pages(hostname, token, path_prefix):
    """Busca todas as páginas de uma API paginada."""
    headers = {"Authorization": token}
    all_dados = []
    page = 1
    while True:
        url = f"https://{hostname}{path_prefix}?page={page}"
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        body = r.json()
        # API de events retorna array
        if isinstance(body, list):
            body = body[0]
        dados = body.get("dados") or body.get("data") or []
        all_dados.extend(dados)
        total_pages = int(body.get("totalPages", 1))
        if page >= total_pages:
            break
        page += 1
    return all_dados


def dias_uteis_mes(hoje: date):
    """
    Conta dias úteis (seg–sáb, sem domingo) do mês atual.
    Retorna (passados, restantes) onde 'hoje' entra em restantes.
    Replica a lógica do workflow n8n original.
    """
    passados = 0
    restantes = 0
    d = date(hoje.year, hoje.month, 1)
    while d.month == hoje.month:
        if d.weekday() != 6:  # 6 = domingo em Python
            if d < hoje:
                passados += 1
            else:
                restantes += 1
        d += timedelta(days=1)
    return passados, restantes


def main():
    token = os.environ["FIESTAHUB_TOKEN"]
    hoje = date.today()
    mes = str(hoje.month).zfill(2)
    ano = str(hoje.year)

    print(f"[comercial] Buscando {mes}/{ano}...")

    vendas = fetch_all_pages("api.fiestahub.com.br", token, f"/sales/{mes}/{ano}")
    print(f"[comercial] {len(vendas)} vendas")

    eventos = fetch_all_pages("api.fiestahub.com.br", token, f"/reports/events/{mes}/{ano}")
    print(f"[comercial] {len(eventos)} eventos")

    # Mapa evento_id → vendedor (da API de vendas)
    evento_para_vendedor = {}
    for v in vendas:
        eid = v.get("evento_id")
        if eid and v.get("vendedor"):
            evento_para_vendedor[eid] = v["vendedor"]

    # Faturamento por evento
    fat_por_evento: dict[int, float] = {}
    for v in vendas:
        eid = v.get("evento_id")
        if eid:
            fat_por_evento[eid] = fat_por_evento.get(eid, 0.0) + float(v.get("total", 0))

    faturamento_total = sum(float(v.get("total", 0)) for v in vendas)

    # Festas únicas (somente se tem produto principal)
    eventos_unicos: dict[int, dict] = {}
    for v in vendas:
        eid = v.get("evento_id")
        if not eid:
            continue
        if eid not in eventos_unicos:
            eventos_unicos[eid] = {"vendedor": v.get("vendedor"), "tem_principal": False}
        if v.get("produto") is not None and not is_produto_extra(v.get("produto_id"), v.get("produto")):
            eventos_unicos[eid]["tem_principal"] = True

    total_festas = sum(1 for e in eventos_unicos.values() if e["tem_principal"])
    ticket_medio = faturamento_total / total_festas if total_festas > 0 else 0.0

    # Dias úteis
    dias_passados, dias_restantes = dias_uteis_mes(hoje)
    media_diaria = faturamento_total / dias_passados if dias_passados > 0 else 0.0
    projecao = media_diaria * (dias_passados + dias_restantes)
    deficit = projecao - META_MENSAL
    percentual_meta = (faturamento_total / META_MENSAL) * 100
    necessario_por_dia = (META_MENSAL - faturamento_total) / dias_restantes if dias_restantes > 0 else 0.0

    # Pagos e inadimplentes (via API de eventos)
    faturamento_pago_total = 0.0
    pago_por_vendedor: dict[str, float] = {}
    inadimplentes = []

    for ev in eventos:
        eid = ev.get("evento_id")
        vendedor = evento_para_vendedor.get(eid, "Não identificado")
        movs = ev.get("movimentacoes") or {}
        paid = movs.get("paid") or []
        nopaid = movs.get("nopaid") or []

        total_pago = sum(float(p.get("valor", 0)) for p in paid)
        total_pendente = sum(float(p.get("valor", 0)) for p in nopaid)

        if total_pago > 0:
            valor_evento = fat_por_evento.get(eid, total_pago)
            faturamento_pago_total += valor_evento
            pago_por_vendedor[vendedor] = pago_por_vendedor.get(vendedor, 0.0) + valor_evento

        if total_pago == 0:
            inadimplentes.append({
                "eventoId": eid,
                "eventoTitulo": ev.get("evento_titulo"),
                "clienteNome": (ev.get("cliente_nome") or "").strip(),
                "telefone": ev.get("cliente_celular") or ev.get("cliente_telefone") or "",
                "dataEvento": ev.get("evento_data_inicio"),
                "vendedor": vendedor,
                "valorPago": 0,
                "valorPendente": fat_por_evento.get(eid, total_pendente),
                "semPagamento": True,
            })

    # Comissões
    bonus_global = calc_bonus_global(faturamento_pago_total)
    comissoes_vendedores = []
    for nome in VENDEDORES_COM_META:
        pago = pago_por_vendedor.get(nome, 0.0)
        pct, valor = calc_comissao_individual(pago)
        comissoes_vendedores.append({
            "nome": nome,
            "faturamentoPago": pago,
            "percentualComissao": pct,
            "comissaoIndividual": valor,
            "bonus": bonus_global,
            "totalComissao": valor + bonus_global,
        })

    # Desempenho por vendedor
    por_vendedor: dict[str, dict] = {}
    for v in vendas:
        nome = v.get("vendedor") or "Outros"
        if nome not in por_vendedor:
            por_vendedor[nome] = {"faturamento": 0.0, "eventos": set()}
        por_vendedor[nome]["faturamento"] += float(v.get("total", 0))
        if v.get("produto") is not None and not is_produto_extra(v.get("produto_id"), v.get("produto")):
            por_vendedor[nome]["eventos"].add(v.get("evento_id"))

    vendedores = []
    for nome, dados in sorted(por_vendedor.items(), key=lambda x: -x[1]["faturamento"]):
        festas = len(dados["eventos"])
        fat = dados["faturamento"]
        vendedores.append({
            "nome": nome,
            "festas": festas,
            "faturamento": fat,
            "faturamentoPago": pago_por_vendedor.get(nome, 0.0),
            "ticketMedio": fat / festas if festas > 0 else 0.0,
            "meta": META_INDIVIDUAL if nome in VENDEDORES_COM_META else None,
            "percentualMeta": (fat / META_INDIVIDUAL) * 100 if nome in VENDEDORES_COM_META else None,
        })

    output = {
        "atualizadoEm": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "periodo": {
            "mes": hoje.month,
            "ano": hoje.year,
            "diasUteisPassados": dias_passados,
            "diasUteisRestantes": dias_restantes,
        },
        "kpis": {
            "faturamentoTotal": faturamento_total,
            "faturamentoPago": faturamento_pago_total,
            "totalFestas": total_festas,
            "ticketMedio": ticket_medio,
            "mediaDiaria": media_diaria,
        },
        "meta": {
            "valor": META_MENSAL,
            "projecao": projecao,
            "deficit": deficit,
            "percentualMeta": percentual_meta,
            "necessarioPorDia": necessario_por_dia,
            "statusColor": "green" if percentual_meta >= 100 else "yellow" if percentual_meta >= 80 else "red",
        },
        "vendedores": vendedores,
        "inadimplentes": inadimplentes,
        "comissoes": {
            "faturamentoPagoTotal": faturamento_pago_total,
            "bonusGlobal": bonus_global,
            "vendedores": comissoes_vendedores,
        },
    }

    json_str = json.dumps(output, ensure_ascii=False, indent=2)
    for path in OUTPUT_PATHS:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json_str, encoding="utf-8")
        print(f"[comercial] Salvo em {path}")

    print(f"[comercial] OK — Festas: {total_festas} | Faturamento: R${faturamento_total:,.2f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        send_alert("comercial.py", e)
        sys.exit(1)
