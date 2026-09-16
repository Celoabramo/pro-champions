"""Envio de e-mail transacional via Resend (API HTTPS).

Trocado de SMTP pra API HTTP porque a Railway bloqueia conexão SMTP de
saída nos planos Free/Trial/Hobby (só libera a partir do plano Pro) —
a própria Railway recomenda um serviço de e-mail transacional via API
nesse caso (https://docs.railway.com/networking/outbound-networking).

Credenciais vêm de variáveis de ambiente — nunca ficam no código:
RESEND_API_KEY (obrigatória) e EMAIL_REMETENTE (endereço de envio,
precisa ser de um domínio verificado na conta Resend — resend.com/domains).
"""

import os

import requests

TIMEOUT_SEGUNDOS = 15


class EmailIndisponivel(Exception):
    """Resend não está configurado ou recusou o envio."""


def _configurado():
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("EMAIL_REMETENTE"))


def enviar_email(destinatario, assunto, corpo_texto):
    if not _configurado():
        raise EmailIndisponivel(
            "Envio de e-mail não configurado. Defina RESEND_API_KEY e EMAIL_REMETENTE no .env."
        )

    api_key = os.environ["RESEND_API_KEY"]
    remetente = os.environ["EMAIL_REMETENTE"]

    try:
        resposta = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": remetente,
                "to": destinatario,
                "subject": assunto,
                "text": corpo_texto,
            },
            timeout=TIMEOUT_SEGUNDOS,
        )
    except requests.RequestException as erro:
        raise EmailIndisponivel(f"Falha ao falar com o Resend: {erro}") from erro

    if not resposta.ok:
        try:
            detalhe = resposta.json().get("message", resposta.text)
        except ValueError:
            detalhe = resposta.text
        raise EmailIndisponivel(f"Resend recusou o envio: {detalhe}")
