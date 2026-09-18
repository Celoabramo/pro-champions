"""
Modelos do banco de dados.

Cada classe aqui vira uma tabela no banco. Por enquanto só temos o
User (jogador). Nas próximas partes vamos adicionar Team, Tournament
e Match aqui também.
"""

import secrets
import string
from datetime import datetime, timedelta

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


def gerar_codigo_convite():
    """Gera um código curto (ex: 7F2K9A) pra outros jogadores entrarem no time."""
    alfabeto = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alfabeto) for _ in range(6))


# Jogos em que um campeonato pode ser disputado. A chave é o que vai pro
# banco (Torneio.jogo); o valor é o nome que aparece na tela. Quando sair
# uma edição nova é só adicionar uma linha aqui — todo lugar que mostra ou
# filtra por jogo lê deste dicionário.
JOGOS = {
    "fc26": "FC 26",
    "fc27": "FC 27",
}
JOGO_PADRAO = "fc26"


# Posições e esquemas táticos (ver PLANO_ESCALACAO_POSICOES.md). Códigos
# iguais aos do FC 26; rótulo curto e nome em português pra tela. Cada
# esquema é uma lista de linhas, da defesa pro ataque (o goleiro é
# implícito e fica sempre na linha de trás). Um slot é "CÓDIGO+índice"
# (ex.: "CB1", "CB2") — é o que fica gravado em User.posicao_escalada.
POSICOES = {
    "GK": ("GOL", "Goleiro"),
    "CB": ("ZAG", "Zagueiro"),
    "LB": ("LE", "Lateral esquerdo"),
    "RB": ("LD", "Lateral direito"),
    "LWB": ("ALE", "Ala esquerdo"),
    "RWB": ("ALD", "Ala direito"),
    "CDM": ("VOL", "Volante"),
    "CM": ("MC", "Meio-campo"),
    "CAM": ("MEI", "Meia atacante"),
    "LM": ("ME", "Meia esquerda"),
    "RM": ("MD", "Meia direita"),
    "LW": ("PE", "Ponta esquerda"),
    "RW": ("PD", "Ponta direita"),
    "CF": ("SA", "Segundo atacante"),
    "ST": ("ATA", "Atacante"),
}

FORMACOES = {
    "3-3-4": [["CB", "CB", "CB"], ["CM", "CM", "CM"], ["LW", "ST", "ST", "RW"]],
    "3-4-3": [["CB", "CB", "CB"], ["LM", "CM", "CM", "RM"], ["LW", "ST", "RW"]],
    "3-5-2": [["CB", "CB", "CB"], ["LM", "CDM", "CDM", "RM"], ["CAM"], ["ST", "ST"]],
    "4-1-2-1-2": [["LB", "CB", "CB", "RB"], ["CDM"], ["LM", "RM"], ["CAM"], ["ST", "ST"]],
    "4-1-4-1": [["LB", "CB", "CB", "RB"], ["CDM"], ["LM", "CM", "CM", "RM"], ["ST"]],
    "4-2-2-2": [["LB", "CB", "CB", "RB"], ["CDM", "CDM"], ["CAM", "CAM"], ["ST", "ST"]],
    "4-2-3-1": [["LB", "CB", "CB", "RB"], ["CDM", "CDM"], ["LM", "CAM", "RM"], ["ST"]],
    "4-2-4": [["LB", "CB", "CB", "RB"], ["CM", "CM"], ["LW", "ST", "ST", "RW"]],
    "4-3-2-1": [["LB", "CB", "CB", "RB"], ["CM", "CM", "CM"], ["CF", "CF"], ["ST"]],
    "4-3-3": [["LB", "CB", "CB", "RB"], ["CM", "CM", "CM"], ["LW", "ST", "RW"]],
    "4-4-2": [["LB", "CB", "CB", "RB"], ["LM", "CM", "CM", "RM"], ["ST", "ST"]],
    "4-5-1": [["LB", "CB", "CB", "RB"], ["LM", "CM", "CAM", "CM", "RM"], ["ST"]],
    "5-2-1-2": [["LWB", "CB", "CB", "CB", "RWB"], ["CM", "CM"], ["CAM"], ["ST", "ST"]],
    "5-3-2": [["LWB", "CB", "CB", "CB", "RWB"], ["CM", "CM", "CM"], ["ST", "ST"]],
    "5-4-1": [["LWB", "CB", "CB", "CB", "RWB"], ["LM", "CM", "CM", "RM"], ["ST"]],
}


