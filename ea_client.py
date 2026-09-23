"""Cliente da API não-oficial de Pro Clubs da EA (proclubs.ea.com).

É a mesma API que o site oficial de Pro Clubs usa por baixo. Não tem
contrato, não tem documentação e pode mudar sem aviso — por isso este
módulo é isolado do resto do site: quem chama trata a exceção e cai no
fluxo de print, que continua sendo o caminho garantido.

O que foi confirmado contra a API de verdade (23/09/2026):

* Sem cabeçalho de navegador a EA recusa. Com os cabeçalhos daqui, responde.
* Ela BLOQUEIA por excesso de chamadas ("Access Denied" em HTML, não JSON).
  Aconteceu depois de algumas dezenas de requisições seguidas. Por isso o
  intervalo mínimo entre chamadas aqui embaixo.
* Plataformas aceitas: common-gen5, ps5, xbox-series-xs, pc, nx.
  "common-gen4"/"ps4" dão HTTP 400 — a geração antiga não existe nessa API.
* A busca por nome casa por PREFIXO, sem diferenciar maiúscula: "SALZ" acha
  "Salzburg", mas "urgo" não acha "Salzburgo".
* O placar que vale é clubs[id]["goals"]. A soma dos gols dos jogadores pode
  dar menos (gol contra, jogador que saiu no meio) — visto numa partida real
  de 4 a 0 em que os jogadores somavam 2.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://proclubs.ea.com/api/fc"

# A EA responde 403/"Access Denied" pra requisição sem cara de navegador.
CABECALHOS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.ea.com/",
    "Origin": "https://www.ea.com",
}

TIMEOUT = 15

# Plataformas que a API aceita no parâmetro "platform". O valor é o rótulo
# que aparece pro capitão escolher. A geração antiga (PS4/Xbox One) ficou de
# fora porque a API recusa: "Invalid query parameter: platform".
PLATAFORMAS = {
    "common-gen5": "PS5 / Xbox Series",
    "pc": "PC",
    "nx": "Nintendo Switch",
}
PLATAFORMA_PADRAO = "common-gen5"

# Intervalo mínimo entre duas chamadas à EA, do processo inteiro. Não é
# educação: é o que evita o bloqueio que já tomamos testando.
INTERVALO_MINIMO = 1.5
_trava = threading.Lock()
_ultima_chamada = 0.0


class EaIndisponivel(Exception):
    """A EA não respondeu o que a gente esperava — quem chama cai no print."""


class EaBloqueou(EaIndisponivel):
    """A EA recusou a requisição (403 / "Access Denied").

    Separada porque pede reação diferente: não adianta tentar de novo já,
    tem que espaçar as chamadas.
    """


def _esperar_a_vez():
    """Segura a chamada até passar INTERVALO_MINIMO desde a anterior."""
    global _ultima_chamada
    with _trava:
        espera = INTERVALO_MINIMO - (time.monotonic() - _ultima_chamada)
        if espera > 0:
            time.sleep(espera)
        _ultima_chamada = time.monotonic()


def _pegar(caminho, parametros):
    """GET no endpoint, devolve o JSON já decodificado.

    Levanta EaBloqueou/EaIndisponivel em qualquer resposta que não seja um
    JSON válido — inclusive a página HTML de "Access Denied", que vem com
    status 200 em alguns casos e enganaria um `resposta.ok`.
    """
    url = "%s/%s?%s" % (BASE, caminho, urllib.parse.urlencode(parametros))
    _esperar_a_vez()

    requisicao = urllib.request.Request(url, headers=CABECALHOS)
    try:
        with urllib.request.urlopen(requisicao, timeout=TIMEOUT) as resposta:
            bruto = resposta.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as erro:
        corpo = ""
        try:
            corpo = erro.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if erro.code in (401, 403, 429):
            raise EaBloqueou(f"a EA recusou a consulta (HTTP {erro.code})") from erro
        raise EaIndisponivel(f"a EA respondeu HTTP {erro.code}: {corpo}") from erro
    except urllib.error.URLError as erro:
        raise EaIndisponivel(f"não deu para falar com a EA: {erro.reason}") from erro
    except Exception as erro:  # timeout de socket, DNS, TLS
        raise EaIndisponivel(f"não deu para falar com a EA: {erro}") from erro

    if "Access Denied" in bruto[:400]:
        raise EaBloqueou("a EA bloqueou a consulta por excesso de chamadas")

    try:
        return json.loads(bruto)
    except ValueError as erro:
        raise EaIndisponivel("a EA devolveu uma resposta que não é JSON") from erro


def buscar_partidas(ea_club_id, ea_platform, max_result_count=5, tipo="friendlyMatch"):
    """Últimas partidas do clube, da mais nova pra mais velha.

    A ordem é feita aqui na mão, pelo timestamp: nas consultas de teste a API
    até devolveu ordenado, mas não é promessa dela — e a fase de validação
    depende de "a mais recente" estar certa.

    `tipo` é o matchType da EA: friendlyMatch (amistoso marcado entre dois
    clubes, que é o caso dos campeonatos daqui), leagueMatch (divisões,
    adversário sorteado) ou playoffMatch.
    """
    dados = _pegar("clubs/matches", {
        "platform": ea_platform,
        "clubIds": ea_club_id,
        "matchType": tipo,
        "maxResultCount": max_result_count,
    })
    if not isinstance(dados, list):
        raise EaIndisponivel("a EA devolveu partidas num formato inesperado")

    def quando(partida):
        try:
            return int(partida.get("timestamp") or 0)
        except (TypeError, ValueError):
            return 0

    return sorted(dados, key=quando, reverse=True)


def buscar_clubes_por_nome(nome, ea_platform=PLATAFORMA_PADRAO):
    """Clubes cujo nome COMEÇA com `nome` (a EA casa por prefixo).

    Devolve uma lista de dicionários simples — só o que a tela precisa pro
    capitão reconhecer o clube dele. O nome no jogo costuma ter detalhe que
    ninguém lembra na hora de digitar (o clube de teste se chama
    "RB  SALZBURG", com dois espaços), então quem procura acerta mais
    digitando um pedaço curto.
    """
    nome = (nome or "").strip()
    if not nome:
        return []

    dados = _pegar("allTimeLeaderboard/search", {
        "platform": ea_platform,
        "clubName": nome,
    })
    if not isinstance(dados, list):
        raise EaIndisponivel("a EA devolveu a busca num formato inesperado")

    clubes = []
    for item in dados:
        info = item.get("clubInfo") or {}
        club_id = info.get("clubId") or item.get("clubId")
        if not club_id:
            continue
        clubes.append({
            "club_id": int(club_id),
            "nome": info.get("name") or "",
            "jogos": _inteiro(item.get("gamesPlayed")),
            "vitorias": _inteiro(item.get("wins")),
            "derrotas": _inteiro(item.get("losses")),
            "empates": _inteiro(item.get("ties")),
        })
    return clubes


def membros_do_clube(ea_club_id, ea_platform=PLATAFORMA_PADRAO):
    """Nomes de quem está no elenco do clube dentro do jogo.

    É o jeito de o capitão ter certeza de que escolheu o clube certo quando
    a busca devolve vários com nome parecido — foi assim que a gente achou o
    clube de teste no meio de doze "RB alguma coisa".
    """
    dados = _pegar("members/stats", {"platform": ea_platform, "clubId": ea_club_id})
    if not isinstance(dados, dict):
        raise EaIndisponivel("a EA devolveu os membros num formato inesperado")
    return [m.get("name") or "" for m in dados.get("members", [])]


def _inteiro(valor, padrao=0):
    """A EA manda quase tudo como string ('4', '8.70'); aqui vira número."""
    try:
        return int(valor)
    except (TypeError, ValueError):
        return padrao


def gols_por_clube(partida):
    """{club_id (str): gols (int)} de uma partida.

    Usa clubs[id]["goals"], que é o placar oficial. Somar os gols dos
    jogadores dá errado: numa partida real de 4 a 0 os jogadores listados
    somavam 2 (gol contra e gente que saiu no meio não aparecem).
    """
    return {
        str(cid): _inteiro(dados.get("goals"))
        for cid, dados in (partida.get("clubs") or {}).items()
    }


def jogadores_da_partida(partida):
    """Lista achatada das estatísticas por jogador, dos dois clubes.

    Cada item traz o essencial já convertido: o resto do payload da EA
    (dezenas de campos) fica de fora de propósito.
    """
    saida = []
    for club_id, jogadores in (partida.get("players") or {}).items():
        for player_id, dados in (jogadores or {}).items():
            saida.append({
                "club_id": str(club_id),
                "player_id": str(player_id),
                "nome": dados.get("playername") or "",
                "posicao": dados.get("pos") or "",
                "gols": _inteiro(dados.get("goals")),
                "assistencias": _inteiro(dados.get("assists")),
                "rating": _decimal(dados.get("rating")),
                "defesas": _inteiro(dados.get("saves")),
                # A própria EA marca o melhor da partida — não precisa
                # calcular pelo maior rating.
                "melhor_da_partida": _inteiro(dados.get("mom")) == 1,
            })
    return saida


def _decimal(valor, padrao=None):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return padrao
