"""Envio de e-mail transacional via SMTP (hoje, só pra redefinição de senha).

Credenciais vêm de variáveis de ambiente — nunca ficam no código:
EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, EMAIL_SMTP_USER, EMAIL_SMTP_SENHA,
e opcionalmente EMAIL_REMETENTE (usa EMAIL_SMTP_USER se não tiver).

Funciona com qualquer provedor SMTP comum — Gmail (com uma "senha de
app", não a senha normal da conta), Outlook, ou um provedor transacional
tipo SendGrid/Mailgun/Resend usando a porta SMTP deles.
"""

import os
import smtplib
from email.mime.text import MIMEText


class EmailIndisponivel(Exception):
    """SMTP não configurado ou recusou o envio."""


def _configurado():
    return bool(
        os.environ.get("EMAIL_SMTP_HOST")
        and os.environ.get("EMAIL_SMTP_USER")
        and os.environ.get("EMAIL_SMTP_SENHA")
    )


def enviar_email(destinatario, assunto, corpo_texto):
    if not _configurado():
        raise EmailIndisponivel(
            "Envio de e-mail não configurado. Defina EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, "
            "EMAIL_SMTP_USER e EMAIL_SMTP_SENHA no .env."
        )

    host = os.environ["EMAIL_SMTP_HOST"]
    porta = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
    usuario = os.environ["EMAIL_SMTP_USER"]
    senha = os.environ["EMAIL_SMTP_SENHA"]
    remetente = os.environ.get("EMAIL_REMETENTE", usuario)

    msg = MIMEText(corpo_texto, "plain", "utf-8")
    msg["Subject"] = assunto
    msg["From"] = remetente
    msg["To"] = destinatario

    try:
        with smtplib.SMTP(host, porta, timeout=15) as servidor:
            servidor.starttls()
            servidor.login(usuario, senha)
            servidor.sendmail(remetente, [destinatario], msg.as_string())
    except (smtplib.SMTPException, OSError) as erro:
        raise EmailIndisponivel(f"Falha ao enviar e-mail: {erro}") from erro