def slots_da_formacao(formacao):
    """Linhas de slots (da defesa pro ataque, goleiro na primeira linha),
    cada slot como (chave, código). Ex.: [("GK1","GK")], [("LB1","LB"),
    ("CB1","CB"),("CB2","CB"),("RB1","RB")], ... Esquema desconhecido
    (time antigo com valor fora da lista) cai pro 4-4-2."""
    linhas = FORMACOES.get(formacao) or FORMACOES["4-4-2"]
    contador = {}
    resultado = [[("GK1", "GK")]]
    for linha in linhas:
        slots = []
        for codigo in linha:
            contador[codigo] = contador.get(codigo, 0) + 1
            slots.append((f"{codigo}{contador[codigo]}", codigo))
        resultado.append(slots)
    return resultado


def chaves_da_formacao(formacao):
    return [chave for linha in slots_da_formacao(formacao) for chave, _ in linha]


# XP e nível (ver PLANO_XP_NIVEL.md). XP = reais ganhos em prêmio: cada real
# de prêmio vira 1 XP pro time e pra cada jogador do elenco na hora. Só se
# guarda xp_total; o nível é calculado daqui, assim dá pra recalibrar a
# curva sem migração nem recalcular ninguém.
#
# Curva: nível N exige 40 × (N-1)^1.5 XP. Nível 10 ≈ R$1.080 em prêmios,
# 20 ≈ R$3.300, 50 ≈ R$13.700, 100 ≈ R$39.400.
def nivel_por_xp(xp):
    xp = max(int(xp or 0), 0)
    nivel = 1 + int((xp / 40) ** (2 / 3))
    # Ajuste fino nas bordas: a raiz em ponto flutuante pode cair um nível
    # abaixo/acima bem no limiar — confere contra a tabela exata.
    while xp_para_nivel(nivel + 1) <= xp:
        nivel += 1
    while nivel > 1 and xp_para_nivel(nivel) > xp:
        nivel -= 1
    return nivel


# XP por destaque de campeonato (melhor ataque, melhor defesa), além do XP
# por prêmio — ver _creditar_xp em app.py.
XP_DESTAQUE = 10


def xp_para_nivel(nivel):
    """XP acumulado mínimo pra estar no nível dado."""
    if nivel <= 1:
        return 0
    return int(round(40 * (nivel - 1) ** 1.5))


class ComXP:
    """Mixin de quem tem xp_total (User e Team)."""

    @property
    def nivel(self):
        return nivel_por_xp(self.xp_total)

    @property
    def xp_proximo_nivel(self):
        return xp_para_nivel(self.nivel + 1)

    @property
    def faixa_nivel(self):
        """Marco de decoração: None | bronze (10) | prata (20) | ouro (50) |
        lenda (100). Só muda a aparência do selo/escudo."""
        nivel = self.nivel
        if nivel >= 100:
            return "lenda"
        if nivel >= 50:
            return "ouro"
        if nivel >= 20:
            return "prata"
        if nivel >= 10:
            return "bronze"
        return None

    @property
    def progresso_nivel(self):
        """0–100: quanto do caminho entre o nível atual e o próximo já foi."""
        base = xp_para_nivel(self.nivel)
        topo = self.xp_proximo_nivel
        if topo <= base:
            return 100
        return int(min(100, max(0, (int(self.xp_total or 0) - base) * 100 / (topo - base))))


