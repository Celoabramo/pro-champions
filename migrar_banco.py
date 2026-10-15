"""Copia o banco do Supabase antigo (São Paulo) para o novo (Virgínia).

Os dados vão DIRETO de um banco para o outro: as senhas, e-mails e chaves
Pix dos usuários nunca saem da sua máquina.

COMO USAR
---------
1) Crie um arquivo chamado "migracao.env" nesta pasta, com as duas
   conexões (pegue as duas no painel do Supabase, em
   Project Settings > Database > Connection string > URI, e use sempre a
   do POOLER / "Transaction pooler", nunca a direta):

       ORIGEM=postgresql://...projeto-antigo...
       DESTINO=postgresql://...projeto-novo...

2) Rode:  python migrar_banco.py

   Sem --gravar ele só CONFERE e não escreve nada. Para copiar de verdade:

       python migrar_banco.py --gravar

O script apaga o conteúdo do banco DESTINO antes de copiar, então pode
rodar quantas vezes quiser (ensaio e, depois, a cópia final definitiva).
Ele nunca escreve no banco de ORIGEM.
"""
import os
import sys

import psycopg2
from psycopg2.extras import execute_values

# Ordem de cópia: uma tabela só entra depois daquelas de que ela depende.
# users, teams e federacoes se apontam em círculo (usuário tem time, time
# tem capitão, federação tem dono), então users entra primeiro com essas
# duas colunas vazias e elas são preenchidas no final.
ORDEM = [
    "users", "federacoes", "teams", "temporadas", "torneios", "grupos",
    "inscricoes", "confrontos", "notificacoes", "pontuacoes_ranking",
    "pedidos_entrada", "inscricoes_push", "conquistas",
]
COLUNAS_ADIADAS = {"users": ["time_id", "federacao_id"]}


def ler_conexoes():
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migracao.env")
    if not os.path.exists(caminho):
        sys.exit("Falta o arquivo migracao.env — veja as instruções no topo deste arquivo.")
    valores = {}
    with open(caminho, encoding="utf-8") as arquivo:
        for linha in arquivo:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            valores[chave.strip().upper()] = valor.strip()
    if not valores.get("ORIGEM") or not valores.get("DESTINO"):
        sys.exit("migracao.env precisa ter ORIGEM e DESTINO.")
    if valores["ORIGEM"] == valores["DESTINO"]:
        sys.exit("ORIGEM e DESTINO são a mesma conexão — conferir antes de rodar.")
    return valores["ORIGEM"], valores["DESTINO"]


def colunas_de(con, tabela):
    with con.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (tabela,))
        return [linha[0] for linha in cur.fetchall()]


def contar(con, tabela):
    with con.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{tabela}"')
        return cur.fetchone()[0]


def copiar(origem, destino, tabela, gravar):
    colunas = colunas_de(origem, tabela)
    # Compara o CONJUNTO de colunas, não a ordem: em produção as colunas
    # novas foram sendo acrescentadas no fim ao longo do tempo, enquanto no
    # banco novo elas nascem no meio. A cópia é feita por nome, então a
    # posição não importa — o que não pode é faltar ou sobrar coluna.
    faltando = set(colunas) ^ set(colunas_de(destino, tabela))
    if faltando:
        sys.exit(f"ERRO: a tabela {tabela} tem colunas diferentes nos dois bancos: {sorted(faltando)}")

    adiadas = COLUNAS_ADIADAS.get(tabela, [])
    # ORDER BY id garante que uma linha que aponta para outra da MESMA
    # tabela (o jogo de volta aponta para o de ida) entre depois dela.
    lista_colunas = ", ".join(f'"{c}"' for c in colunas)
    with origem.cursor() as cur:
        cur.execute(f'SELECT {lista_colunas} FROM "{tabela}" ORDER BY 1')
        linhas = cur.fetchall()

    if adiadas:
        indices = [colunas.index(c) for c in adiadas]
        linhas = [tuple(None if i in indices else v for i, v in enumerate(linha)) for linha in linhas]

    if gravar and linhas:
        with destino.cursor() as cur:
            alvo = ", ".join(f'"{c}"' for c in colunas)
            execute_values(cur, f'INSERT INTO "{tabela}" ({alvo}) VALUES %s',
                           linhas, page_size=len(linhas))
    return len(linhas)


