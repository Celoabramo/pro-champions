"""Leitura automática do placar de um print via IA (Claude), usando a
biblioteca oficial "anthropic".

A chave vem de ANTHROPIC_API_KEY (variável de ambiente, lida do .env) —
nunca fica escrita no código.
"""

import base64
import json
import os

import anthropic

MODELO = "claude-haiku-4-5"
TIMEOUT_SEGUNDOS = 30

MEDIA_TYPE_POR_EXTENSAO = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

PROMPT = """Essa imagem é um print de tela de fim de partida de um jogo de futebol (FIFA/EA FC Pro Clubs).

Leia o placar final e os nomes dos dois times na imagem. Responda APENAS com um JSON,
sem nenhum texto antes ou depois e sem usar bloco de código, no formato exato:

{"time_casa": "...", "time_visitante": "...", "placar_casa": N, "placar_visitante": N}

Se a imagem não for um print de placar de partida, ou não der pra ler o placar com
confiança, responda apenas:

{"erro": "imagem_invalida"}"""


class LeituraIndisponivel(Exception):
    """A chamada pra IA falhou, ou a resposta não veio num JSON válido."""


def ler_placar(caminho_imagem):
    """Manda o print pra Claude e devolve o dict já decodificado do JSON.

    Pode devolver {"erro": "imagem_invalida"} — quem chamar decide o que
    fazer com isso. Falhas de rede/timeout/JSON inválido viram
    LeituraIndisponivel, nunca deixam a exceção crua escapar.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LeituraIndisponivel(
            "ANTHROPIC_API_KEY não configurada. Defina essa variável de ambiente "
            "com a chave da sua conta Anthropic."
        )

    extensao = caminho_imagem.rsplit(".", 1)[-1].lower()
    media_type = MEDIA_TYPE_POR_EXTENSAO.get(extensao, "image/png")

    with open(caminho_imagem, "rb") as arquivo:
        imagem_base64 = base64.b64encode(arquivo.read()).decode()

    cliente = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SEGUNDOS)

    try:
        resposta = cliente.messages.create(
            model=MODELO,
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": imagem_base64},
                        },
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        )
    except anthropic.APITimeoutError as erro:
        raise LeituraIndisponivel("A IA demorou demais pra responder. Tenta de novo.") from erro
    except anthropic.APIConnectionError as erro:
        raise LeituraIndisponivel("Não deu pra conectar com a IA agora. Tenta de novo.") from erro
    except anthropic.APIError as erro:
        raise LeituraIndisponivel(f"A IA recusou a chamada: {erro}") from erro

    texto = resposta.content[0].text.strip()
    texto = texto.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        dados = json.loads(texto)
    except ValueError as erro:
        raise LeituraIndisponivel("A IA não devolveu uma leitura válida. Tenta de novo.") from erro

    return dados