class User(UserMixin, ComXP, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    nickname = db.Column(db.String(40), unique=True, nullable=False)
    senha_hash = db.Column(db.String(255), nullable=False)
    criado_em = db.Column(db.DateTime, server_default=db.func.now())

    # Só quem tem essa flag marcada pode criar campeonato. Por enquanto não
    # tem painel de admin — a promoção é feita direto no banco (ver comando
    # `flask promover-organizador` no app.py). Um painel de verdade fica
    # pra uma próxima parte.
    is_organizador = db.Column(db.Boolean, nullable=False, default=False)

    # Cada jogador pertence a, no máximo, um time por vez
    time_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    time = db.relationship("Team", back_populates="membros", foreign_keys=[time_id])

    # Chave Pix pra receber o prêmio quando o time dele for campeão. A
    # AbacatePay exige saber o tipo da chave (CPF, CNPJ, telefone, e-mail
    # ou aleatória) além do valor — não dá pra adivinhar isso só olhando
    # o formato (CPF e telefone têm o mesmo tamanho, por exemplo).
    chave_pix = db.Column(db.String(140), nullable=True)
    tipo_chave_pix = db.Column(db.String(10), nullable=True)  # CPF | CNPJ | PHONE | EMAIL | RANDOM

    # Token de redefinição de senha ("esqueci minha senha"). Guardamos só o
    # hash (sha256) do token, nunca o valor em si — igual senha, não dá pra
    # reconstruir o token original a partir do que tá no banco. Expira em
    # 30 minutos (reset_token_expira) e é apagado assim que usado uma vez.
    reset_token_hash = db.Column(db.String(64), nullable=True)
    reset_token_expira = db.Column(db.DateTime, nullable=True)

    # Preferências de notificação do organizador. A de campeonato
    # finalizado nunca é silenciada, independente dessas flags — ver
    # criar_notificacao() em app.py.
    notif_campeonatos_silenciadas = db.Column(db.Boolean, nullable=False, default=False)
    notif_partidas_silenciadas = db.Column(db.Boolean, nullable=False, default=False)

    # Federação da qual o usuário é administrador (dono ou admin comum).
    # Um usuário só pode estar numa federação por vez — mesma lógica do
    # time_id acima.
    federacao_id = db.Column(db.Integer, db.ForeignKey("federacoes.id"), nullable=True)
    federacao = db.relationship("Federacao", back_populates="admins", foreign_keys=[federacao_id])

    # Link do grupo de WhatsApp da federação — só faz sentido pra quem é
    # dono de uma (Federacao.dono_id). Mostrado como botão na página
    # pública da federação; se estiver vazio, o botão avisa que não tem
    # grupo cadastrado em vez de sumir. A coluna já existe em produção
    # (migração rodada separadamente antes desse campo voltar ao model).
    link_grupo_whatsapp = db.Column(db.String(255), nullable=True)

    # XP de carreira do jogador — segue a pessoa quando ela troca de time.
    xp_total = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    # Escalação (ver PLANO_ESCALACAO_POSICOES.md). posicao_preferida é o
    # código da posição que o jogador declara gostar (ex.: "ST"), livre e
    # independente de time. posicao_escalada é o slot que ele ocupa no
    # esquema do time atual (ex.: "CB2") — um slot por jogador, um jogador
    # por slot dentro do time; zera quando ele sai do time ou o esquema
    # muda e o slot deixa de existir.
    posicao_preferida = db.Column(db.String(4), nullable=True)
    posicao_escalada = db.Column(db.String(6), nullable=True)

    @property
    def rotulo_posicao_preferida(self):
        return POSICOES.get(self.posicao_preferida, (None, None))[0]

    def __repr__(self):
        return f"<User {self.nickname}>"


class Federacao(db.Model):
    """Organização que agrupa campeonatos e pode ter vários administradores.

    Quem cria a federação vira o dono (só ele pode remover outros admins ou
    excluir a federação). Outros usuários entram como admins comuns usando
    o código de convite — mesmo padrão do código de convite de time.
    """

    __tablename__ = "federacoes"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), unique=True, nullable=False)
    logo = db.Column(db.String(255), nullable=True)
    codigo_convite = db.Column(db.String(6), unique=True, nullable=False, default=gerar_codigo_convite)
    criado_em = db.Column(db.DateTime, server_default=db.func.now())

    dono_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    dono = db.relationship("User", foreign_keys=[dono_id])

    admins = db.relationship("User", back_populates="federacao", foreign_keys=[User.federacao_id])

    # Fatia do lucro de cada campeonato que vai pra federação (o resto fica
    # com a plataforma). Padrão 0.8 (80/20); federação parceira pode ter
    # outro acordo — o admin do site ajusta em /admin/federacoes. Entra na
    # conta do piso de lucro na criação, no ajuste por bye e no repasse.
    fatia_lucro = db.Column(db.Float, nullable=False, default=0.8, server_default="0.8")

    @property
    def fatia_lucro_pct(self):
        return int(round((self.fatia_lucro if self.fatia_lucro is not None else 0.8) * 100))

    @property
    def fatia_plataforma_pct(self):
        return 100 - self.fatia_lucro_pct

    def __repr__(self):
        return f"<Federacao {self.nome}>"


