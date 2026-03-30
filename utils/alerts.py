"""
Módulo de alertas por e-mail.
Envia notificação quando qualquer script falha.
"""

import os
import smtplib
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_alert(script_name: str, error: Exception, context: str = "") -> None:
    """
    Envia e-mail de alerta quando um script falha.

    Args:
        script_name: nome do script que falhou
        error: exceção capturada
        context: informação extra opcional (ex: ID da conversa)
    """
    smtp_from = os.getenv("ALERT_EMAIL_FROM", "alerts@yourcompany.com")
    smtp_pass = os.getenv("ALERT_EMAIL_PASS", "")
    smtp_to = os.getenv("ALERT_EMAIL_TO", "admin@yourcompany.com")

    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    tb = traceback.format_exc()

    subject = f"[BoateBus ERRO] {script_name} — {now}"

    body_lines = [
        f"Script:   {script_name}",
        f"Horário:  {now}",
        f"Erro:     {type(error).__name__}: {error}",
    ]
    if context:
        body_lines.append(f"Contexto: {context}")
    body_lines += ["", "Traceback:", tb]
    body_text = "\n".join(body_lines)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = smtp_to
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_from, smtp_pass)
            server.send_message(msg)
        print(f"[ALERT] E-mail enviado para {smtp_to} — {subject}")
    except Exception as mail_err:
        print(f"[ALERT] Falha ao enviar e-mail de alerta: {mail_err}")
