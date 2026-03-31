"""
Servidor webhook FastAPI — recebe eventos do Chatwoot em tempo real.
Substitui o workflow n8n "NightBus - ConversaScore Captura".

Deploy: Docker service (ver docker-compose.yml)
URL pública: https://webhook.yourdomain.com/conversascore-webhook
Configurar no Chatwoot: Settings → Integrations → Webhooks
  Eventos: message_created, conversation_status_changed
"""

import io
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

import psycopg2
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.alerts import send_alert
from utils.db import get_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [webhook] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="NightBus Webhook", docs_url=None, redoc_url=None)

# ── Constantes ─────────────────────────────────────────────────
AGENTES: dict[int, str] = {
    100001: "Agent One",
    100002: "Agent Two",
}

HANDOFF_MSGS = [
    "Estou te direcionando para um dos nossos consultores",
    "nosso time de consultores seguem com você aqui",
]

RECOVERY_MSGS = [
    "você já viu a gente no insta",
    "O que está faltando para reservarmos sua festa",
    "Vamos reservar sua festa",
    "Ficou alguma dúvida que posso ser útil",
]


# ── Helpers ────────────────────────────────────────────────────

def classify_event(body: dict) -> dict:
    """Classifica o evento recebido do Chatwoot."""
    event = body.get("event", "")
    conversation = body.get("conversation") or {}
    message = body.get("message") or body.get("content_attributes") or {}

    msg_content = (message.get("content") or "").strip()
    message_type = message.get("message_type")  # 0=incoming, 1=outgoing, 2=activity
    attachments = message.get("attachments") or []

    action = "ignorar"
    motivo = ""

    if event == "message_created":
        is_handoff = any(m in msg_content for m in HANDOFF_MSGS)
        is_recovery = any(m in msg_content for m in RECOVERY_MSGS)

        if is_handoff:
            action = "handoff"
            motivo = "Mensagem de encerramento do bot detectada"
        elif is_recovery:
            action = "ignorar"
            motivo = "Mensagem de recuperação do bot"
        elif message_type == 2:
            action = "ignorar"
            motivo = "Mensagem de atividade (sistema)"
        else:
            action = "capturar"
            motivo = "Mensagem normal pós-handoff"

    elif event == "conversation_status_changed":
        if conversation.get("status") == "resolved":
            action = "finalizar"
            motivo = "Conversa resolvida"
        else:
            action = "ignorar"
            motivo = f"Status mudou para: {conversation.get('status')}"
    else:
        action = "ignorar"
        motivo = f"Evento não relevante: {event}"

    # Detecta áudio
    audio_att = next(
        (a for a in attachments if a.get("file_type") == "audio" or
         (a.get("content_type") or "").startswith("audio/")),
        None,
    )

    meta = conversation.get("meta") or {}
    assignee = meta.get("assignee") or {}
    sender = meta.get("sender") or {}

    assignee_id = assignee.get("id") or conversation.get("assignee_id")
    assignee_name = AGENTES.get(assignee_id, "Não identificado")
    sender_type = "cliente" if message_type == 0 else "vendedor"

    import datetime
    ts_raw = message.get("created_at")
    if ts_raw:
        timestamp = datetime.datetime.utcfromtimestamp(ts_raw).isoformat() + "Z"
    else:
        timestamp = datetime.datetime.utcnow().isoformat() + "Z"

    return {
        "action": action,
        "motivo": motivo,
        "conversation_id": conversation.get("id"),
        "contact_id": sender.get("id") or conversation.get("contact_id"),
        "contact_name": sender.get("name") or "",
        "contact_phone": sender.get("phone_number") or "",
        "assignee_id": assignee_id,
        "assignee_name": assignee_name,
        "message_id": message.get("id"),
        "message_content": msg_content,
        "message_type": sender_type,
        "has_audio": audio_att is not None,
        "audio_url": (audio_att or {}).get("data_url") or (audio_att or {}).get("file_url"),
        "timestamp": timestamp,
        "conversation_status": conversation.get("status"),
    }


def transcribe_audio(audio_url: str) -> str:
    """Baixa áudio e transcreve via OpenAI Whisper."""
    openai_key = os.environ["OPENAI_API_KEY"]

    # Baixa o áudio
    r = requests.get(audio_url, timeout=60)
    r.raise_for_status()
    audio_bytes = r.content

    files = {"file": ("audio.ogg", io.BytesIO(audio_bytes), "audio/ogg")}
    data = {"model": "whisper-1", "language": "pt"}
    headers = {"Authorization": f"Bearer {openai_key}"}

    resp = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers=headers,
        files=files,
        data=data,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("text") or "[Transcrição vazia]"