class Grupo(db.Model):
    """Um grupo da fase de grupos (ex: "Grupo A"), com 4 times jogando
    todos contra todos. Só existe em campeonatos com formato "fase_grupos".
    """

    __tablename__ = "grupos"

    id = db.Column(db.Integer, primary_key=True)
    torneio_id = db.Column(db.Integer, db.ForeignKey("torneios.id"), nullable=False)
    nome = db.Column(db.String(20), nullable=False)  # "Grupo A", "Grupo B", ...

    def __repr__(self):
        return f"<Grupo {self.nome} torneio={self.torneio_id}>"


class Inscricao(db.Model):
    """Inscrição de um time num campeonato.

    Enquanto o Pix não cai, status fica "pendente" e o time NÃO aparece
    em `Torneio.times_inscritos` (isso só lista quem já pagou). Campeonato
    gratuito já nasce com status "pago".
    """

    __tablename__ = "inscricoes"

    id = db.Column(db.Integer, primary_key=True)

    torneio_id = db.Column(db.Integer, db.ForeignKey("torneios.id"), nullable=False)
    time_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    torneio = db.relationship("Torneio", back_populates="inscricoes")
    time = db.relationship("Team")

    status = db.Column(db.String(20), nullable=False, default="pendente")  # pendente | pago

    # Só preenchido em campeonatos de formato "fase_grupos" — em qual grupo
    # esse time caiu no sorteio.
    grupo_id = db.Column(db.Integer, db.ForeignKey("grupos.id"), nullable=True)
    grupo = db.relationship("Grupo", foreign_keys=[grupo_id])

    # Dados do PIX transparente gerado na AbacatePay pra essa inscrição.
    pix_id = db.Column(db.String(80), nullable=True)
    pix_br_code = db.Column(db.Text, nullable=True)
    pix_br_code_base64 = db.Column(db.Text, nullable=True)
    pix_expira_em = db.Column(db.DateTime, nullable=True)

    criado_em = db.Column(db.DateTime, server_default=db.func.now())
    pago_em = db.Column(db.DateTime, nullable=True)

    # Reembolso de inscrição paga, quando o time sai/é removido do campeonato.
    # Volta o valor da inscrição menos TAXA_REEMBOLSO (as duas taxas fixas da
    # AbacatePay: a que já foi cobrada pra receber + a que vai ser cobrada pra
    # devolver). reembolso_status: None (nunca precisou) | concluido |
    # pendente (AbacatePay aceitou, ainda confirmando) | falhou | sem_saldo
    # (valor a devolver ficaria abaixo do mínimo de Pix da AbacatePay).
    reembolso_valor = db.Column(db.Float, nullable=True)
    reembolso_transferencia_id = db.Column(db.String(80), nullable=True)
    reembolso_status = db.Column(db.String(20), nullable=True)

    @property
    def foi_reembolsada(self):
        """O dinheiro saiu (ou está saindo) de volta pro time.

        Só "concluido" e "pendente" contam. "falhou" NÃO: a tentativa não
        transferiu nada, então o time continua pago e inscrito — tratar
        "falhou" como reembolsado sumia com um time que pagou de verdade
        (aconteceu em produção: capitão sem chave Pix, reembolso falhou e o
        time desapareceu da lista de inscritos).
        """
        return self.reembolso_status in ("concluido", "pendente")

    __table_args__ = (db.UniqueConstraint("torneio_id", "time_id", name="uq_inscricao_torneio_time"),)

    def __repr__(self):
        return f"<Inscricao time={self.time_id} torneio={self.torneio_id} status={self.status}>"


