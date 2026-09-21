"""Notificação push no navegador (Web Push, com chaves VAPID).

As chaves vêm de VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY no ambiente. Sem elas,
`configurado()` é False: o perfil esconde o botão de ativar e nenhum envio é
tentado — o resto do site segue normal.
"""

import json
import logging
import os

from pywebpush import WebPushException, webpush

from models import InscricaoPush, db

log = logging.getLogger(__name__)

# Quem o serviço de push (Google/Mozilla/Apple) pode contatar sobre esse
# remetente, exigido pelo padrão VAPID.
CONTATO_VAPID = "mailto:suporte@prochampions.com.br"

# Respostas do serviço de push que significam "essa subscription morreu"
# (navegador desinstalado, permissão revogada, endpoint expirado). Nesses
# casos a linha é apagada em vez de tentar de novo pra sempre.
STATUS_SUBSCRIPTION_MORTA = {404, 410}


def chave_publica():
    return os.environ.get("VAPID_PUBLIC_KEY", "").strip()


def configurado():
    return bool(chave_publica() and os.environ.get("VAPID_PRIVATE_KEY", "").strip())


def enviar_para_todos(titulo, corpo, url):
    """Manda o mesmo push pra cada navegador cadastrado.

    Falha em um não derruba os outros: subscription morta é apagada, erro de
    rede/serviço só vai pro log. Devolve (enviados, removidos).
    """
    if not configurado():
        return 0, 0

    payload = json.dumps({"titulo": titulo, "corpo": corpo, "url": url})
    privada = os.environ["VAPID_PRIVATE_KEY"].strip()
    enviados = removidos = 0

    for inscricao in InscricaoPush.query.all():
        try:
            webpush(
                subscription_info=inscricao.como_subscription(),
                data=payload,
                vapid_private_key=privada,
                vapid_claims={"sub": CONTATO_VAPID},
                ttl=60 * 60 * 24,
            )
            enviados += 1
        except WebPushException as erro:
            status = getattr(erro.response, "status_code", None)
            if status in STATUS_SUBSCRIPTION_MORTA:
                db.session.delete(inscricao)
                removidos += 1
            else:
                log.warning(f"Push falhou (inscrição {inscricao.id}, status {status}): {erro}")
        except Exception as erro:  # rede, DNS, etc. — nunca derruba o lote
            log.warning(f"Push falhou (inscrição {inscricao.id}): {erro}")

    if removidos:
        db.session.commit()
    return enviados, removidos