def handle_handoff(ev: dict, conn) -> None:
    """Registra o início de uma conversa (handoff do bot para vendedor)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversascore.conversas (
                chatwoot_conversation_id, chatwoot_contact_id, cliente_nome,
                cliente_telefone, vendedor_agent_id, vendedor_nome, handoff_at, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'aberta')
            ON CONFLICT (chatwoot_conversation_id) DO UPDATE SET
                vendedor_agent_id = EXCLUDED.vendedor_agent_id,
                vendedor_nome     = EXCLUDED.vendedor_nome,
                -- Preserva o handoff mais antigo (pode haver 2 msgs de handoff na mesma conversa)
                handoff_at        = LEAST(conversascore.conversas.handoff_at, EXCLUDED.handoff_at),
                updated_at        = NOW()
            """,
            (
                ev["conversation_id"],
                ev["contact_id"],
                ev["contact_name"],
                ev["contact_phone"],
                ev["assignee_id"] or 0,
                ev["assignee_name"],
                ev["timestamp"],
            ),
        )
    conn.commit()
    log.info(f"Handoff registrado — conv {ev['conversation_id']} → {ev['assignee_name']}")


def handle_capturar(ev: dict, conn) -> None:
    """Salva mensagem pós-handoff (texto ou áudio transcrito)."""
    content = ev["message_content"]
    tipo = "texto"

    if ev["has_audio"] and ev["audio_url"]:
        try:
            content = transcribe_audio(ev["audio_url"])
            tipo = "audio_transcrito"
            log.info(f"Áudio transcrito — conv {ev['conversation_id']}: {content[:60]}...")
        except Exception as e:
            log.warning(f"Falha ao transcrever áudio: {e}")
            content = "[Erro ao transcrever áudio]"
            tipo = "audio"

    sender = ev["message_type"]  # "cliente" ou "vendedor"

    with conn.cursor() as cur:
        # Insere mensagem
        cur.execute(
            """
            WITH conv AS (
                SELECT id FROM conversascore.conversas
                WHERE chatwoot_conversation_id = %s LIMIT 1
            )
            INSERT INTO conversascore.mensagens (
                conversa_id, chatwoot_message_id, remetente, tipo,
                conteudo, audio_url, is_mensagem_bot, enviada_at
            )
            SELECT conv.id, %s, %s, %s, %s, %s, false, %s
            FROM conv
            WHERE EXISTS (SELECT 1 FROM conv)
            """,
            (
                ev["conversation_id"],
                ev["message_id"],
                sender,
                tipo,
                content,
                ev["audio_url"],
                ev["timestamp"],
            ),
        )

        # Atualiza contadores na conversa
        counter_col = "total_mensagens_cliente" if sender == "cliente" else "total_mensagens_vendedor"
        audio_inc = ", total_audios_transcritos = total_audios_transcritos + 1" if tipo == "audio_transcrito" else ""
        cur.execute(
            f"""
            UPDATE conversascore.conversas SET
                {counter_col} = {counter_col} + 1
                {audio_inc},
                ultima_mensagem_at = %s,
                primeira_resposta_at = CASE
                    WHEN primeira_resposta_at IS NULL AND %s = 'vendedor'
                    THEN %s ELSE primeira_resposta_at
                END
            WHERE chatwoot_conversation_id = %s
            """,
            (ev["timestamp"], sender, ev["timestamp"], ev["conversation_id"]),
        )

    conn.commit()
    log.info(f"Mensagem salva — conv {ev['conversation_id']} [{sender}] tipo={tipo}")


def handle_finalizar(ev: dict, conn) -> None:
    """Marca a conversa como finalizada para score posterior."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE conversascore.conversas SET
                status = 'finalizada',
                finalizada_at = NOW()
            WHERE chatwoot_conversation_id = %s AND status = 'aberta'
            """,
            (ev["conversation_id"],),
        )
    conn.commit()
    log.info(f"Conversa finalizada — conv {ev['conversation_id']}")


def process_event(body: dict) -> dict:
    ev = classify_event(body)
    action = ev["action"]

    if action == "ignorar":
        log.info(f"Ignorado: {ev['motivo']} | conv {ev.get('conversation_id')}")
        return {"ok": True, "action": "ignorar"}

    conn = get_connection(cursor_factory=None)  # psycopg2 default cursor
    try:
        if action == "handoff":
            handle_handoff(ev, conn)
        elif action == "capturar":
            handle_capturar(ev, conn)
        elif action == "finalizar":
            handle_finalizar(ev, conn)
    finally:
        conn.close()

    return {"ok": True, "action": action}


# ── Endpoints ──────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "service": "nightbus-webhook"}


@app.post("/conversascore-webhook")
def webhook(request: Request, body: Dict[str, Any]):
    try:
        result = process_event(body)
        return JSONResponse(content=result)
    except psycopg2.Error as e:
        log.error(f"Erro PostgreSQL: {e}")
        send_alert("webhook.py", e, f"conv_id={body.get('conversation', {}).get('id')}")
        raise HTTPException(status_code=500, detail="Erro de banco de dados")
    except Exception as e:
        log.error(f"Erro inesperado: {e}", exc_info=True)
        send_alert("webhook.py", e, f"event={body.get('event')}")
        raise HTTPException(status_code=500, detail=str(e))