class Torneio(db.Model):
    __tablename__ = "torneios"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    valor_inscricao = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="aberto")  # aberto | em_andamento | finalizado
    criado_em = db.Column(db.DateTime, server_default=db.func.now())

    # Quantidade de vagas do campeonato (= número de chaves do chaveamento).
    # Precisa ser potência de 2 pra dar pra montar um chaveamento eliminatório.
    max_times = db.Column(db.Integer, nullable=False, default=8)

    # Data em que o campeonato acontece. Usada pra ordenar a listagem em
    # ordem cronológica — nullable só pra não quebrar campeonatos antigos
    # criados antes desse campo existir.
    data_campeonato = db.Column(db.Date, nullable=True)

    # Horário do campeonato — opcional, o organizador pode deixar só a data.
    hora_campeonato = db.Column(db.Time, nullable=True)

    # formato: mata_mata (chaveamento eliminatório direto) | fase_grupos
    # (grupos de 4 jogando todos contra todos, os 2 primeiros de cada grupo
    # avançam pro mata-mata).
    formato = db.Column(db.String(20), nullable=False, default="mata_mata")

    # geracao: antiga (last gen) | nova (next gen) — só informativo, não
    # afeta nenhuma regra do campeonato, é pra jogador saber em qual
    # geração de console o campeonato vai rolar.
    geracao = db.Column(db.String(20), nullable=False, default="nova")

    # jogo: em qual edição do jogo o campeonato é disputado (ver JOGOS). Só
    # informativo, igual a geracao. server_default garante que os campeonatos
    # que já existiam antes dessa coluna fiquem marcados como FC 26 — todos
    # foram criados quando só existia essa edição.
    jogo = db.Column(db.String(10), nullable=False, default=JOGO_PADRAO, server_default=JOGO_PADRAO)

    @property
    def nome_jogo(self):
        return JOGOS.get(self.jogo, self.jogo or JOGOS[JOGO_PADRAO])

    # Ranking por temporada (ver PLANO_TEMPORADA_RANKING.md). temporada_id é
    # preenchido no sorteio do chaveamento com a temporada aberta naquele
    # momento (None se não tiver nenhuma). elegivel_ranking é travado no
    # mesmo instante: só campeonato PAGO e CHEIO (todas as vagas ocupadas,
    # sem ninguém passando de bye) gera ponto — trava contra farmar ponto
    # com campeonato grátis de 2 times. Nunca é recalculado depois.
    temporada_id = db.Column(db.Integer, db.ForeignKey("temporadas.id"), nullable=True)
    temporada = db.relationship("Temporada", foreign_keys=[temporada_id])
    elegivel_ranking = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    # Todo campeonato pertence a uma federação — pode ser administrado por
    # qualquer admin dela, não só por quem criou. `criado_por` só guarda
    # quem apertou o botão, pra exibir "Federação X por Fulano" na tela do
    # campeonato.
    federacao_id = db.Column(db.Integer, db.ForeignKey("federacoes.id"), nullable=False)
    federacao = db.relationship("Federacao", foreign_keys=[federacao_id])

    criado_por_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    criado_por = db.relationship("User", foreign_keys=[criado_por_id])

    # Prêmio pago ao campeão via Pix (AbacatePay) quando o campeonato termina.
    valor_premio = db.Column(db.Float, nullable=False, default=0)
    time_campeao_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    time_campeao = db.relationship("Team", foreign_keys=[time_campeao_id])

    # premio_status: None (ainda não tentou pagar) | pendente | concluido | falhou
    premio_transferencia_id = db.Column(db.String(80), nullable=True)
    premio_status = db.Column(db.String(20), nullable=True)

    # Prêmios opcionais de vice e terceiro, mesmo ciclo do prêmio do
    # campeão (status: None | concluido | pendente | falhou | sem_colocado,
    # esse último quando a colocação não existe — ex.: W.O. duplo na
    # disputa de 3º). Entram na conta do lucro na criação e no repasse.
    valor_premio_vice = db.Column(db.Float, nullable=False, default=0, server_default="0")
    valor_premio_terceiro = db.Column(db.Float, nullable=False, default=0, server_default="0")
    premio_vice_transferencia_id = db.Column(db.String(80), nullable=True)
    premio_vice_status = db.Column(db.String(20), nullable=True)
    premio_terceiro_transferencia_id = db.Column(db.String(80), nullable=True)
    premio_terceiro_status = db.Column(db.String(20), nullable=True)

    # XP creditado no encerramento (idempotente — ver _creditar_xp em app.py).
    xp_creditado = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    @property
    def premios(self):
        """{colocacao: valor} só com prêmios acima de zero."""
        todos = {
            "campeao": self.valor_premio or 0,
            "vice": self.valor_premio_vice or 0,
            "terceiro": self.valor_premio_terceiro or 0,
        }
        return {c: v for c, v in todos.items() if v > 0}

    @property
    def total_premios(self):
        return round(sum(self.premios.values()), 2)

    # Repasse dos 80% do lucro (arrecadação das inscrições menos o prêmio)
    # pro admin dono da federação — os outros 20% ficam parados na conta
    # AbacatePay mesmo, sem transferência nenhuma (é a parte da plataforma).
    # repasse_federacao_status: None (não tentou) | sem_lucro (não tinha o
    # que repassar) | pendente | concluido | falhou
    repasse_federacao_valor = db.Column(db.Float, nullable=True)
    repasse_federacao_transferencia_id = db.Column(db.String(80), nullable=True)
    repasse_federacao_status = db.Column(db.String(20), nullable=True)

    inscricoes = db.relationship("Inscricao", back_populates="torneio")
    confrontos = db.relationship(
        "Confronto", back_populates="torneio", order_by="Confronto.ordem"
    )

    @property
    def times_inscritos(self):
        """Só os times que estão realmente disputando o campeonato.

        Precisa ter pago (Pix pendente não segura vaga) e NÃO ter sido
        reembolsado — quem pagou atrasado, depois das vagas acabarem, tem o
        dinheiro devolvido e não entra na competição; sem esse filtro ele
        continuaria contando como time inscrito e o chaveamento (que exige o
        número exato de times) não fechava.

        `i.time` pode vir None se o time foi dissolvido depois de pago
        (capitão saiu e era o último membro) — filtra esses casos.
        """
        return [
            i.time for i in self.inscricoes
            if i.status == "pago" and i.time is not None and not i.foi_reembolsada
        ]

    def __repr__(self):
        return f"<Torneio {self.nome}>"


