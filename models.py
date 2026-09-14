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


class User(UserMixin, db.Model):
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
        """Só os times com inscrição paga — Pix pendente não conta vaga.

        `i.time` pode vir None se o time foi dissolvido depois de pago
        (capitão saiu e era o último membro) — filtra esses casos.
        """
        return [i.time for i in self.inscricoes if i.status == "pago" and i.time is not None]

    def __repr__(self):
        return f"<Torneio {self.nome}>"


class Team(db.Model):
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
