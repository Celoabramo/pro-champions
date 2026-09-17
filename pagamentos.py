"""Cliente bem fino pra API da AbacatePay (Pix).

Documentação: https://docs.abacatepay.com

A chave de API vem da variável de ambiente ABACATEPAY_API_KEY — nunca
fica escrita no código. Sem ela, qualquer chamada aqui levanta
PagamentoIndisponivel (o chamador decide como avisar o usuário).
"""

import os
import re

import requests

BASE_URL = "https://api.abacatepay.com/v2"
TIMEOUT_SEGUNDOS = 15


class PagamentoIndisponivel(Exception):
    """A API de pagamento não está configurada ou recusou a chamada."""


def _headers():
    api_key = os.environ.get("ABACATEPAY_API_KEY")
    if not api_key:
        raise PagamentoIndisponivel(
            "ABACATEPAY_API_KEY não configurada. Defina essa variável de ambiente "
            "com a chave da sua conta AbacatePay."
        )
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _chamar(metodo, caminho, **kwargs):
    try:
        resposta = requests.request(metodo, f"{BASE_URL}{caminho}", headers=_headers(), timeout=TIMEOUT_SEGUNDOS, **kwargs)
    except requests.RequestException as erro:
        raise PagamentoIndisponivel(f"Falha ao falar com a AbacatePay: {erro}") from erro

    try:
        corpo = resposta.json()
    except ValueError:
        raise PagamentoIndisponivel("Resposta inesperada da AbacatePay.")

    if not resposta.ok or not corpo.get("success", False):
        mensagem = corpo.get("error") or f"Erro HTTP {resposta.status_code} na AbacatePay."
        raise PagamentoIndisponivel(mensagem)

    return corpo["data"]


def criar_pix(valor_reais, descricao, external_id=None, expira_em_segundos=1800):
    """Cria um PIX via Checkout Transparente e devolve os dados do QR Code.

    `brCodeBase64` já vem como data URI completa (`data:image/png;base64,...`),
    pronta pra usar direto num <img src="...">.
    """
    dados = {
        "amount": round(valor_reais * 100),
        "description": descricao,
        "expiresIn": expira_em_segundos,
    }
    if external_id:
        dados["metadata"] = {"externalId": external_id}

    return _chamar("POST", "/transparents/create", json={"method": "PIX", "data": dados})


def checar_pix(pix_id):
    """Consulta o status atual de um PIX (PENDING, PAID, EXPIRED...)."""
    return _chamar("GET", "/transparents/check", params={"id": pix_id})


def simular_pagamento(pix_id):
    """Só funciona em modo sandbox/devMode na AbacatePay — útil pra testar o fluxo local."""
    return _chamar("POST", "/transparents/simulate-payment", params={"id": pix_id})


TIPOS_CHAVE_PIX = {
    "CPF": "CPF",
    "CNPJ": "CNPJ",
    "PHONE": "Telefone",
    "EMAIL": "E-mail",
    "RANDOM": "Aleatória",
}


def _formatar_chave_pix(chave, tipo_chave):
    """Ajusta a chave pro formato que o DICT (diretório do Pix, do Banco
    Central) realmente usa antes de mandar pra AbacatePay.

    Achado em produção: chave de telefone salva como só os 11 dígitos
    (ex: "11947010769") é uma chave de verdade — o banco do dono resolve
    ela certinho — mas a AbacatePay recusava com "Chave Pix inválida para
    tipo PHONE". O motivo é que o DICT guarda chave de telefone no formato
    internacional (+55DDNÚMERO), não em dígitos crus; a maioria dos bancos
    aceita digitar só os números e completa o +55 por trás, mas a API não
    faz essa complementação sozinha. Só telefone precisa disso — CPF, CNPJ,
    e-mail e chave aleatória já vão do jeito que a pessoa cadastrou.
    """
    if tipo_chave != "PHONE":
        return chave
    digitos = re.sub(r"\D", "", chave)
    # 10 dígitos (DDD + fixo) ou 11 (DDD + celular) é chave sem o país —
    # completa com 55. Já vindo com 12/13 dígitos (alguém colou com o 55
    # na frente) não mexe, pra não grudar um 55 duplicado.
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    return f"+{digitos}"


def enviar_pix(valor_reais, chave_pix, tipo_chave, descricao, external_id):
    """Envia um Pix pra uma chave de terceiro (ex: prêmio do campeão).

    Endpoint real é POST /pix/send (a página de docs chama de "create",
    mas o caminho da API é esse). Valor mínimo da AbacatePay é R$ 1,00.
    """
    if tipo_chave not in TIPOS_CHAVE_PIX:
        raise PagamentoIndisponivel(f"Tipo de chave Pix inválido: {tipo_chave}.")

    corpo = {
        "amount": round(valor_reais * 100),
        "externalId": external_id,
        "description": descricao,
        "pix": {"key": _formatar_chave_pix(chave_pix, tipo_chave), "type": tipo_chave},
    }
    return _chamar("POST", "/pix/send", json=corpo)