class Team(ComXP, db.Model):
    __tablename__ = "teams"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(60), unique=True, nullable=False)
    tag = db.Column(db.String(5), nullable=False)  # sigla curta, ex: "FLA"
    codigo_convite = db.Column(db.String(6), unique=True, nullable=False, default=gerar_codigo_convite)
    formacao = db.Column(db.String(10), nullable=False, default="4-4-2")
    criado_em = db.Column(db.DateTime, server_default=db.func.now())

    # Escudo do time. Sem imagem enviada, a tela usa um escudo genérico
    # de placeholder (ver ESCUDO_GENERICO em app.py).
    escudo_imagem = db.Column(db.String(255), nullable=True)

    # XP de carreira do time — não se transfere quando um jogador sai.
    xp_total = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    # Mercado de transferências: o capitão marca o time como "recrutando"
    # (com um recado curto) e jogadores sem time mandam pedido de entrada
    # pela página pública do time — ver PedidoEntrada.
    recrutando = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    mensagem_recrutamento = db.Column(db.String(160), nullable=True)

    @property
    def escalacao(self):
        """Linhas do esquema com quem ocupa cada slot: [[(chave, código,
        membro_ou_None), ...], ...] da defesa (goleiro) pro ataque."""
        por_slot = {m.posicao_escalada: m for m in self.membros if m.posicao_escalada}
        return [
            [(chave, codigo, por_slot.get(chave)) for chave, codigo in linha]
            for linha in slots_da_formacao(self.formacao)
        ]

    @property
    def slots_vagos(self):
        """[(chave, código)] dos slots do esquema sem ninguém."""
        return [(chave, codigo) for linha in self.escalacao for chave, codigo, membro in linha if membro is None]

    @property
    def posicoes_vagas(self):
        """Rótulos curtos das posições em aberto, sem repetir (ex.: ["LE", "ATA"])."""
        vistos = []
        for _, codigo in self.slots_vagos:
            rotulo = POSICOES[codigo][0]
            if rotulo not in vistos:
                vistos.append(rotulo)
        return vistos

    capitao_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    capitao = db.relationship("User", foreign_keys=[capitao_id])

    membros = db.relationship("User", back_populates="time", foreign_keys=[User.time_id])

    def __repr__(self):
        return f"<Team {self.nome}>"


