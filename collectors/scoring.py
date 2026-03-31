#!/usr/bin/env python3
"""
Calcula o ConversaScore das conversas finalizadas via Claude API e gera scores.json.
Substitui o workflow n8n "FiestaHub - ConversaScore Diário".
Agendamento: 6h diário (via crontab)
"""

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.alerts import send_alert
from utils.db import get_connection

OUTPUT_PATH = "/home/your-project/scores.json"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_CONVERSAS_POR_RUN = 30


# ── Critérios objetivos ────────────────────────────────────────

def calc_velocidade(handoff_at: datetime | None, primeira_resposta_at: datetime | None) -> tuple[int, float | None]:
    """Critério 1 — Velocidade de Resposta (25%)."""
    if not handoff_at or not primeira_resposta_at:
        return 0, None

    diff_min = (primeira_resposta_at - handoff_at).total_seconds() / 60

    # Heurística: desconta ~15h por cada noite cruzada fora do horário comercial
    if diff_min > 540:
        dias = int(diff_min // 1440)
        diff_min -= dias * 900

    diff_min = max(diff_min, 0)

    if diff_min <= 5:
        nota = 10
    elif diff_min <= 15:
        nota = 7
    elif diff_min <= 60:
        nota = 4
    else:
        nota = 1

    return nota, round(diff_min, 1)


def calc_followup(mensagens: list) -> tuple[int, bool, float | None]:
    """
    Critério 2 — Continuidade / Follow-up (20%).
    Verifica se houve silêncio do cliente seguido de retomada do vendedor.
    """
    ultimo_msg_cliente: datetime | None = None
    vendedor_retomou_apos: float | None = None
    followup_retomou = False

    for msg in mensagens:
        enviada_at = msg.get("enviada_at")
        if isinstance(enviada_at, str):
            enviada_at = datetime.fromisoformat(enviada_at.replace("Z", "+00:00"))

        remetente = msg.get("remetente", "")

        if remetente == "cliente":
            ultimo_msg_cliente = enviada_at
            vendedor_retomou_apos = None
        elif remetente == "vendedor" and ultimo_msg_cliente and enviada_at:
            diff_horas = (enviada_at - ultimo_msg_cliente).total_seconds() / 3600
            if diff_horas > 4:  # silêncio significativo
                vendedor_retomou_apos = diff_horas
                followup_retomou = True

    nota = 0
    tempo_horas = None

    if mensagens:
        ultima = mensagens[-1]
        if followup_retomou and vendedor_retomou_apos is not None:
            tempo_horas = vendedor_retomou_apos
            if vendedor_retomou_apos <= 12:
                nota = 10
            elif vendedor_retomou_apos <= 24:
                nota = 7
            elif vendedor_retomou_apos <= 48:
                nota = 4
            else:
                nota = 2
        elif ultima.get("remetente") == "vendedor":
            nota = 8  # Vendedor foi o último — bom
        else:
            nota = 0  # Cliente foi o último, sem follow-up

    return nota, followup_retomou, tempo_horas


# ── Análise por IA ─────────────────────────────────────────────

def analyze_with_claude(conversa_texto: str, conv_id: int) -> dict:
    """
    Critérios 3 (Qualidade Comercial) e 4 (Tom e Personalização) via Claude API.
    Retorna dict com notas e análises.
    """
    if not conversa_texto or conversa_texto.count("\n") < 2:
        return {
            "qualidade_nota": 5,
            "qualidade_analise": "Conversa muito curta para análise completa.",
            "tom_nota": 5,
            "tom_analise": "Conversa muito curta para análise completa.",
            "tokens_usados": 0,
        }

    api_key = os.environ["ANTHROPIC_API_KEY"]

    prompt = f"""Você é um analista de qualidade de atendimento comercial. Analise a conversa abaixo entre um vendedor da FiestaHub (empresa de festas e eventos em Belo Horizonte) e um cliente.

AVALIE DOIS CRITÉRIOS:

**CRITÉRIO 3 - QUALIDADE COMERCIAL (nota 0-10):**
- O vendedor apresentou opções de pacote/serviço?
- Respondeu dúvidas sobre preço, data, capacidade?
- Conduziu para fechamento (pediu dados, mandou proposta, alinhou data)?
- O vendedor VENDEU ou só RESPONDEU?

**CRITÉRIO 4 - TOM E PERSONALIZAÇÃO (nota 0-10):**
- O vendedor foi cordial e educado?
- Usou o nome do cliente?
- Personalizou a abordagem (mencionou tipo de festa, número de convidados, etc)?
- Evitou respostas genéricas/robotizadas?
- Demonstrou entusiasmo pelo evento do cliente?

O atendimento é via WhatsApp, então linguagem informal e emojis são NORMAIS e POSITIVOS.

Responda SOMENTE em JSON válido, sem markdown:
{{"qualidade_nota": X, "qualidade_analise": "justificativa curta", "tom_nota": X, "tom_analise": "justificativa curta"}}

CONVERSA:
{conversa_texto[:4000]}"""

    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()

    tokens = (result.get("usage") or {}).get("input_tokens", 0) + (result.get("usage") or {}).get("output_tokens", 0)
    text = (result.get("content") or [{}])[0].get("text", "")

    try:
        clean = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        qualidade_nota = max(0, min(10, float(parsed.get("qualidade_nota", 5))))
        tom_nota = max(0, min(10, float(parsed.get("tom_nota", 5))))
        return {
            "qualidade_nota": qualidade_nota,
            "qualidade_analise": parsed.get("qualidade_analise", ""),
            "tom_nota": tom_nota,
            "tom_analise": parsed.get("tom_analise", ""),
            "tokens_usados": tokens,
        }
    except Exception as e:
        print(f"[scoring] Erro ao parsear resposta Claude (conv {conv_id}): {e}")
        return {
            "qualidade_nota": 5.0,
            "qualidade_analise": "Erro ao parsear análise.",
            "tom_nota": 5.0,
            "tom_analise": "Erro ao parsear análise.",
            "tokens_usados": tokens,
        }


# ── Geração do scores.json ─────────────────────────────────────

def generate_scores_json(conn) -> dict:
    """Consulta o banco e gera o payload completo do scores.json."""
    hoje = date.today()
    mes = hoje.month
    ano = hoje.year

    with conn.cursor() as cur:
        # Score geral do mês
        cur.execute(
            """
            SELECT AVG(s.score_total) AS media, COUNT(*) AS total
            FROM conversascore.scores s
            JOIN conversascore.conversas c ON c.id = s.conversa_id
            WHERE EXTRACT(MONTH FROM c.handoff_at) = %s
              AND EXTRACT(YEAR  FROM c.handoff_at) = %s
            """,
            (mes, ano),
        )
        geral = cur.fetchone()

        # Por vendedor
        cur.execute(
            """
            SELECT
                c.vendedor_nome      AS nome,
                AVG(s.score_total)   AS media,
                COUNT(*)             AS conversas_analisadas,
                AVG(s.velocidade_nota) AS velocidade,
                AVG(s.followup_nota)   AS followup,
                AVG(s.qualidade_nota)  AS qualidade,
                AVG(s.tom_nota)        AS tom,
                AVG(s.resultado_nota)  AS resultado
            FROM conversascore.scores s
            JOIN conversascore.conversas c ON c.id = s.conversa_id
            WHERE EXTRACT(MONTH FROM c.handoff_at) = %s
              AND EXTRACT(YEAR  FROM c.handoff_at) = %s
            GROUP BY c.vendedor_nome
            ORDER BY AVG(s.score_total) DESC
            """,
            (mes, ano),
        )
        vendedores_rows = cur.fetchall()

        # Últimas 20 conversas
        cur.execute(
            """
            SELECT
                c.cliente_nome         AS cliente,
                c.vendedor_nome        AS vendedor,
                c.finalizada_at        AS data,
                s.velocidade_nota      AS velocidade,
                s.followup_nota        AS followup,
                s.qualidade_nota       AS qualidade,
                s.tom_nota             AS tom,
                s.resultado_nota       AS resultado,
                s.score_total          AS "scoreTotal"
            FROM conversascore.scores s
            JOIN conversascore.conversas c ON c.id = s.conversa_id
            WHERE EXTRACT(MONTH FROM c.handoff_at) = %s
              AND EXTRACT(YEAR  FROM c.handoff_at) = %s
            ORDER BY s.scored_at DESC
            LIMIT 20
            """,
            (mes, ano),
        )
        conversas_rows = cur.fetchall()

        # Insights do mês (gerados externamente)
        cur.execute(
            "SELECT tipo, texto FROM conversascore.insights WHERE mes = %s AND ano = %s ORDER BY gerado_at DESC",
            (mes, ano),
        )
        insights_rows = cur.fetchall()

    def _f(v):
        return round(float(v), 1) if v is not None else 0.0

    return {
        "atualizadoEm": datetime.now().isoformat(),
        "periodo": {"mes": mes, "ano": ano},
        "scoreGeral": {
            "media": _f(geral["media"] if geral else None),
            "totalAnalisadas": int(geral["total"] if geral else 0),
        },
        "vendedores": [
            {
                "nome": v["nome"],
                "media": _f(v["media"]),
                "conversasAnalisadas": int(v["conversas_analisadas"]),
                "criterios": {
                    "velocidade": _f(v["velocidade"]),
                    "followup": _f(v["followup"]),
                    "qualidade": _f(v["qualidade"]),
                    "tom": _f(v["tom"]),
                    "resultado": _f(v["resultado"]),
                },
            }
            for v in vendedores_rows
        ],
        "conversas": [
            {
                "cliente": c["cliente"],
                "vendedor": c["vendedor"],
                "data": c["data"].isoformat() if c["data"] else None,
                "velocidade": _f(c["velocidade"]),
                "followup": _f(c["followup"]),
                "qualidade": _f(c["qualidade"]),
                "tom": _f(c["tom"]),
                "resultado": _f(c["resultado"]),
                "scoreTotal": _f(c["scoreTotal"]),
            }
            for c in conversas_rows
        ],
        "insights": [{"tipo": i["tipo"], "texto": i["texto"]} for i in insights_rows],
    }


# ── Main ───────────────────────────────────────────────────────

def main():
    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.id, c.chatwoot_conversation_id, c.cliente_nome,
                c.vendedor_nome, c.vendedor_agent_id,
                c.handoff_at, c.primeira_resposta_at,
                c.ultima_mensagem_at, c.finalizada_at,
                c.total_mensagens_cliente, c.total_mensagens_vendedor,
                c.total_audios_transcritos
            FROM conversascore.conversas c
            LEFT JOIN conversascore.scores s ON s.conversa_id = c.id
            WHERE c.status = 'finalizada'
              AND s.id IS NULL
              AND c.total_mensagens_vendedor > 0
            ORDER BY c.finalizada_at DESC
            LIMIT %s
            """,
            (MAX_CONVERSAS_POR_RUN,),
        )
        pendentes = cur.fetchall()

    print(f"[scoring] {len(pendentes)} conversas pendentes de score")

    if pendentes:
        for conv in pendentes:
            conv_id = conv["id"]
            print(f"[scoring] Processando conv {conv['chatwoot_conversation_id']} → {conv['vendedor_nome']}...")

            # Busca mensagens
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT remetente, tipo, conteudo, enviada_at
                    FROM conversascore.mensagens
                    WHERE conversa_id = %s AND is_mensagem_bot = false
                    ORDER BY enviada_at ASC
                    """,
                    (conv_id,),
                )
                mensagens = cur.fetchall()

            # Critério 1 — Velocidade
            vel_nota, vel_tempo = calc_velocidade(conv["handoff_at"], conv["primeira_resposta_at"])

            # Critério 2 — Follow-up
            fup_nota, fup_retomou, fup_horas = calc_followup(mensagens)

            # Prepara texto para IA
            conversa_texto = "\n".join(
                f"[{'CLIENTE' if m['remetente'] == 'cliente' else 'VENDEDOR'}]: {m['conteudo'] or '[sem texto]'}"
                for m in mensagens
            )

            # Critérios 3 e 4 — Claude API
            try:
                ai = analyze_with_claude(conversa_texto, conv_id)
            except Exception as e:
                print(f"[scoring] Erro Claude API (conv {conv_id}): {e}")
                send_alert("scoring.py", e, f"conv_id={conv_id}")
                ai = {"qualidade_nota": 5, "qualidade_analise": "Erro IA", "tom_nota": 5, "tom_analise": "Erro IA", "tokens_usados": 0}

            # Critério 5 — Resultado (heurística)
            resultado_nota = 5

            # Score total ponderado (escala 0–100)
            score_total = (
                vel_nota * 0.25
                + fup_nota * 0.20
                + ai["qualidade_nota"] * 0.25
                + ai["tom_nota"] * 0.15
                + resultado_nota * 0.15
            ) * 10

            # Salva no banco
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversascore.scores (
                        conversa_id,
                        velocidade_nota, velocidade_tempo_min,
                        followup_nota, followup_retomou, followup_tempo_horas,
                        qualidade_nota, qualidade_analise,
                        tom_nota, tom_analise,
                        resultado_nota, resultado_status,
                        score_total, tokens_consumidos
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (conversa_id) DO UPDATE SET
                        velocidade_nota   = EXCLUDED.velocidade_nota,
                        followup_nota     = EXCLUDED.followup_nota,
                        qualidade_nota    = EXCLUDED.qualidade_nota,
                        tom_nota          = EXCLUDED.tom_nota,
                        resultado_nota    = EXCLUDED.resultado_nota,
                        score_total       = EXCLUDED.score_total,
                        scored_at         = NOW()
                    """,
                    (
                        conv_id,
                        vel_nota, vel_tempo,
                        fup_nota, fup_retomou, fup_horas,
                        ai["qualidade_nota"], ai["qualidade_analise"],
                        ai["tom_nota"], ai["tom_analise"],
                        resultado_nota, "em_andamento",
                        score_total, ai["tokens_usados"],
                    ),
                )
                cur.execute(
                    "UPDATE conversascore.conversas SET status = 'scored' WHERE id = %s",
                    (conv_id,),
                )
            conn.commit()
            print(f"[scoring] Score: {score_total:.1f} | V:{vel_nota} F:{fup_nota} Q:{ai['qualidade_nota']} T:{ai['tom_nota']}")

    # Gera scores.json independente de ter processado novas conversas
    print("[scoring] Gerando scores.json...")
    output = generate_scores_json(conn)
    conn.close()

    json_str = json.dumps(output, ensure_ascii=False, indent=2, default=str)
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(OUTPUT_PATH).write_text(json_str, encoding="utf-8")
    print(f"[scoring] Salvo em {OUTPUT_PATH} — média: {output['scoreGeral']['media']} | total: {output['scoreGeral']['totalAnalisadas']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        send_alert("scoring.py", e)
        sys.exit(1)