def preencher_adiadas(origem, destino, gravar):
    """Agora que times e federações existem, liga cada usuário a eles."""
    with origem.cursor() as cur:
        cur.execute("SELECT id, time_id, federacao_id FROM users ORDER BY id")
        dados = [linha for linha in cur.fetchall() if linha[1] is not None or linha[2] is not None]
    if gravar and dados:
        with destino.cursor() as cur:
            execute_values(cur, """
                UPDATE users AS u SET time_id = v.time_id, federacao_id = v.federacao_id
                FROM (VALUES %s) AS v(id, time_id, federacao_id) WHERE u.id = v.id
            """, dados, template="(%s::int, %s::int, %s::int)", page_size=len(dados))
    return len(dados)


def acertar_contadores(destino, gravar):
    """Deixa o contador de ID de cada tabela acima do maior id já usado —
    sem isso o próximo cadastro tentaria um id que já existe."""
    if not gravar:
        return
    with destino.cursor() as cur:
        for tabela in ORDEM:
            cur.execute(f"""
                SELECT setval(pg_get_serial_sequence('{tabela}', 'id'),
                              COALESCE((SELECT MAX(id) FROM "{tabela}"), 1),
                              (SELECT MAX(id) IS NOT NULL FROM "{tabela}"))
            """)


def main():
    gravar = "--gravar" in sys.argv
    url_origem, url_destino = ler_conexoes()

    origem = psycopg2.connect(url_origem)
    origem.set_session(readonly=True)          # trava: nunca escreve na origem
    destino = psycopg2.connect(url_destino)

    try:
        print("MODO:", "COPIANDO DE VERDADE" if gravar else "só conferindo (use --gravar para copiar)")
        print()

        if gravar:
            with destino.cursor() as cur:
                cur.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in ORDEM) + " RESTART IDENTITY CASCADE")

        print(f"{'tabela':<22}{'origem':>8}{'copiado':>9}")
        print("-" * 39)
        total = 0
        for tabela in ORDEM:
            quantas = copiar(origem, destino, tabela, gravar)
            total += quantas
            print(f"{tabela:<22}{quantas:>8}{quantas if gravar else 0:>9}")

        ligados = preencher_adiadas(origem, destino, gravar)
        acertar_contadores(destino, gravar)

        print("-" * 39)
        print(f"{'TOTAL':<22}{total:>8}")
        print(f"\nusuários religados ao time/federação: {ligados}")

        if not gravar:
            destino.rollback()
            print("\nNada foi escrito (modo conferência). Rode com --gravar pra copiar.")
            return

        # Confere ANTES de confirmar: a contagem enxerga o que acabou de ser
        # inserido nesta mesma transação, então, se algo não bater, nada é
        # gravado e o banco novo continua como estava.
        print("\nCONFERÊNCIA (origem x destino, antes de confirmar):")
        tudo_bate = True
        for tabela in ORDEM:
            a, b = contar(origem, tabela), contar(destino, tabela)
            if a != b:
                tudo_bate = False
            print(f"  {'ok ' if a == b else 'ERRO'} {tabela:<22}{a:>6} x {b:<6}")

        if tudo_bate:
            destino.commit()
            print("\nTUDO BATEU e foi gravado. Pode trocar a DATABASE_URL no Railway.")
        else:
            destino.rollback()
            print("\nDIFERENÇA ENCONTRADA — desfiz tudo, o banco novo não foi alterado. Me avise.")
    finally:
        origem.close()
        destino.close()


if __name__ == "__main__":
    main()