class Confronto(db.Model):
    """Um jogo dentro do chaveamento de um campeonato.

    Por enquanto só guardamos o confronto da primeira fase (sorteio dos
    times). Avançar vencedor pra fase seguinte fica pra próxima parte.

    status: pendente (esperando print de 1 ou dos 2 times) | divergente
    (os 2 prints chegaram mas a leitura da IA não bateu — precisa reenviar)
    | validado (as duas leituras bateram, placar confirmado) | wo (o
    organizador declarou W.O. — ver vencedor_wo_id)
    """

    __tablename__ = "confrontos"

    id = db.Column(db.Integer, primary_key=True)
    fase = db.Column(db.String(30), nullable=False)  # ex: "Quartas de final"
    ordem = db.Column(db.Integer, nullable=False)  # posição do confronto dentro da fase
    status = db.Column(db.String(25), nullable=False, default="pendente")

    torneio_id = db.Column(db.Integer, db.ForeignKey("torneios.id"), nullable=False)
    torneio = db.relationship("Torneio", back_populates="confrontos")

    time1_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    time2_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    time1 = db.relationship("Team", foreign_keys=[time1_id])
    time2 = db.relationship("Team", foreign_keys=[time2_id])

    # Nome do arquivo do print enviado por cada capitão (fica em static/prints/).
    print_time1 = db.Column(db.String(255), nullable=True)
    print_time2 = db.Column(db.String(255), nullable=True)

    # Nick de cada capitão dentro do FC 26, do jeito exato que aparece no
    # jogo. Serve só pros dois times se acharem lá dentro e combinarem quem
    # manda o convite da partida — o site não usa esse nome pra nada mais.
    # É por confronto (e não fixo no perfil) porque quem hospeda pode mudar
    # de uma partida pra outra.
    nick_fc_time1 = db.Column(db.String(40), nullable=True)
    nick_fc_time2 = db.Column(db.String(40), nullable=True)

    # Disputa de 3º lugar: jogo entre os dois perdedores da semifinal,
    # criado junto com a final. Fica fora da progressão da chave — o
    # resultado dele não alimenta fase nenhuma, só define o 3º colocado
    # (ranking e campeonato com prêmio de 3º). fase fica "Disputa de 3º
    # lugar" pra exibição; o booleano é o que o código consulta.
    eh_disputa_3_lugar = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    # Leitura bruta (JSON) que a IA tirou do print de cada time, guardada
    # até os dois times enviarem pra dar pra comparar. leitura_timeN_ok é
    # None enquanto não chega print desse time, True quando a IA leu um
    # placar válido, False quando a leitura falhou (imagem inválida ou
    # erro na chamada) — nesse caso o time precisa reenviar o print.
    leitura_time1 = db.Column(db.Text, nullable=True)
    leitura_time2 = db.Column(db.Text, nullable=True)
    leitura_time1_ok = db.Column(db.Boolean, nullable=True)
    leitura_time2_ok = db.Column(db.Boolean, nullable=True)

    placar_time1 = db.Column(db.Integer, nullable=True)
    placar_time2 = db.Column(db.Integer, nullable=True)

    vencedor_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    vencedor = db.relationship("Team", foreign_keys=[vencedor_id])

    # Horário em que o organizador marcou a partida como iniciada. É a
    # partir daqui que conta o prazo de 1h pro W.O. automático.
    hora_inicio = db.Column(db.DateTime, nullable=True)

    # Time que venceu por W.O. Fica nulo se o organizador declarou W.O.
    # duplo (nenhum dos dois compareceu) — nesse caso não tem vencedor.
    vencedor_wo_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    vencedor_wo = db.relationship("Team", foreign_keys=[vencedor_wo_id])

    PRAZO_WO = timedelta(hours=1)

    @property
    def eh_bye(self):
        """Confronto sem adversário — o time passa direto de fase por W.O.

        Acontece quando o campeonato começa com menos times do que as vagas:
        a chave é montada no tamanho válido mais próximo (potência de 2) e as
        vagas que sobram ficam vazias. Quem cai numa delas avança sem jogar.
        """
        return self.time1_id is None or self.time2_id is None

    @property
    def nome_time1(self):
        return self.time1.nome if self.time1 else "Sem adversário"

    @property
    def nome_time2(self):
        return self.time2.nome if self.time2 else "Sem adversário"

    @property
    def prazo_wo(self):
        """Horário em que o W.O. automático fica disponível. None se a partida ainda não começou."""
        if not self.hora_inicio:
            return None
        return self.hora_inicio + self.PRAZO_WO

    @property
    def elegivel_wo(self):
        """True se o prazo de 1h já passou e a partida ainda não foi confirmada nem encerrada."""
        prazo = self.prazo_wo
        if not prazo:
            return False
        return datetime.utcnow() > prazo and self.status not in ("validado", "wo")

    def __repr__(self):
        return f"<Confronto {self.fase} #{self.ordem}>"


class Notificacao(db.Model):
    """Aviso pro organizador sobre o andamento dos campeonatos dele.

    tipo: campeonato_iniciado | campeonato_finalizado | partida_iniciada |
    partida_finalizada. campeonato_finalizado nunca é silenciada, mesmo com
    User.notif_campeonatos_silenciadas ligado — ver criar_notificacao() em
    app.py.
    """

    __tablename__ = "notificacoes"

    id = db.Column(db.Integer, primary_key=True)

    usuario_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    usuario = db.relationship("User", foreign_keys=[usuario_id])

    tipo = db.Column(db.String(30), nullable=False)
    mensagem = db.Column(db.String(255), nullable=False)

    torneio_id = db.Column(db.Integer, db.ForeignKey("torneios.id"), nullable=True)
    confronto_id = db.Column(db.Integer, db.ForeignKey("confrontos.id"), nullable=True)
    torneio = db.relationship("Torneio", foreign_keys=[torneio_id])
    confronto = db.relationship("Confronto", foreign_keys=[confronto_id])

    lida = db.Column(db.Boolean, nullable=False, default=False)
    criado_em = db.Column(db.DateTime, server_default=db.func.now())

    def __repr__(self):
        return f"<Notificacao {self.tipo} usuario={self.usuario_id}>"


class Temporada(db.Model):
    """Temporada do ranking geral do site (ver PLANO_TEMPORADA_RANKING.md).

    Só uma fica "aberta" por vez. Quem gera ponto são os campeonatos
    elegíveis que acontecem enquanto ela está aberta. No fechamento o 1º
    lugar é travado em time_campeao_id e o prêmio só sai se a meta de
    times participantes foi batida (premio_pago_status = "sem_meta" caso
    contrário).
    """

    __tablename__ = "temporadas"

    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.Integer, nullable=False, unique=True)
    nome = db.Column(db.String(60), nullable=False)
    data_inicio = db.Column(db.Date, nullable=False)
    data_fim = db.Column(db.Date, nullable=False)
    meta_times = db.Column(db.Integer, nullable=False, default=100)
    valor_premio = db.Column(db.Float, nullable=False, default=1000.0)
    status = db.Column(db.String(20), nullable=False, default="aberta")  # aberta | encerrada
    criado_em = db.Column(db.DateTime, server_default=db.func.now())

    # Mesmo vocabulário do prêmio de campeonato: concluido | pendente |
    # falhou | sem_meta (não bateu a meta, não sai dinheiro).
    premio_pago_status = db.Column(db.String(20), nullable=True)
    premio_transferencia_id = db.Column(db.String(80), nullable=True)
    time_campeao_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=True)
    time_campeao = db.relationship("Team", foreign_keys=[time_campeao_id])

    def __repr__(self):
        return f"<Temporada {self.numero} {self.status}>"


# Pontos por colocação num campeonato elegível. Um registro por time por
# campeonato — dá pra mostrar o extrato exato de cada ponto e o resultado de
# uma temporada fechada nunca muda depois.
PONTOS_POR_COLOCACAO = {"campeao": 3, "vice": 2, "terceiro": 1}


class PontuacaoRanking(db.Model):
    __tablename__ = "pontuacoes_ranking"

    id = db.Column(db.Integer, primary_key=True)
    temporada_id = db.Column(db.Integer, db.ForeignKey("temporadas.id"), nullable=False)
    torneio_id = db.Column(db.Integer, db.ForeignKey("torneios.id"), nullable=False)
    time_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    colocacao = db.Column(db.String(10), nullable=False)  # campeao | vice | terceiro
    pontos = db.Column(db.Integer, nullable=False)
    criado_em = db.Column(db.DateTime, server_default=db.func.now())

    temporada = db.relationship("Temporada", foreign_keys=[temporada_id])
    torneio = db.relationship("Torneio", foreign_keys=[torneio_id])
    time = db.relationship("Team", foreign_keys=[time_id])

    __table_args__ = (db.UniqueConstraint("torneio_id", "time_id", name="uq_pontuacao_torneio_time"),)

    def __repr__(self):
        return f"<PontuacaoRanking t{self.temporada_id} torneio={self.torneio_id} time={self.time_id} {self.colocacao}>"


class PedidoEntrada(db.Model):
    """Pedido de um jogador pra entrar num time que está recrutando.

    status: pendente | aceito | recusado | cancelado. Aceitar usa a mesma
    regra de sempre (1 time por jogador): o jogador precisa continuar sem
    time na hora que o capitão aceita, senão o pedido é recusado sozinho.
    """

    __tablename__ = "pedidos_entrada"

    id = db.Column(db.Integer, primary_key=True)
    time_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    mensagem = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(12), nullable=False, default="pendente")
    criado_em = db.Column(db.DateTime, server_default=db.func.now())
    respondido_em = db.Column(db.DateTime, nullable=True)

    time = db.relationship("Team", foreign_keys=[time_id])
    usuario = db.relationship("User", foreign_keys=[usuario_id])

    def __repr__(self):
        return f"<PedidoEntrada usuario={self.usuario_id} time={self.time_id} {self.status}>"
