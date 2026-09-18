"""
Aplicação principal do site de campeonatos de Pro Clubs.

Como rodar:
    1. python3 -m venv venv
    2. source venv/bin/activate  (no Windows: venv\\Scripts\\activate)
    3. pip install -r requirements.txt
    4. python app.py
    5. Abrir http://localhost:5000 no navegador
"""

import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import string
import unicodedata
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from time import sleep as _dormir

import click
from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, flash, abort, jsonify, send_from_directory
from sqlalchemy import case
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from models import (
    db, User, Team, Torneio, Confronto, Inscricao, Notificacao, Federacao, Grupo,
    Temporada, PontuacaoRanking, PONTOS_POR_COLOCACAO, JOGOS, JOGO_PADRAO, XP_DESTAQUE,
    PedidoEntrada, POSICOES, FORMACOES, chaves_da_formacao,
)
import email_util
import pagamentos
import validacao_ia

# Lê variáveis de um arquivo .env na raiz do projeto (se existir) e joga
# elas em os.environ — assim ABACATEPAY_API_KEY etc. nunca ficam escritas
# no código. Esse arquivo NÃO deve ser commitado (veja .env.example).
load_dotenv()

app = Flask(__name__)

# E-mail do dono do site — o único que pode promover outros usuários a
# organizador (só organizadores podem criar federação/campeonato).
ADMIN_EMAIL = "marcelinhopolar@gmail.com"

# Chave secreta usada pelo Flask para proteger sessões de login e os
# tokens CSRF dos formulários. Em produção isso PRECISA vir de uma
# variável de ambiente — o valor fixo abaixo é só um fallback para rodar
# localmente; se for usado no ar, qualquer um que veja o código consegue
# forjar sessão de outro usuário.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "troque-essa-chave-antes-de-colocar-no-ar")

# Trava extra no cookie de sessão (o que mantém você logado): só HTTP (JS
# não consegue ler), só volta para o site em navegação normal (não em links
# vindos de outro site) e, fora do modo debug local, só trafega via
# HTTPS — sem isso, alguém numa rede insegura poderia roubar o cookie e
# se passar por você logado.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_DEBUG") != "1"

# Localmente usa SQLite em arquivo (simples, sem precisar instalar nada).
# Em produção, defina DATABASE_URL no ambiente apontando pra um Postgres —
# o Railway não mantém arquivos entre deploys, um banco em arquivo local
# se perderia a cada atualização do site.
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///pro_clubs.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Prints do placar, escudos de time e logos de federação — arquivos
# enviados pelos usuários, servidos pela rota arquivo_upload() abaixo
# (não pelo static/ do Flask, que é só pro que já vem no código).
#
# PASTA_UPLOADS por padrão é uma pasta local (bom pra rodar na sua
# máquina) — em produção (Railway), aponta via variável de ambiente pra
# dentro de um Volume, que é a única parte do sistema de arquivos que
# sobrevive a um novo deploy. Sem isso, TODO upload (print de resultado,
# escudo de time, logo de federação) some silenciosamente no próximo
# deploy, porque cada deploy sobe um container zerado.
app.config["PASTA_UPLOADS"] = os.environ.get("PASTA_UPLOADS", os.path.join(app.root_path, "uploads"))
app.config["PASTA_PRINTS"] = os.path.join(app.config["PASTA_UPLOADS"], "prints")
app.config["PASTA_ESCUDOS"] = os.path.join(app.config["PASTA_UPLOADS"], "escudos")
app.config["PASTA_FEDERACOES"] = os.path.join(app.config["PASTA_UPLOADS"], "federacoes")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB por upload
EXTENSOES_PRINT_PERMITIDAS = {"png", "jpg", "jpeg", "webp"}
EXTENSOES_IMAGEM_PERMITIDAS = {"png", "jpg", "jpeg", "webp"}
CATEGORIAS_UPLOAD_VALIDAS = {"prints", "escudos", "federacoes"}

os.makedirs(app.config["PASTA_PRINTS"], exist_ok=True)
os.makedirs(app.config["PASTA_ESCUDOS"], exist_ok=True)
os.makedirs(app.config["PASTA_FEDERACOES"], exist_ok=True)


@app.route("/uploads/<categoria>/<path:nome_arquivo>")
def arquivo_upload(categoria, nome_arquivo):
    if categoria not in CATEGORIAS_UPLOAD_VALIDAS:
        abort(404)
    return send_from_directory(os.path.join(app.config["PASTA_UPLOADS"], categoria), nome_arquivo)


def _apagar_upload(categoria, nome_arquivo):
    """Apaga um arquivo enviado, se ele ainda existir.

    O disco do site é um volume de tamanho fixo. Sem apagar o arquivo antigo
    ao substituir (print reenviado, escudo/logo trocados), cada troca deixava
    um arquivo órfão para sempre — e quando o volume enchesse, TODO upload do
    site pararia de funcionar.

    Falha aqui nunca derruba a ação do usuário: se o arquivo já não existe ou
    não dá para apagar, só registra e segue.
    """
    if not nome_arquivo or categoria not in CATEGORIAS_UPLOAD_VALIDAS:
        return

    caminho = os.path.join(app.config["PASTA_UPLOADS"], categoria, os.path.basename(nome_arquivo))
    try:
        os.remove(caminho)
    except FileNotFoundError:
        pass
    except OSError as erro:
        app.logger.warning(f"Não deu para apagar o upload {categoria}/{nome_arquivo}: {erro}")

# Secret usado para assinar/validar o webhook da AbacatePay (HMAC). Também
# vem de variável de ambiente — sem isso o webhook automático não valida,
# mas o botão manual "já paguei, verificar" continua funcionando.
app.config["ABACATEPAY_WEBHOOK_SECRET"] = os.environ.get("ABACATEPAY_WEBHOOK_SECRET", "")

db.init_app(app)

# Protege todo formulário POST contra CSRF (alguém te levar para uma página
# maliciosa que manda uma requisição em nome da sua sessão sem você saber).
# Todo <form method="POST"> dos templates precisa ter {{ csrf_token() }}
# dentro — os que não tiverem são recusados com 400.
csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def erro_csrf(erro):
    flash("Sua sessão expirou ou a página estava aberta há muito tempo. Tenta de novo.", "erro")
    return redirect(request.referrer or url_for("home"))


@app.errorhandler(404)
def erro_404(erro):
    """Página inexistente — mostra uma tela do site com caminho de volta,
    em vez do 404 cru e sem estilo do servidor."""
    return render_template("404.html"), 404


# Limita quantas vezes o mesmo IP pode tentar login, cadastro e código de
# convite por minuto — para ninguém conseguir forçar senha ou código
# tentando várias vezes seguidas. Guarda a contagem em memória (dá conta
# do recado num app de um processo só; se um dia rodar em vários workers,
# precisa trocar por Redis — ver storage_uri).
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per hour"],
    storage_uri="memory://",
)


@app.errorhandler(429)
def erro_limite_taxa(erro):
    flash("Muitas tentativas seguidas. Espera um minuto e tenta de novo.", "erro")
    return redirect(request.referrer or url_for("home"))


# Configuração do sistema de login (quem pode acessar o quê)
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Faça login para acessar essa página."
login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    """O Flask-Login usa essa função para saber quem é o usuário logado."""
    return User.query.get(int(user_id))


def criar_notificacao(usuario, tipo, mensagem, torneio=None, confronto=None):
    """Cria uma notificação para o usuário (hoje, só organizadores recebem).

    Respeita as preferências de silêncio dele — exceto "campeonato_finalizado",
    que é sempre enviada, mesmo com as notificações de campeonato silenciadas.
    """
    if tipo == "campeonato_iniciado" and usuario.notif_campeonatos_silenciadas:
        return
    if tipo in ("partida_iniciada", "partida_finalizada") and usuario.notif_partidas_silenciadas:
        return

    db.session.add(Notificacao(
        usuario_id=usuario.id,
        tipo=tipo,
        mensagem=mensagem,
        torneio_id=torneio.id if torneio else None,
        confronto_id=confronto.id if confronto else None,
    ))


def notificar_federacao(torneio, tipo, mensagem, confronto=None):
    """Manda a notificação para todos os admins da federação dona do campeonato."""
    for admin in torneio.federacao.admins:
        criar_notificacao(admin, tipo, mensagem, torneio=torneio, confronto=confronto)


@app.context_processor
def injetar_posicoes():
    return dict(POSICOES=POSICOES)


@app.context_processor
def injetar_notificacoes_nao_lidas():
    """Deixa a contagem de não lidas disponível em todo template (sino no menu)."""
    if current_user.is_authenticated:
        total = Notificacao.query.filter_by(usuario_id=current_user.id, lida=False).count()
    else:
        total = 0
    return dict(notificacoes_nao_lidas=total)


@app.context_processor
def injetar_static_versionado():
    """static_v(arquivo) gera a URL do arquivo estático com a data de
    modificação dele colada no final (?v=169...). Assim, toda vez que a
    gente altera o CSS/JS, o link muda e o navegador do usuário busca a
    versão nova na hora — sem isso, o navegador podia continuar usando o
    estilo.css antigo guardado em cache e a pessoa achar que a mudança
    não foi aplicada.
    """

    def static_v(nome_arquivo):
        caminho = os.path.join(app.static_folder, nome_arquivo)
        try:
            versao = int(os.path.getmtime(caminho))
        except OSError:
            versao = 0
        return f"{url_for('static', filename=nome_arquivo)}?v={versao}"

    return dict(static_v=static_v)


@app.context_processor
def injetar_ano_atual():
    """Ano exibido no © do rodapé — para não precisar atualizar na mão todo ano."""
    return dict(ano_atual=datetime.utcnow().year)


@app.context_processor
def injetar_eh_admin_site():
    eh_admin = current_user.is_authenticated and current_user.email == ADMIN_EMAIL
    return dict(eh_admin_site=eh_admin)


def _texto_marquee(nomes, minimo=16):
    """Repete a lista de nomes até ter pelo menos `minimo` itens, depois
    duplica esse trecho para virar uma faixa que rola sem fim (ver CSS
    .faixa-marquee) — com poucos times/federações cadastrados, sem isso a
    faixa ficaria curta demais e apareceria um vão em branco no loop."""
    if not nomes:
        return []
    passe = list(nomes)
    while len(passe) < minimo:
        passe += nomes
    return passe + passe


@app.route("/")
def home():
    times = [t.nome for t in Team.query.order_by(Team.criado_em.desc()).limit(30).all()]
    federacoes = [f.nome for f in Federacao.query.order_by(Federacao.criado_em.desc()).limit(30).all()]
    return render_template(
        "home.html",
        nomes_marquee=_texto_marquee(times + federacoes),
    )


@app.route("/termos-de-uso")
def termos_uso():
    return render_template("termos_uso.html")


@app.route("/politica-de-privacidade")
def politica_privacidade():
    return render_template("politica_privacidade.html")


@app.route("/cadastro", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def cadastro():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        nickname = request.form["nickname"].strip()
        senha = request.form["senha"]

        if len(senha) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "erro")
            return redirect(url_for("cadastro"))

        if not request.form.get("aceite_termos"):
            flash("Para criar conta, você precisa aceitar os Termos de uso e a Política de privacidade.", "erro")
            return redirect(url_for("cadastro"))

        # Confere se já existe alguém com esse e-mail ou nick
        if User.query.filter_by(email=email).first():
            flash("Esse e-mail já está cadastrado.", "erro")
            return redirect(url_for("cadastro"))

        if User.query.filter_by(nickname=nickname).first():
            flash("Esse nick já está em uso. Escolha outro.", "erro")
            return redirect(url_for("cadastro"))

        # Nunca guardamos a senha em texto puro — só o "hash" dela
        novo_usuario = User(
            email=email,
            nickname=nickname,
            senha_hash=generate_password_hash(senha),
        )
        db.session.add(novo_usuario)
        db.session.commit()

        login_user(novo_usuario)
        flash("Cadastro feito com sucesso!", "sucesso")
        return redirect(url_for("perfil"))

    return render_template("cadastro.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        senha = request.form["senha"]

        usuario = User.query.filter_by(email=email).first()

        if usuario and check_password_hash(usuario.senha_hash, senha):
            login_user(usuario)
            return redirect(url_for("perfil"))

        flash("E-mail ou senha incorretos.", "erro")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


@app.route("/esqueci-senha", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def esqueci_senha():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        usuario = User.query.filter_by(email=email).first()

        if usuario:
            token = secrets.token_urlsafe(32)
            usuario.reset_token_hash = hashlib.sha256(token.encode()).hexdigest()
            usuario.reset_token_expira = datetime.utcnow() + timedelta(minutes=30)
            db.session.commit()

            link = url_for("redefinir_senha", token=token, _external=True)
            try:
                email_util.enviar_email(
                    usuario.email,
                    "Redefinir sua senha — Pro Champions",
                    f"Oi, {usuario.nickname}!\n\n"
                    "Recebemos um pedido para redefinir sua senha no Pro Champions. "
                    "Clique no link abaixo para escolher uma nova senha (válido por 30 minutos):\n\n"
                    f"{link}\n\n"
                    "Se não foi você quem pediu, pode ignorar esse e-mail — sua senha continua a mesma.",
                )
            except email_util.EmailIndisponivel as erro:
                app.logger.error(f"Falha ao enviar e-mail de redefinição para {usuario.email}: {erro}")

        # Mensagem igual não importa se o e-mail existe ou não — evita que
        # alguém use esse formulário para descobrir quais e-mails têm conta.
        flash("Se esse e-mail estiver cadastrado, você vai receber um link de redefinição em instantes.", "sucesso")
        return redirect(url_for("login"))

    return render_template("esqueci_senha.html")


@app.route("/redefinir-senha/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def redefinir_senha(token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    usuario = User.query.filter_by(reset_token_hash=token_hash).first()

    if not usuario or not usuario.reset_token_expira or usuario.reset_token_expira < datetime.utcnow():
        flash("Esse link expirou ou já foi usado. Peça um novo.", "erro")
        return redirect(url_for("esqueci_senha"))

    if request.method == "POST":
        senha = request.form["senha"]
        if len(senha) < 8:
            flash("A senha precisa ter pelo menos 8 caracteres.", "erro")
            return redirect(url_for("redefinir_senha", token=token))

        usuario.senha_hash = generate_password_hash(senha)
        usuario.reset_token_hash = None
        usuario.reset_token_expira = None
        db.session.commit()

        flash("Senha redefinida com sucesso! Já pode entrar.", "sucesso")
        return redirect(url_for("login"))

    return render_template("redefinir_senha.html", token=token)


@app.route("/perfil")
@login_required
def perfil():
    campeonatos_inscrito = []
    campeonatos_pendentes = []

    if current_user.time:
        inscricoes = Inscricao.query.filter_by(time_id=current_user.time.id).all()
        campeonatos_inscrito = [i.torneio for i in inscricoes if i.status == "pago"]
        campeonatos_pendentes = [i for i in inscricoes if i.status == "pendente"]

    return render_template(
        "perfil.html",
        usuario=current_user,
        campeonatos_inscrito=campeonatos_inscrito,
        campeonatos_pendentes=campeonatos_pendentes,
        tipos_chave_pix=pagamentos.TIPOS_CHAVE_PIX,
    )


@app.route("/perfil/pix", methods=["GET", "POST"])
@login_required
def perfil_pix():
    if request.method == "POST":
        chave = request.form["chave_pix"].strip()
        tipo = request.form["tipo_chave_pix"]

        if not chave:
            flash("Informe a chave Pix.", "erro")
            return redirect(url_for("perfil_pix"))

        if tipo not in pagamentos.TIPOS_CHAVE_PIX:
            flash("Tipo de chave Pix inválido.", "erro")
            return redirect(url_for("perfil_pix"))

        current_user.chave_pix = chave
        current_user.tipo_chave_pix = tipo
        db.session.commit()

        flash("Chave Pix salva! É para ela que o prêmio cai se seu time for campeão.", "sucesso")
        return redirect(url_for("perfil"))

    return render_template("perfil_pix.html", usuario=current_user, tipos=pagamentos.TIPOS_CHAVE_PIX)


@app.route("/admin/organizadores", methods=["GET", "POST"])
@login_required
def admin_organizadores():
    if current_user.email != ADMIN_EMAIL:
        abort(404)

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        usuario = User.query.filter_by(email=email).first()
        if not usuario:
            flash(f"Nenhum usuário encontrado com o e-mail {email}.", "erro")
        elif usuario.is_organizador:
            flash(f"{usuario.nickname} já é organizador.", "erro")
        else:
            usuario.is_organizador = True
            db.session.commit()
            flash(f"{usuario.nickname} ({usuario.email}) agora é organizador!", "sucesso")
        return redirect(url_for("admin_organizadores"))

    organizadores = User.query.filter_by(is_organizador=True).order_by(User.nickname).all()
    return render_template("admin_organizadores.html", organizadores=organizadores)


# ---------------------------------------------------------------------------
# Ranking por temporada (ver PLANO_TEMPORADA_RANKING.md)
# ---------------------------------------------------------------------------

def _tabela_ranking(temporada):
    """Classificação da temporada: uma linha por time que jogou pelo menos
    um campeonato elegível (pago e cheio) nela, com pontos, títulos, vices,
    terceiros, campeonatos jogados e partidas jogadas.

    Ordem (e desempate, nessa sequência): mais pontos, mais títulos, mais
    vices, MENOS partidas jogadas, nome. A regra de desempate está nos
    termos de uso — não mudar aqui sem mudar lá.

    Devolve (linhas, times_participantes).
    """
    torneios = Torneio.query.filter_by(temporada_id=temporada.id, elegivel_ranking=True).all()
    torneio_ids = [t.id for t in torneios]
    linhas = {}

    def linha(time_obj):
        if time_obj.id not in linhas:
            linhas[time_obj.id] = {
                "time": time_obj, "pontos": 0, "titulos": 0, "vices": 0, "terceiros": 0,
                "campeonatos": 0, "partidas": 0,
            }
        return linhas[time_obj.id]

    if torneio_ids:
        # Participação: inscrição paga e não devolvida num campeonato elegível.
        inscricoes = Inscricao.query.filter(
            Inscricao.torneio_id.in_(torneio_ids), Inscricao.status == "pago"
        ).all()
        for inscricao in inscricoes:
            if inscricao.time is not None and not inscricao.foi_reembolsada:
                linha(inscricao.time)["campeonatos"] += 1

        # Partidas jogadas de verdade (bye não conta — não teve adversário).
        confrontos = Confronto.query.filter(
            Confronto.torneio_id.in_(torneio_ids), Confronto.status.in_(["validado", "wo"])
        ).all()
        for confronto in confrontos:
            if confronto.time1 is None or confronto.time2 is None:
                continue
            for time_obj in (confronto.time1, confronto.time2):
                if time_obj.id in linhas:
                    linhas[time_obj.id]["partidas"] += 1

    for pontuacao in PontuacaoRanking.query.filter_by(temporada_id=temporada.id).all():
        if pontuacao.time is None:
            continue
        item = linha(pontuacao.time)
        item["pontos"] += pontuacao.pontos
        if pontuacao.colocacao == "campeao":
            item["titulos"] += 1
        elif pontuacao.colocacao == "vice":
            item["vices"] += 1
        elif pontuacao.colocacao == "terceiro":
            item["terceiros"] += 1

    ordenadas = sorted(
        linhas.values(),
        key=lambda l: (-l["pontos"], -l["titulos"], -l["vices"], l["partidas"], l["time"].nome.lower()),
    )
    for posicao, item in enumerate(ordenadas, start=1):
        item["posicao"] = posicao
    return ordenadas, len(ordenadas)


@app.route("/ranking")
def ranking():
    temporada = _temporada_aberta()
    if temporada is None:
        # Sem temporada aberta: mostra a última encerrada (se tiver), pra
        # página não ficar vazia entre uma temporada e outra.
        temporada = Temporada.query.filter_by(status="encerrada").order_by(Temporada.numero.desc()).first()
    linhas, participantes = _tabela_ranking(temporada) if temporada else ([], 0)
    dias_restantes = None
    if temporada and temporada.status == "aberta":
        dias_restantes = max((temporada.data_fim - date.today()).days, 0)
    return render_template(
        "ranking.html",
        temporada=temporada,
        linhas=linhas,
        participantes=participantes,
        dias_restantes=dias_restantes,
        hoje=date.today(),
    )


@app.route("/ranking/historico")
def ranking_historico():
    temporadas = Temporada.query.filter_by(status="encerrada").order_by(Temporada.numero.desc()).all()
    tabelas = {t.id: _tabela_ranking(t) for t in temporadas}
    return render_template("ranking_historico.html", temporadas=temporadas, tabelas=tabelas)


def _pagar_premio_temporada(temporada, campeao):
    """Transfere o prêmio da temporada pro capitão do 1º colocado. Mesmas
    regras do prêmio de campeonato: (mensagem, erro), nunca reenvia em cima
    de concluído/pendente, manda valor + taxa pra chegar o valor cheio."""
    if temporada.premio_pago_status in ("concluido", "pendente"):
        return None, None
    capitao = campeao.capitao
    if capitao is None or not capitao.chave_pix or not capitao.tipo_chave_pix:
        temporada.premio_pago_status = "falhou"
        return None, (
            f"{capitao.nickname if capitao else 'O capitão'} (capitão do {campeao.nome}) ainda não cadastrou "
            "uma chave Pix para receber o prêmio da temporada."
        )
    try:
        transferencia = pagamentos.enviar_pix(
            valor_reais=temporada.valor_premio + TAXA_ABACATEPAY_ESTIMADA,
            chave_pix=capitao.chave_pix,
            tipo_chave=capitao.tipo_chave_pix,
            descricao=f"Premio {temporada.nome} - {campeao.nome}",
            external_id=f"premio-temporada-{temporada.id}",
        )
    except pagamentos.PagamentoIndisponivel as erro:
        temporada.premio_pago_status = "falhou"
        return None, f"Prêmio da temporada: não deu para pagar agora ({erro})."

    temporada.premio_transferencia_id = transferencia.get("id")
    status_abacatepay = (transferencia.get("status") or "").upper()
    temporada.premio_pago_status = "concluido" if status_abacatepay in ("COMPLETED", "PAID") else "pendente"
    if temporada.premio_pago_status == "concluido":
        return f"Prêmio de R$ {temporada.valor_premio:.2f} da {temporada.nome} pago para {campeao.nome}!", None
    return "Transferência do prêmio da temporada enviada — status ainda pendente na AbacatePay.", None


@app.route("/admin/federacoes", methods=["GET", "POST"])
@login_required
def admin_federacoes():
    """Admin do site ajusta a fatia do lucro de cada federação (padrão
    80/20) e vê o código de convite de admin de cada uma."""
    if current_user.email != ADMIN_EMAIL:
        abort(404)

    if request.method == "POST":
        federacao = Federacao.query.get_or_404(request.form.get("federacao_id", type=int))
        try:
            pct = int(request.form.get("fatia_pct", ""))
            if not 0 <= pct <= 95:
                raise ValueError
        except ValueError:
            flash("A fatia da federação precisa ser um número inteiro entre 0 e 95 (%).", "erro")
            return redirect(url_for("admin_federacoes"))
        federacao.fatia_lucro = pct / 100
        db.session.commit()
        flash(f"{federacao.nome}: federação fica com {pct}% do lucro, plataforma com {100 - pct}%. Vale pros repasses daqui pra frente.", "sucesso")
        return redirect(url_for("admin_federacoes"))

    federacoes = Federacao.query.order_by(Federacao.nome).all()
    return render_template("admin_federacoes.html", federacoes=federacoes)


@app.route("/admin/usuarios")
@login_required
def admin_usuarios():
    """Consulta rápida do admin do site: acha um jogador por nick, e-mail
    ou nome do time e vê a chave Pix cadastrada — pra diagnosticar
    reembolso/prêmio que falhou sem precisar entrar no banco."""
    if current_user.email != ADMIN_EMAIL:
        abort(404)
    busca = request.args.get("q", "").strip()
    usuarios = []
    if busca:
        padrao = f"%{busca}%"
        usuarios = (
            User.query.outerjoin(Team, User.time_id == Team.id)
            .filter(db.or_(User.nickname.ilike(padrao), User.email.ilike(padrao), Team.nome.ilike(padrao)))
            .order_by(User.nickname)
            .limit(30)
            .all()
        )
    return render_template("admin_usuarios.html", busca=busca, usuarios=usuarios, tipos=pagamentos.TIPOS_CHAVE_PIX)


@app.route("/admin/temporadas", methods=["GET", "POST"])
@login_required
def admin_temporadas():
    if current_user.email != ADMIN_EMAIL:
        abort(404)

    if request.method == "POST":
        try:
            numero = int(request.form.get("numero", ""))
            data_inicio = datetime.strptime(request.form.get("data_inicio", ""), "%Y-%m-%d").date()
            data_fim = datetime.strptime(request.form.get("data_fim", ""), "%Y-%m-%d").date()
            meta_times = int(request.form.get("meta_times", "100"))
            valor_premio = float(request.form.get("valor_premio", "1000").replace(",", "."))
        except ValueError:
            flash("Preencha número, datas, meta e prêmio da temporada com valores válidos.", "erro")
            return redirect(url_for("admin_temporadas"))

        nome = request.form.get("nome", "").strip() or f"Temporada {numero}"
        if data_fim <= data_inicio:
            flash("A data de fim precisa ser depois da de início.", "erro")
        elif numero <= 0 or meta_times <= 0 or valor_premio < 0:
            flash("Número, meta e prêmio precisam ser positivos.", "erro")
        elif Temporada.query.filter_by(numero=numero).first():
            flash(f"Já existe uma temporada de número {numero}.", "erro")
        elif _temporada_aberta():
            # Só uma aberta por vez — senão um campeonato não saberia em qual
            # temporada pontuar.
            flash("Já tem uma temporada aberta. Encerre ela antes de abrir outra.", "erro")
        else:
            db.session.add(Temporada(
                numero=numero, nome=nome, data_inicio=data_inicio, data_fim=data_fim,
                meta_times=meta_times, valor_premio=valor_premio,
            ))
            db.session.commit()
            flash(f"{nome} aberta! Campeonatos pagos e cheios sorteados a partir de agora valem ponto.", "sucesso")
        return redirect(url_for("admin_temporadas"))

    temporadas = Temporada.query.order_by(Temporada.numero.desc()).all()
    resumo = {}
    for temporada in temporadas:
        linhas, participantes = _tabela_ranking(temporada)
        resumo[temporada.id] = {"lider": linhas[0] if linhas else None, "participantes": participantes}
    return render_template("admin_temporadas.html", temporadas=temporadas, resumo=resumo, hoje=date.today().isoformat())


@app.route("/admin/temporadas/<int:temporada_id>/encerrar", methods=["POST"])
@login_required
@limiter.limit("5 per minute")
def encerrar_temporada(temporada_id):
    """Fecha a temporada: trava o 1º lugar e paga o prêmio se a meta de
    times foi batida. Exige a senha do admin — sai dinheiro de verdade."""
    if current_user.email != ADMIN_EMAIL:
        abort(404)
    temporada = Temporada.query.get_or_404(temporada_id)

    if not check_password_hash(current_user.senha_hash, request.form.get("senha", "")):
        flash("Senha incorreta. A temporada não foi encerrada.", "erro")
        return redirect(url_for("admin_temporadas"))

    if temporada.status == "aberta":
        linhas, participantes = _tabela_ranking(temporada)
        temporada.status = "encerrada"
        temporada.time_campeao_id = linhas[0]["time"].id if linhas else None
        if temporada.time_campeao_id is None:
            temporada.premio_pago_status = "sem_meta"
            flash(f"{temporada.nome} encerrada sem nenhum time pontuando — sem campeão e sem prêmio.", "sucesso")
        elif participantes < temporada.meta_times:
            temporada.premio_pago_status = "sem_meta"
            flash(
                f"{temporada.nome} encerrada. {linhas[0]['time'].nome} terminou em 1º, mas a temporada teve "
                f"{participantes} time(s) de {temporada.meta_times} — o prêmio de R$ {temporada.valor_premio:.2f} não é pago.",
                "sucesso",
            )
        else:
            flash(f"{temporada.nome} encerrada. {linhas[0]['time'].nome} é o campeão da temporada!", "sucesso")
        db.session.commit()

    # Pagamento (ou nova tentativa, se falhou antes) — só com meta batida.
    if temporada.time_campeao_id and temporada.premio_pago_status not in ("sem_meta", "concluido", "pendente"):
        mensagem, erro = _pagar_premio_temporada(temporada, temporada.time_campeao)
        db.session.commit()
        if mensagem:
            flash(mensagem, "sucesso")
        if erro:
            flash(erro, "erro")
    elif temporada.premio_pago_status in ("concluido", "pendente"):
        flash("O prêmio dessa temporada já foi enviado.", "erro")

    return redirect(url_for("admin_temporadas"))


@app.route("/federacao/criar", methods=["POST"])
@login_required
def criar_federacao():
    if not current_user.is_organizador:
        flash("Só organizadores autorizados podem criar uma federação.", "erro")
        return redirect(url_for("perfil"))

    if current_user.federacao_id:
        flash("Você já faz parte de uma federação.", "erro")
        return redirect(url_for("perfil"))

    nome = request.form.get("nome", "").strip()
    if not nome:
        flash("Dê um nome para federação.", "erro")
        return redirect(url_for("perfil"))

    if Federacao.query.filter_by(nome=nome).first():
        flash("Já existe uma federação com esse nome.", "erro")
        return redirect(url_for("perfil"))

    federacao = Federacao(nome=nome, dono_id=current_user.id)
    db.session.add(federacao)
    db.session.flush()
    current_user.federacao_id = federacao.id
    db.session.commit()

    flash(f'Federação "{federacao.nome}" criada! Use o código {federacao.codigo_convite} para convidar outros admins.', "sucesso")
    return redirect(url_for("perfil"))


@app.route("/federacao/entrar", methods=["POST"])
@login_required
@limiter.limit("10 per minute")
def entrar_federacao():
    if current_user.federacao_id:
        flash("Você já faz parte de uma federação.", "erro")
        return redirect(url_for("perfil"))

    codigo = request.form.get("codigo", "").strip().upper()
    federacao = Federacao.query.filter_by(codigo_convite=codigo).first()
    if not federacao:
        flash("Código de convite inválido.", "erro")
        return redirect(url_for("perfil"))

    current_user.federacao_id = federacao.id
    db.session.commit()

    flash(f'Agora você é admin da federação "{federacao.nome}".', "sucesso")
    return redirect(url_for("perfil"))


@app.route("/federacao/sair", methods=["POST"])
@login_required
def sair_federacao():
    federacao = current_user.federacao
    if not federacao:
        flash("Você não faz parte de nenhuma federação.", "erro")
        return redirect(url_for("perfil"))

    if federacao.dono_id == current_user.id:
        flash("O dono não pode sair da federação — exclua ela ou transfira a posse antes.", "erro")
        return redirect(url_for("perfil"))

    current_user.federacao_id = None
    db.session.commit()

    flash(f'Você saiu da federação "{federacao.nome}".', "sucesso")
    return redirect(url_for("perfil"))


@app.route("/federacao/logo", methods=["POST"])
@login_required
def enviar_logo_federacao():
    if not current_user.federacao:
        flash("Você precisa fazer parte de uma federação.", "erro")
        return redirect(url_for("perfil"))

    arquivo = request.files.get("logo")
    if not arquivo or not arquivo.filename:
        flash("Selecione uma imagem antes de enviar.", "erro")
        return redirect(url_for("perfil"))

    extensao = arquivo.filename.rsplit(".", 1)[-1].lower() if "." in arquivo.filename else ""
    if extensao not in EXTENSOES_IMAGEM_PERMITIDAS:
        flash("Formato inválido. Envie uma imagem (png, jpg ou webp).", "erro")
        return redirect(url_for("perfil"))

    nome_arquivo = secure_filename(f"federacao{current_user.federacao_id}-{uuid.uuid4().hex}.{extensao}")
    arquivo.save(os.path.join(app.config["PASTA_FEDERACOES"], nome_arquivo))

    _apagar_upload("federacoes", current_user.federacao.logo)
    current_user.federacao.logo = nome_arquivo
    db.session.commit()

    flash("Logo da federação atualizada!", "sucesso")
    return redirect(url_for("perfil"))


@app.route("/federacao/whatsapp", methods=["POST"])
@login_required
def salvar_link_whatsapp_federacao():
    if not current_user.federacao or current_user.federacao.dono_id != current_user.id:
        flash("Só o dono da federação pode configurar o link do grupo.", "erro")
        return redirect(url_for("perfil"))

    link = request.form.get("link_grupo_whatsapp", "").strip()
    if link and not (link.startswith("https://chat.whatsapp.com/") or link.startswith("http://chat.whatsapp.com/")):
        flash("Link inválido — use o link de convite do grupo (começa com chat.whatsapp.com).", "erro")
        return redirect(url_for("perfil"))

    current_user.link_grupo_whatsapp = link or None
    db.session.commit()

    flash("Link do grupo de WhatsApp salvo!" if link else "Link do grupo de WhatsApp removido.", "sucesso")
    return redirect(url_for("perfil"))


@app.route("/perfil/notificacoes", methods=["POST"])
@login_required
def atualizar_preferencias_notificacao():
    silenciadas = request.form.getlist("silenciar")
    current_user.notif_campeonatos_silenciadas = "campeonatos" in silenciadas
    current_user.notif_partidas_silenciadas = "partidas" in silenciadas
    db.session.commit()

    flash("Preferências de notificação salvas.", "sucesso")
    return redirect(url_for("perfil"))


@app.route("/notificacoes")
@login_required
def notificacoes():
    lista = (
        Notificacao.query
        .filter_by(usuario_id=current_user.id)
        .order_by(Notificacao.criado_em.desc())
        .limit(50)
        .all()
    )

    # Marca como lida ao abrir a página, mas guarda quais estavam sem ler
    # antes disso para destacar elas visualmente nessa mesma visita.
    ids_novas = {n.id for n in lista if not n.lida}
    for notificacao in lista:
        notificacao.lida = True
    if ids_novas:
        db.session.commit()

    return render_template("notificacoes.html", notificacoes=lista, ids_novas=ids_novas)


@app.route("/notificacoes/<int:notificacao_id>/excluir", methods=["POST"])
@login_required
def excluir_notificacao(notificacao_id):
    notificacao = Notificacao.query.get_or_404(notificacao_id)
    if notificacao.usuario_id != current_user.id:
        abort(404)

    db.session.delete(notificacao)
    db.session.commit()
    return redirect(url_for("notificacoes"))


@app.route("/notificacoes/limpar", methods=["POST"])
@login_required
def limpar_notificacoes():
    Notificacao.query.filter_by(usuario_id=current_user.id).delete()
    db.session.commit()
    flash("Notificações limpas.", "sucesso")
    return redirect(url_for("notificacoes"))


@app.route("/time")
@login_required
def time():
    pedidos_recebidos = []
    if current_user.time and current_user.time.capitao_id == current_user.id:
        pedidos_recebidos = (
            PedidoEntrada.query.filter_by(time_id=current_user.time_id, status="pendente")
            .order_by(PedidoEntrada.criado_em.asc())
            .all()
        )
    meus_pedidos = []
    if not current_user.time_id:
        meus_pedidos = (
            PedidoEntrada.query.filter_by(usuario_id=current_user.id)
            .order_by(PedidoEntrada.criado_em.desc())
            .limit(10)
            .all()
        )
    return render_template(
        "time.html",
        usuario=current_user,
        formacoes=FORMACOES_VALIDAS,
        pedidos_recebidos=pedidos_recebidos,
        meus_pedidos=meus_pedidos,
        posicoes=POSICOES,
    )


# ---------------------------------------------------------------------------
# Mercado de transferências (item 5 do ROADMAP.md)
# ---------------------------------------------------------------------------

@app.route("/time/recrutamento", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def atualizar_recrutamento():
    """Capitão liga/desliga o "recrutando" e o recado que aparece no diretório."""
    time_atual = current_user.time
    if not time_atual or time_atual.capitao_id != current_user.id:
        flash("Só o capitão pode mexer no recrutamento do time.", "erro")
        return redirect(url_for("time"))

    time_atual.recrutando = request.form.get("recrutando") == "1"
    mensagem = request.form.get("mensagem_recrutamento", "").strip()[:160]
    time_atual.mensagem_recrutamento = mensagem or None
    db.session.commit()

    if time_atual.recrutando:
        flash("Time marcado como recrutando — ele aparece com vaga aberta no diretório de times.", "sucesso")
    else:
        flash("Recrutamento fechado.", "sucesso")
    return redirect(url_for("time"))


@app.route("/times/<int:time_id>/pedir-entrada", methods=["POST"])
@login_required
@limiter.limit("10 per minute")
def pedir_entrada(time_id):
    """Jogador sem time pede pra entrar num time que está recrutando. O
    capitão recebe uma notificação e decide."""
    time_obj = Team.query.get_or_404(time_id)

    if current_user.time_id:
        flash("Você já faz parte de um time. Saia dele antes de pedir pra entrar em outro.", "erro")
        return redirect(url_for("detalhe_time", time_id=time_id))
    if not time_obj.recrutando:
        flash(f"{time_obj.nome} não está recrutando no momento.", "erro")
        return redirect(url_for("detalhe_time", time_id=time_id))
    if PedidoEntrada.query.filter_by(time_id=time_obj.id, usuario_id=current_user.id, status="pendente").first():
        flash("Você já mandou um pedido pra esse time — aguarde o capitão responder.", "erro")
        return redirect(url_for("detalhe_time", time_id=time_id))

    pedido = PedidoEntrada(
        time_id=time_obj.id,
        usuario_id=current_user.id,
        mensagem=request.form.get("mensagem", "").strip()[:200] or None,
    )
    db.session.add(pedido)
    if time_obj.capitao:
        criar_notificacao(
            time_obj.capitao,
            "pedido_entrada",
            f"{current_user.nickname} (nível {current_user.nivel}) pediu pra entrar no {time_obj.nome}. Responda em \"Meu time\".",
        )
    db.session.commit()
    flash(f"Pedido enviado pro capitão do {time_obj.nome}! Você recebe uma notificação quando ele responder.", "sucesso")
    return redirect(url_for("detalhe_time", time_id=time_id))


@app.route("/time/pedidos/<int:pedido_id>/cancelar", methods=["POST"])
@login_required
def cancelar_pedido_entrada(pedido_id):
    pedido = PedidoEntrada.query.get_or_404(pedido_id)
    if pedido.usuario_id != current_user.id:
        abort(404)
    if pedido.status == "pendente":
        pedido.status = "cancelado"
        pedido.respondido_em = datetime.utcnow()
        db.session.commit()
        flash("Pedido cancelado.", "sucesso")
    return redirect(request.referrer or url_for("time"))


@app.route("/time/pedidos/<int:pedido_id>/<resposta>", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def responder_pedido_entrada(pedido_id, resposta):
    """Capitão aceita ou recusa. Aceitar coloca o jogador no time pela
    mesma trava de sempre (1 time por jogador) e recusa sozinho os outros
    pedidos pendentes dele."""
    if resposta not in ("aceitar", "recusar"):
        abort(404)
    pedido = PedidoEntrada.query.get_or_404(pedido_id)
    time_obj = pedido.time
    if time_obj is None or time_obj.capitao_id != current_user.id:
        flash("Só o capitão do time pode responder esse pedido.", "erro")
        return redirect(url_for("time"))
    if pedido.status != "pendente":
        flash("Esse pedido já foi respondido.", "erro")
        return redirect(url_for("time"))

    jogador = pedido.usuario
    pedido.respondido_em = datetime.utcnow()

    if resposta == "recusar" or jogador is None:
        pedido.status = "recusado"
        if jogador is not None:
            criar_notificacao(jogador, "pedido_respondido", f"O {time_obj.nome} recusou seu pedido de entrada.")
        db.session.commit()
        flash("Pedido recusado.", "sucesso")
        return redirect(url_for("time"))

    if jogador.time_id:
        # Entrou em outro time enquanto o pedido esperava — não dá pra
        # estar em dois.
        pedido.status = "recusado"
        db.session.commit()
        flash(f"{jogador.nickname} já entrou em outro time nesse meio-tempo — pedido recusado automaticamente.", "erro")
        return redirect(url_for("time"))

    jogador.time_id = time_obj.id
    jogador.posicao_escalada = None
    pedido.status = "aceito"
    # Os outros pedidos pendentes dele perdem o sentido.
    for outro in PedidoEntrada.query.filter_by(usuario_id=jogador.id, status="pendente").all():
        if outro.id != pedido.id:
            outro.status = "recusado"
            outro.respondido_em = datetime.utcnow()
    criar_notificacao(jogador, "pedido_respondido", f"O {time_obj.nome} aceitou seu pedido — você entrou no time!")
    db.session.commit()
    flash(f"{jogador.nickname} entrou no {time_obj.nome}!", "sucesso")
    return redirect(url_for("time"))


@app.route("/time/criar", methods=["POST"])
@login_required
def criar_time():
    if current_user.time_id:
        flash("Você já faz parte de um time. Saia dele antes de criar outro.", "erro")
        return redirect(url_for("time"))

    nome = request.form["nome"].strip()
    tag = request.form["tag"].strip().upper()[:5]

    if Team.query.filter_by(nome=nome).first():
        flash("Já existe um time com esse nome.", "erro")
        return redirect(url_for("time"))

    if not request.form.get("confirmar_nome_parecido"):
        nomes_parecidos = [t.nome for t in Team.query.all() if _nomes_parecidos(nome, t.nome)]
        if nomes_parecidos:
            return render_template(
                "confirmar_nome_time.html",
                nome=nome,
                tag=tag,
                nomes_parecidos=nomes_parecidos,
            )

    novo_time = Team(nome=nome, tag=tag, capitao_id=current_user.id)
    db.session.add(novo_time)
    db.session.flush()  # já gera o id do time antes de vincular o usuário

    current_user.time_id = novo_time.id
    db.session.commit()

    flash(f"Time {nome} criado! Código de convite: {novo_time.codigo_convite}", "sucesso")
    return redirect(url_for("time"))


@app.route("/time/entrar", methods=["POST"])
@login_required
@limiter.limit("10 per minute")
def entrar_time():
    if current_user.time_id:
        flash("Você já faz parte de um time. Saia dele antes de entrar em outro.", "erro")
        return redirect(url_for("time"))

    codigo = request.form["codigo"].strip().upper()
    time_encontrado = Team.query.filter_by(codigo_convite=codigo).first()

    if not time_encontrado:
        flash("Código de convite inválido.", "erro")
        return redirect(url_for("time"))

    current_user.time_id = time_encontrado.id
    current_user.posicao_escalada = None
    db.session.commit()

    flash(f"Você entrou no time {time_encontrado.nome}!", "sucesso")
    return redirect(url_for("time"))


@app.route("/time/sair", methods=["POST"])
@login_required
def sair_time():
    time_atual = current_user.time

    if current_user.time_id == current_user.id:
        pass  # placeholder de segurança, não usado por enquanto

    if time_atual and time_atual.capitao_id == current_user.id and len(time_atual.membros) > 1:
        flash("Você é o capitão. Passe a braçadeira para outro membro antes de sair.", "erro")
        return redirect(url_for("passar_bracadeira"))

    # Sair sendo o último membro dissolve o time. Se ele estiver num
    # campeonato que já começou, dissolver deixaria o chaveamento apontando
    # para um time que não existe mais (e o banco recusa a exclusão) — então
    # esse caso é barrado antes de mexer em qualquer coisa.
    if time_atual and len(time_atual.membros) == 1:
        em_andamento = [
            i.torneio.nome for i in Inscricao.query.filter_by(time_id=time_atual.id).all()
            if i.torneio and i.torneio.status != "aberto"
        ]
        if em_andamento:
            flash(
                f"Você é o último membro do {time_atual.nome} e ele está disputando "
                f"{', '.join(em_andamento)}. Saindo agora, o time seria desfeito no meio do campeonato — "
                "espere o campeonato terminar ou chame alguém para o time antes de sair.",
                "erro",
            )
            return redirect(url_for("time"))

    current_user.time_id = None
    current_user.posicao_escalada = None
    db.session.commit()

    # Time vazio é desfeito. As inscrições dele saem junto — e as que já
    # estavam pagas são devolvidas (menos a taxa), senão o time ficava sem
    # vaga E sem o dinheiro. Só sobram campeonatos ainda abertos aqui: os que
    # já começaram foram barrados acima.
    avisos = []
    if time_atual and len(time_atual.membros) == 0:
        for inscricao in Inscricao.query.filter_by(time_id=time_atual.id).all():
            mensagem, erro = _reembolsar_inscricao(inscricao)
            if mensagem:
                avisos.append(mensagem)
            elif erro:
                avisos.append(erro)
        db.session.commit()

        Inscricao.query.filter_by(time_id=time_atual.id).delete()
        PedidoEntrada.query.filter_by(time_id=time_atual.id).delete()
        _apagar_upload("escudos", time_atual.escudo_imagem)
        db.session.delete(time_atual)
        db.session.commit()

    flash(" ".join(["Você saiu do time."] + avisos), "sucesso")
    return redirect(url_for("time"))


@app.route("/time/passar-bracadeira")
@login_required
def passar_bracadeira():
    time_atual = current_user.time

    if not time_atual or time_atual.capitao_id != current_user.id:
        flash("Só o capitão pode passar a braçadeira.", "erro")
        return redirect(url_for("time"))

    outros_membros = [m for m in time_atual.membros if m.id != current_user.id]
    return render_template("passar_bracadeira.html", time=time_atual, membros=outros_membros)


@app.route("/time/passar-bracadeira/confirmar/<int:membro_id>", methods=["GET", "POST"])
@login_required
def confirmar_bracadeira(membro_id):
    time_atual = current_user.time

    if not time_atual or time_atual.capitao_id != current_user.id:
        flash("Só o capitão pode passar a braçadeira.", "erro")
        return redirect(url_for("time"))

    novo_capitao = User.query.get(membro_id)

    if not novo_capitao or novo_capitao.time_id != time_atual.id:
        flash("Esse jogador não faz parte do time.", "erro")
        return redirect(url_for("passar_bracadeira"))

    if request.method == "POST":
        time_atual.capitao_id = novo_capitao.id
        db.session.commit()
        flash(f"{novo_capitao.nickname} agora é o capitão do time.", "sucesso")
        return redirect(url_for("time"))

    # GET: só mostra a tela de confirmação, não executa nada ainda
    return render_template("confirmar_bracadeira.html", jogador=novo_capitao)


FORMACOES_VALIDAS = list(FORMACOES)


@app.route("/time/formacao", methods=["POST"])
@login_required
def atualizar_formacao():
    time_atual = current_user.time

    if not time_atual or time_atual.capitao_id != current_user.id:
        flash("Só o capitão pode mudar o esquema tático.", "erro")
        return redirect(url_for("time"))

    formacao = request.form["formacao"]
    if formacao not in FORMACOES_VALIDAS:
        flash("Esquema tático inválido.", "erro")
        return redirect(url_for("time"))

    time_atual.formacao = formacao
    # Slot que não existe mais no esquema novo (ex.: saiu o segundo
    # atacante) libera o jogador pra reserva — ele escolhe outro depois.
    validos = set(chaves_da_formacao(formacao))
    desescalados = []
    for membro in time_atual.membros:
        if membro.posicao_escalada and membro.posicao_escalada not in validos:
            membro.posicao_escalada = None
            desescalados.append(membro.nickname)
    db.session.commit()
    aviso = f" {', '.join(desescalados)} saiu/saíram da escalação (a posição não existe nesse esquema)." if desescalados else ""
    flash(f"Esquema tático atualizado para {formacao}.{aviso}", "sucesso")
    return redirect(url_for("time"))


@app.route("/time/escalacao", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def escolher_posicao():
    """Jogador ocupa um slot vago do esquema do próprio time (ou sai do
    que ocupa). Um slot por jogador, um jogador por slot."""
    time_atual = current_user.time
    if not time_atual:
        flash("Você precisa estar num time pra escolher posição.", "erro")
        return redirect(url_for("time"))

    slot = request.form.get("slot", "").strip()
    if not slot:
        current_user.posicao_escalada = None
        db.session.commit()
        flash("Você saiu da escalação — ficou como reserva.", "sucesso")
        return redirect(url_for("time"))

    if slot not in chaves_da_formacao(time_atual.formacao):
        flash("Essa posição não existe no esquema do time.", "erro")
        return redirect(url_for("time"))
    ocupante = next((m for m in time_atual.membros if m.posicao_escalada == slot and m.id != current_user.id), None)
    if ocupante:
        flash(f"Essa posição já é do {ocupante.nickname}. Escolha outra ou peça pra ele liberar.", "erro")
        return redirect(url_for("time"))

    current_user.posicao_escalada = slot
    db.session.commit()
    codigo = slot.rstrip("0123456789")
    flash(f"Você agora joga de {POSICOES[codigo][1].lower()} ({POSICOES[codigo][0]}).", "sucesso")
    return redirect(url_for("time"))


@app.route("/time/escalacao/<int:membro_id>/tirar", methods=["POST"])
@login_required
def tirar_da_escalacao(membro_id):
    """Capitão tira um membro do slot (vira reserva)."""
    time_atual = current_user.time
    if not time_atual or time_atual.capitao_id != current_user.id:
        flash("Só o capitão pode mexer na escalação dos outros.", "erro")
        return redirect(url_for("time"))
    membro = User.query.get_or_404(membro_id)
    if membro.time_id != time_atual.id:
        abort(404)
    membro.posicao_escalada = None
    db.session.commit()
    flash(f"{membro.nickname} saiu da escalação.", "sucesso")
    return redirect(url_for("time"))


@app.route("/perfil/posicao", methods=["POST"])
@login_required
def atualizar_posicao_preferida():
    codigo = request.form.get("posicao_preferida", "").strip()
    if codigo and codigo not in POSICOES:
        flash("Posição inválida.", "erro")
        return redirect(url_for("perfil"))
    current_user.posicao_preferida = codigo or None
    db.session.commit()
    flash("Posição preferida salva." if codigo else "Posição preferida removida.", "sucesso")
    return redirect(url_for("perfil"))


@app.route("/time/escudo", methods=["POST"])
@login_required
def enviar_escudo_time():
    time_atual = current_user.time

    if not time_atual or time_atual.capitao_id != current_user.id:
        flash("Só o capitão pode mudar o escudo do time.", "erro")
        return redirect(url_for("time"))

    arquivo = request.files.get("escudo")
    if not arquivo or not arquivo.filename:
        flash("Selecione uma imagem antes de enviar.", "erro")
        return redirect(url_for("time"))

    extensao = arquivo.filename.rsplit(".", 1)[-1].lower() if "." in arquivo.filename else ""
    if extensao not in EXTENSOES_IMAGEM_PERMITIDAS:
        flash("Formato inválido. Envie uma imagem (png, jpg ou webp).", "erro")
        return redirect(url_for("time"))

    nome_arquivo = secure_filename(f"time{time_atual.id}-{uuid.uuid4().hex}.{extensao}")
    arquivo.save(os.path.join(app.config["PASTA_ESCUDOS"], nome_arquivo))

    _apagar_upload("escudos", time_atual.escudo_imagem)
    time_atual.escudo_imagem = nome_arquivo
    db.session.commit()

    flash("Escudo do time atualizado!", "sucesso")
    return redirect(url_for("time"))


@app.route("/campeonatos")
def campeonatos():
    # Inscrições abertas primeiro, depois em andamento, finalizados por
    # último. Dentro de cada grupo, ordem cronológica pela data do
    # campeonato (os sem data definida — campeonatos antigos — vão para o
    # final do grupo).
    ordem_status = case(
        (Torneio.status == "aberto", 0),
        (Torneio.status == "em_andamento", 1),
        else_=2,
    )
    consulta = Torneio.query

    federacao_id = request.args.get("federacao", type=int)
    if federacao_id:
        consulta = consulta.filter(Torneio.federacao_id == federacao_id)

    geracao = request.args.get("geracao")
    if geracao in ("antiga", "nova"):
        consulta = consulta.filter(Torneio.geracao == geracao)
    else:
        geracao = None

    jogo = request.args.get("jogo")
    if jogo in JOGOS:
        consulta = consulta.filter(Torneio.jogo == jogo)
    else:
        jogo = None

    lista = consulta.order_by(
        ordem_status,
        Torneio.data_campeonato.is_(None),
        Torneio.data_campeonato.asc(),
        Torneio.criado_em.desc(),
    ).all()

    federacoes = Federacao.query.order_by(Federacao.nome).all()
    federacao_obj = next((f for f in federacoes if f.id == federacao_id), None)

    return render_template(
        "campeonatos.html",
        torneios=lista,
        federacoes=federacoes,
        federacao_selecionada=federacao_id,
        federacao_obj=federacao_obj,
        geracao_selecionada=geracao,
        jogo_selecionado=jogo,
        jogos=JOGOS,
    )


@app.route("/federacoes")
def federacoes():
    lista = Federacao.query.order_by(Federacao.nome).all()

    contagens = dict(
        db.session.query(Torneio.federacao_id, db.func.count(Torneio.id))
        .group_by(Torneio.federacao_id)
        .all()
    )
    abertos = dict(
        db.session.query(Torneio.federacao_id, db.func.count(Torneio.id))
        .filter(Torneio.status == "aberto")
        .group_by(Torneio.federacao_id)
        .all()
    )

    return render_template(
        "federacoes.html",
        federacoes=lista,
        contagens=contagens,
        abertos=abertos,
    )


@app.route("/federacoes/<int:federacao_id>")
def detalhe_federacao(federacao_id):
    federacao = Federacao.query.get_or_404(federacao_id)

    torneios = Torneio.query.filter_by(federacao_id=federacao.id).order_by(Torneio.criado_em.desc()).all()
    torneio_ids = [t.id for t in torneios]

    # Ranking de times que mais foram campeões nos campeonatos dessa
    # federação — Counter conta quantas vezes cada time_campeao_id aparece.
    contagem_titulos = Counter(t.time_campeao_id for t in torneios if t.time_campeao_id)
    ranking_campeoes = [
        (Team.query.get(time_id), titulos)
        for time_id, titulos in contagem_titulos.most_common(5)
    ]

    times_participantes = 0
    premios_pagos = 0.0
    ranking_artilharia = []
    if torneio_ids:
        times_participantes = (
            db.session.query(Inscricao.time_id)
            .filter(Inscricao.torneio_id.in_(torneio_ids), Inscricao.status == "pago")
            .distinct()
            .count()
        )
        premios_pagos = sum(
            valor
            for t in torneios
            for valor, status in _status_premios(t).values()
            if status == "concluido"
        )

        # Gols marcados por time nas partidas já confirmadas (só "validado"
        # tem placar de verdade — W.O. não tem gols pra contar).
        confrontos_validados = Confronto.query.filter(
            Confronto.torneio_id.in_(torneio_ids), Confronto.status == "validado"
        ).all()
        gols_por_time = Counter()
        for confronto in confrontos_validados:
            if confronto.time1_id is not None and confronto.placar_time1 is not None:
                gols_por_time[confronto.time1_id] += confronto.placar_time1
            if confronto.time2_id is not None and confronto.placar_time2 is not None:
                gols_por_time[confronto.time2_id] += confronto.placar_time2
        ranking_artilharia = [
            (Team.query.get(time_id), gols)
            for time_id, gols in gols_por_time.most_common(5)
        ]

    return render_template(
        "federacao_detalhe.html",
        federacao=federacao,
        torneios=torneios,
        ranking_campeoes=ranking_campeoes,
        ranking_artilharia=ranking_artilharia,
        times_participantes=times_participantes,
        premios_pagos=premios_pagos,
    )


def _titulos_por_time():
    """{time_id: quantidade de campeonatos que ele venceu}."""
    return dict(
        db.session.query(Torneio.time_campeao_id, db.func.count(Torneio.id))
        .filter(Torneio.time_campeao_id.isnot(None))
        .group_by(Torneio.time_campeao_id)
        .all()
    )


def _campeonatos_jogados_por_time():
    """{time_id: quantidade de campeonatos em que ele entrou de verdade}.

    Conta inscrição paga que não foi devolvida — time reembolsado saiu do
    campeonato antes de jogar, então não conta como participação.
    """
    return dict(
        db.session.query(Inscricao.time_id, db.func.count(Inscricao.id))
        .filter(
            Inscricao.status == "pago",
            db.or_(
                Inscricao.reembolso_status.is_(None),
                Inscricao.reembolso_status.notin_(["concluido", "pendente"]),
            ),
        )
        .group_by(Inscricao.time_id)
        .all()
    )


@app.route("/times")
def times():
    """Diretório público de todos os times do site.

    Existe pra jogador achar time (e time achar jogador) sem precisar de
    código de convite na mão — é a base do mercado de transferências e do
    ranking (ver ROADMAP.md). Ordena por títulos, depois por campeonatos
    jogados, pra quem mais compete aparecer primeiro.
    """
    busca = request.args.get("q", "").strip()
    so_recrutando = request.args.get("recrutando") == "1"
    consulta = Team.query
    if busca:
        padrao = f"%{busca}%"
        consulta = consulta.filter(db.or_(Team.nome.ilike(padrao), Team.tag.ilike(padrao)))
    if so_recrutando:
        consulta = consulta.filter(Team.recrutando.is_(True))
    lista = consulta.all()

    titulos = _titulos_por_time()
    jogados = _campeonatos_jogados_por_time()
    lista.sort(key=lambda t: (-titulos.get(t.id, 0), -jogados.get(t.id, 0), t.nome.lower()))

    return render_template(
        "times.html",
        times=lista,
        titulos=titulos,
        jogados=jogados,
        busca=busca,
        so_recrutando=so_recrutando,
        total=Team.query.count(),
    )


@app.route("/times/<int:time_id>")
def detalhe_time(time_id):
    """Página pública de um time: elenco, títulos e histórico de campeonatos."""
    time_obj = Team.query.get_or_404(time_id)

    # Só campeonatos em que o time entrou de verdade (mesma regra do
    # diretório): inscrição paga e não devolvida.
    inscricoes = [
        i for i in Inscricao.query.filter_by(time_id=time_obj.id, status="pago").all()
        if not i.foi_reembolsada and i.torneio is not None
    ]
    historico = sorted(
        (i.torneio for i in inscricoes),
        key=lambda t: (t.data_campeonato or date.min, t.criado_em or datetime.min),
        reverse=True,
    )
    titulos = sum(1 for t in historico if t.time_campeao_id == time_obj.id)

    # Extrato de pontos do ranking, por temporada (mais recente primeiro).
    pontuacoes = (
        PontuacaoRanking.query.filter_by(time_id=time_obj.id)
        .order_by(PontuacaoRanking.temporada_id.desc(), PontuacaoRanking.criado_em.desc())
        .all()
    )
    extrato = {}
    for pontuacao in pontuacoes:
        extrato.setdefault(pontuacao.temporada, []).append(pontuacao)

    pedido_pendente = None
    if current_user.is_authenticated:
        pedido_pendente = PedidoEntrada.query.filter_by(
            time_id=time_obj.id, usuario_id=current_user.id, status="pendente"
        ).first()

    return render_template(
        "time_detalhe.html",
        time_obj=time_obj,
        historico=historico,
        titulos=titulos,
        extrato=extrato,
        pedido_pendente=pedido_pendente,
    )


TAMANHOS_VALIDOS = [4, 8, 16, 32]

# A AbacatePay cobra uma taxa fixa (não percentual) por Pix — na prática,
# testamos e confirmamos ~R$0,80 por transação. Isso é insignificante numa
# inscrição de R$20, mas destrói uma de R$1 (a taxa sozinha já é 80% do
# valor). Esses mínimos existem pra nenhum organizador criar sem querer
# um campeonato pago que nunca vai sobrar dinheiro suficiente pra pagar
# o prêmio/repasse depois que a taxa é descontada.
TAXA_ABACATEPAY_ESTIMADA = 0.80
VALOR_MINIMO_INSCRICAO_PAGA = 5.00
VALOR_MINIMO_PIX_ABACATEPAY = 1.00  # mínimo documentado da própria AbacatePay pra qualquer Pix
CUSTO_IA_POR_LEITURA_ESTIMADO = 0.02  # custo da API da Anthropic (Claude Haiku) por print lido, com folga
LUCRO_MINIMO_PLATAFORMA = 5.00  # lucro real mínimo (já descontando taxas e IA) que um campeonato pago precisa garantir


@app.route("/campeonatos/criar", methods=["GET", "POST"])
@login_required
def criar_campeonato():
    if not current_user.federacao_id:
        flash("Você precisa fazer parte de uma federação para criar campeonatos.", "erro")
        return redirect(url_for("perfil"))

    if request.method == "POST":
        nome = request.form["nome"].strip()
        descricao = request.form["descricao"].strip()
        valor_texto = request.form["valor_inscricao"].strip().replace(",", ".")

        try:
            valor = float(valor_texto)
            if valor < 0:
                raise ValueError
        except ValueError:
            flash("Valor de inscrição inválido.", "erro")
            return redirect(url_for("criar_campeonato"))

        if 0 < valor < VALOR_MINIMO_INSCRICAO_PAGA:
            flash(
                f"Inscrição paga precisa ser de pelo menos R$ {VALOR_MINIMO_INSCRICAO_PAGA:.2f} — "
                f"a AbacatePay cobra uma taxa fixa por Pix (na prática, cerca de R$ {TAXA_ABACATEPAY_ESTIMADA:.2f}), "
                "e um valor muito baixo não deixa sobrar dinheiro suficiente pra pagar o prêmio depois.",
                "erro",
            )
            return redirect(url_for("criar_campeonato"))

        try:
            max_times = int(request.form["max_times"])
            if max_times not in TAMANHOS_VALIDOS:
                raise ValueError
        except (ValueError, KeyError):
            flash("Número de chaves inválido.", "erro")
            return redirect(url_for("criar_campeonato"))

        premios = {}
        for campo, rotulo in (("valor_premio", "do campeão"), ("valor_premio_vice", "do vice"), ("valor_premio_terceiro", "do 3º lugar")):
            premio_texto = request.form.get(campo, "").strip().replace(",", ".")
            try:
                premios[campo] = float(premio_texto) if premio_texto else 0
                if premios[campo] < 0:
                    raise ValueError
            except ValueError:
                flash(f"Valor do prêmio {rotulo} inválido.", "erro")
                return redirect(url_for("criar_campeonato"))
            if 0 < premios[campo] < VALOR_MINIMO_PIX_ABACATEPAY:
                flash(
                    f"Prêmio {rotulo} precisa ser de pelo menos R$ {VALOR_MINIMO_PIX_ABACATEPAY:.2f} (mínimo exigido pela "
                    "AbacatePay pra qualquer Pix) ou R$ 0,00 (sem prêmio).",
                    "erro",
                )
                return redirect(url_for("criar_campeonato"))
        valor_premio = premios["valor_premio"]
        valor_premio_vice = premios["valor_premio_vice"]
        valor_premio_terceiro = premios["valor_premio_terceiro"]

        # Prêmio de vice/terceiro sem prêmio de campeão (ou maior que o do
        # campeão) não faz sentido — pódio é decrescente.
        if not (valor_premio >= valor_premio_vice >= valor_premio_terceiro):
            flash("Os prêmios precisam ser decrescentes: campeão ≥ vice ≥ 3º lugar.", "erro")
            return redirect(url_for("criar_campeonato"))

        if valor > 0:
            # Estimativa conservadora de leituras de IA (mata-mata: max_times-1 partidas,
            # 2 prints por partida, +20% de folga pra reenvio em caso de divergência).
            leituras_estimadas = (max_times - 1) * 2 * 1.2
            custo_ia_estimado = leituras_estimadas * CUSTO_IA_POR_LEITURA_ESTIMADO

            # A AbacatePay cobra a mesma taxa fixa em QUALQUER envio, não só
            # no recebimento — então mandar o prêmio e o repasse cheios
            # custa +1 taxa cada. Essas taxas de envio saem da fatia da
            # plataforma (nunca do prêmio nem do repasse), então entram
            # aqui como custo direto do lucro estimado da plataforma.
            arrecadacao_liquida_maxima = max_times * max(valor - TAXA_ABACATEPAY_ESTIMADA, 0)
            custo_premio_maximo = _custo_premios([valor_premio, valor_premio_vice, valor_premio_terceiro])
            base_para_divisao = arrecadacao_liquida_maxima - custo_premio_maximo
            lucro_plataforma_estimado = (
                base_para_divisao * (1 - _fatia_federacao(current_user.federacao))
                - TAXA_ABACATEPAY_ESTIMADA  # taxa de envio do repasse pra federação
                - custo_ia_estimado
            )

            if lucro_plataforma_estimado < LUCRO_MINIMO_PLATAFORMA:
                total_premios = valor_premio + valor_premio_vice + valor_premio_terceiro
                flash(
                    f"Com {max_times} times a R$ {valor:.2f} e R$ {total_premios:.2f} em prêmios, o lucro "
                    f"da plataforma fica em torno de R$ {lucro_plataforma_estimado:.2f} — abaixo do mínimo "
                    f"de R$ {LUCRO_MINIMO_PLATAFORMA:.2f}. Aumente o valor da inscrição, reduza os prêmios ou "
                    "escolha um chaveamento com mais times.",
                    "erro",
                )
                return redirect(url_for("criar_campeonato"))

        try:
            data_campeonato = datetime.strptime(request.form["data_campeonato"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            flash("Data do campeonato inválida.", "erro")
            return redirect(url_for("criar_campeonato"))

        if data_campeonato < date.today():
            flash("A data do campeonato não pode ser no passado.", "erro")
            return redirect(url_for("criar_campeonato"))

        hora_texto = request.form.get("hora_campeonato", "").strip()
        hora_campeonato = None
        if hora_texto:
            try:
                hora_campeonato = datetime.strptime(hora_texto, "%H:%M").time()
            except ValueError:
                flash("Horário do campeonato inválido.", "erro")
                return redirect(url_for("criar_campeonato"))

        formato = request.form.get("formato", "mata_mata")
        if formato not in ("mata_mata", "fase_grupos"):
            flash("Formato de campeonato inválido.", "erro")
            return redirect(url_for("criar_campeonato"))

        geracao = request.form.get("geracao", "nova")
        if geracao not in ("antiga", "nova"):
            flash("Geração do campeonato inválida.", "erro")
            return redirect(url_for("criar_campeonato"))

        jogo = request.form.get("jogo", JOGO_PADRAO)
        if jogo not in JOGOS:
            flash("Jogo do campeonato inválido.", "erro")
            return redirect(url_for("criar_campeonato"))

        novo_campeonato = Torneio(
            nome=nome,
            descricao=descricao,
            valor_inscricao=valor,
            max_times=max_times,
            valor_premio=valor_premio,
            valor_premio_vice=valor_premio_vice,
            valor_premio_terceiro=valor_premio_terceiro,
            data_campeonato=data_campeonato,
            hora_campeonato=hora_campeonato,
            formato=formato,
            geracao=geracao,
            jogo=jogo,
            federacao_id=current_user.federacao_id,
            criado_por_id=current_user.id,
        )
        db.session.add(novo_campeonato)
        db.session.commit()

        flash("Campeonato criado!", "sucesso")
        return redirect(url_for("detalhe_campeonato", torneio_id=novo_campeonato.id))

    return render_template(
        "criar_campeonato.html",
        tamanhos=TAMANHOS_VALIDOS,
        hoje=date.today().isoformat(),
        jogos=JOGOS,
        jogo_padrao=JOGO_PADRAO,
    )


@app.route("/campeonatos/<int:torneio_id>")
def detalhe_campeonato(torneio_id):
    torneio = Torneio.query.get_or_404(torneio_id)

    time_ja_inscrito = False
    inscricao_pendente = None
    if current_user.is_authenticated and current_user.time:
        time_ja_inscrito = current_user.time in torneio.times_inscritos
        if not time_ja_inscrito:
            inscricao_pendente = Inscricao.query.filter_by(
                torneio_id=torneio.id, time_id=current_user.time.id, status="pendente"
            ).first()

    eh_organizador = _pode_administrar_torneio(torneio)
    times_pagos = len(torneio.times_inscritos)
    pode_gerar_chaveamento = (
        eh_organizador
        and torneio.status == "aberto"
        and times_pagos >= 2
        and (torneio.formato != "fase_grupos" or times_pagos % 4 == 0)
    )
    # Quanto o prêmio ficaria se o organizador começasse agora, com menos
    # times do que as vagas — usado no aviso antes de confirmar.
    premios_se_comecar_agora = _premios_ajustados_para(torneio, times_pagos) if pode_gerar_chaveamento else torneio.premios
    premio_pendente = bool(_premios_pendentes(torneio))
    repasse_pendente = torneio.repasse_federacao_status not in ("concluido", "sem_lucro", "abaixo_minimo")
    # Só depois de encerrado: com a disputa de 3º lugar ainda em aberto, o
    # campeão já é conhecido mas o campeonato não acabou — e pagar apaga os
    # prints, que a disputa ainda vai precisar.
    pode_pagar_premio = (
        eh_organizador
        and torneio.status == "finalizado"
        and torneio.time_campeao_id
        and (premio_pendente or repasse_pendente)
    )
    eh_membro_campeao = (
        current_user.is_authenticated
        and torneio.time_campeao_id is not None
        and current_user.time_id == torneio.time_campeao_id
    )

    grupos_info = []
    if torneio.formato == "fase_grupos" and torneio.status != "aberto":
        for grupo in Grupo.query.filter_by(torneio_id=torneio.id).order_by(Grupo.nome).all():
            confrontos_grupo = (
                Confronto.query
                .filter_by(torneio_id=torneio.id, fase=grupo.nome)
                .order_by(Confronto.ordem)
                .all()
            )
            grupos_info.append({
                "grupo": grupo,
                "confrontos": confrontos_grupo,
                "tabela": _classificacao_grupo(grupo),
            })

    return render_template(
        "campeonato_detalhe.html",
        torneio=torneio,
        time_ja_inscrito=time_ja_inscrito,
        inscricao_pendente=inscricao_pendente,
        eh_organizador=eh_organizador,
        pode_gerar_chaveamento=pode_gerar_chaveamento,
        times_pagos=times_pagos,
        premios_se_comecar_agora=premios_se_comecar_agora,
        total_premios_se_comecar_agora=round(sum(premios_se_comecar_agora.values()), 2),
        status_premios=_status_premios(torneio),
        pode_pagar_premio=pode_pagar_premio,
        eh_membro_campeao=eh_membro_campeao,
        grupos_info=grupos_info,
        rodadas_bracket=_estrutura_bracket(torneio) if torneio.status != "aberto" else None,
        disputa_terceiro=Confronto.query.filter_by(torneio_id=torneio.id, eh_disputa_3_lugar=True).first(),
        colocacoes=_colocacoes_do_torneio(torneio) if torneio.status == "finalizado" else {},
        destaques=_destaques_do_torneio(torneio) if torneio.status == "finalizado" else None,
        xp_destaque=XP_DESTAQUE if len(torneio.times_inscritos) >= 4 else 0,
        tem_contestacao_aberta=_tem_contestacao_aberta(torneio) if eh_organizador else False,
    )


def _pode_administrar_torneio(torneio):
    """Quem pode mexer num campeonato: qualquer admin da federação dona dele,
    ou o admin do site.

    O admin do site entra aqui porque ele precisa conseguir intervir em
    campeonato de qualquer federação — excluir um que não fechou os times,
    remover e reembolsar um time, destravar uma partida — sem depender do
    organizador estar disponível.
    """
    if not current_user.is_authenticated:
        return False
    return current_user.email == ADMIN_EMAIL or current_user.federacao_id == torneio.federacao_id


def _vagas_ocupadas(torneio, ignorar_inscricao_id=None):
    """Vagas que já não estão disponíveis: as pagas MAIS as com Pix gerado e
    ainda dentro do prazo de pagamento.

    Contar só as pagas (como era antes) deixava qualquer quantidade de times
    gerar Pix ao mesmo tempo num campeonato de 4 vagas — se 6 pagassem, o
    chaveamento nunca mais era gerado (ele exige o número exato de times) e
    o dinheiro dos que sobraram ficava preso. Pix vencido não segura vaga.

    `ignorar_inscricao_id` tira uma inscrição da conta. É usado ao confirmar
    um pagamento: a inscrição que está sendo paga já ocupa uma vaga como
    "pendente", então sem ignorá-la o último time a completar o campeonato
    via a própria vaga como se fosse de outro e era recusado (e reembolsado)
    sem motivo nenhum.
    """
    agora = datetime.utcnow()
    return sum(
        1
        for i in torneio.inscricoes
        if i.id != ignorar_inscricao_id
        and (
            i.status == "pago"
            or (i.status == "pendente" and i.pix_expira_em and i.pix_expira_em > agora)
        )
    )


@app.route("/campeonatos/<int:torneio_id>/inscrever", methods=["POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def inscrever_time(torneio_id):
    torneio = Torneio.query.get_or_404(torneio_id)

    if not current_user.time:
        flash("Você precisa ter um time para inscrever no campeonato.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if current_user.time.capitao_id != current_user.id:
        flash("Só o capitão pode inscrever o time num campeonato.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if torneio.status != "aberto":
        flash("As inscrições desse campeonato já estão fechadas.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if current_user.time in torneio.times_inscritos:
        flash("Seu time já está inscrito nesse campeonato.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    inscricao_existente = Inscricao.query.filter_by(
        torneio_id=torneio.id, time_id=current_user.time.id
    ).first()
    # Só manda de volta pra cobrança existente se ela ainda dá pra pagar.
    # Antes, com o Pix vencido (30 min), o time era devolvido pro mesmo QR
    # Code morto pra sempre — sem nunca gerar um novo e sem conseguir
    # entrar no campeonato. Vencido, cai no fluxo de baixo e gera outro.
    pix_ainda_vale = (
        inscricao_existente
        and inscricao_existente.status == "pendente"
        and inscricao_existente.pix_expira_em
        and inscricao_existente.pix_expira_em > datetime.utcnow()
    )
    if pix_ainda_vale:
        return redirect(url_for("pagamento_pix", torneio_id=torneio_id, inscricao_id=inscricao_existente.id))

    if _vagas_ocupadas(torneio) >= torneio.max_times:
        flash("As vagas desse campeonato já estão preenchidas.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    # Campeonato gratuito: confirma a vaga na hora, sem passar pelo Pix.
    if torneio.valor_inscricao <= 0:
        inscricao = Inscricao(
            torneio_id=torneio.id,
            time_id=current_user.time.id,
            status="pago",
            pago_em=datetime.utcnow(),
        )
        db.session.add(inscricao)
        db.session.commit()
        flash(f"{current_user.time.nome} inscrito no {torneio.nome}!", "sucesso")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    # Campeonato pago: cria a inscrição pendente e gera o Pix na AbacatePay.
    inscricao = inscricao_existente or Inscricao(torneio_id=torneio.id, time_id=current_user.time.id)
    inscricao.status = "pendente"
    db.session.add(inscricao)
    db.session.commit()

    try:
        pix = pagamentos.criar_pix(
            valor_reais=torneio.valor_inscricao,
            descricao=f"Inscrição {current_user.time.nome} - {torneio.nome}",
            external_id=f"inscricao-{inscricao.id}",
        )
    except pagamentos.PagamentoIndisponivel as erro:
        db.session.delete(inscricao)
        db.session.commit()
        flash(f"Não deu para gerar o Pix agora: {erro}", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    inscricao.pix_id = pix.get("id")
    inscricao.pix_br_code = pix.get("brCode")
    inscricao.pix_br_code_base64 = pix.get("brCodeBase64")
    inscricao.pix_expira_em = datetime.utcnow() + timedelta(seconds=1800)
    db.session.commit()

    return redirect(url_for("pagamento_pix", torneio_id=torneio_id, inscricao_id=inscricao.id))


@app.route("/campeonatos/<int:torneio_id>/inscricao/<int:inscricao_id>/pix")
@login_required
def pagamento_pix(torneio_id, inscricao_id):
    inscricao = Inscricao.query.get_or_404(inscricao_id)
    if inscricao.torneio_id != torneio_id:
        abort(404)
    if current_user.id != inscricao.time.capitao_id:
        flash("Só o capitão do time pode ver essa cobrança.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if inscricao.status == "pago":
        flash("Essa inscrição já está paga.", "sucesso")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    return render_template(
        "pagamento_pix.html",
        inscricao=inscricao,
        torneio=inscricao.torneio,
        modo_teste=bool(os.environ.get("ABACATEPAY_MODO_TESTE")),
        pix_vencido=bool(inscricao.pix_expira_em and inscricao.pix_expira_em <= datetime.utcnow()),
    )


@app.route("/campeonatos/<int:torneio_id>/inscricao/<int:inscricao_id>/verificar", methods=["POST"])
@login_required
def verificar_pagamento(torneio_id, inscricao_id):
    inscricao = Inscricao.query.get_or_404(inscricao_id)
    if inscricao.torneio_id != torneio_id:
        abort(404)
    if current_user.id != inscricao.time.capitao_id:
        flash("Só o capitão do time pode verificar essa cobrança.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if inscricao.status != "pago":
        try:
            status = pagamentos.checar_pix(inscricao.pix_id)
        except pagamentos.PagamentoIndisponivel as erro:
            flash(f"Não deu para consultar o Pix agora: {erro}", "erro")
            return redirect(url_for("pagamento_pix", torneio_id=torneio_id, inscricao_id=inscricao_id))

        if status.get("status") == "PAID":
            recusa = _motivo_para_recusar_vaga(inscricao)
            if recusa:
                # O dinheiro entrou, mas a vaga não existe mais. Não dá pra
                # fingir que inscreveu — marca pra devolução e avisa.
                inscricao.status = "pago"
                inscricao.pago_em = datetime.utcnow()
                db.session.commit()
                mensagem, erro_reembolso = _reembolsar_inscricao(inscricao)
                db.session.commit()
                flash(f"{recusa} {mensagem or erro_reembolso}", "erro")
                return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

            inscricao.status = "pago"
            inscricao.pago_em = datetime.utcnow()
            db.session.commit()
        else:
            flash("Ainda não identificamos o pagamento. Assim que cair, clica em verificar de novo.", "erro")
            return redirect(url_for("pagamento_pix", torneio_id=torneio_id, inscricao_id=inscricao_id))

    flash(f"Pagamento confirmado! {inscricao.time.nome} inscrito no {inscricao.torneio.nome}.", "sucesso")
    return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))


@app.route("/campeonatos/<int:torneio_id>/inscricao/<int:inscricao_id>/simular", methods=["POST"])
@login_required
def simular_pagamento_pix(torneio_id, inscricao_id):
    """Só existe com ABACATEPAY_MODO_TESTE=1 — chama o simulate-payment da AbacatePay
    (sandbox) para testar o fluxo inteiro sem precisar pagar um Pix de verdade."""
    if not os.environ.get("ABACATEPAY_MODO_TESTE"):
        abort(404)

    inscricao = Inscricao.query.get_or_404(inscricao_id)
    if inscricao.torneio_id != torneio_id or current_user.id != inscricao.time.capitao_id:
        abort(404)

    try:
        pagamentos.simular_pagamento(inscricao.pix_id)
    except pagamentos.PagamentoIndisponivel as erro:
        flash(f"Não deu para simular o pagamento: {erro}", "erro")
        return redirect(url_for("pagamento_pix", torneio_id=torneio_id, inscricao_id=inscricao_id))

    return redirect(url_for("verificar_pagamento", torneio_id=torneio_id, inscricao_id=inscricao_id), code=307)


# Chave pública fixa da AbacatePay, publicada na documentação deles
# (docs.abacatepay.com/pages/webhooks/security) — é a mesma para qualquer
# integração, não é um segredo nosso. Usada só para conferir a assinatura
# HMAC do corpo do webhook (prova que o evento veio mesmo da AbacatePay).
ABACATEPAY_PUBLIC_KEY = (
    "t9dXRhHHo3yDEj5pVDYz0frf7q6bMKyMRmxxCPIPp3RCplBfXRxqlC6ZpiWmOqj4L63qEaeUOtrCI8P0VMUgo6iIga2ri9og"
    "aHFs0WIIywSMg0q7RmBfybe1E5XJcfC4IW3alNqym0tXoAKkzvfEjZxV6bE0oG2zJrNNYmUCKZyV0KZ3JS8Votf9EAWWYdiD"
    "kMkpbMdPggfh1EqHlVkMiTady6jOR3hyzGEHrIz2Ret0xHKMbiqkr9HS1JhNHDX9"
)


@app.route("/webhooks/abacatepay", methods=["POST"])
@csrf.exempt
def webhook_abacatepay():
    """Recebe a confirmação automática de pagamento da AbacatePay.

    Duas camadas de validação, conforme a documentação deles:
    1. O "webhookSecret" que a gente mesmo escolheu ao cadastrar o
       webhook no painel — vem na query string da URL chamada.
    2. A assinatura HMAC-SHA256 (header X-Webhook-Signature) do corpo
       bruto da requisição, calculada com a chave pública da AbacatePay
       (ABACATEPAY_PUBLIC_KEY acima) e comparada em base64.
    Sem as duas batendo, a chamada é recusada; isso não afeta o fluxo
    manual do botão "já paguei".
    """
    secret_configurado = app.config["ABACATEPAY_WEBHOOK_SECRET"]
    secret_recebido = request.args.get("webhookSecret", "")

    if not secret_configurado or not hmac.compare_digest(secret_recebido, secret_configurado):
        abort(401)

    assinatura_recebida = request.headers.get("X-Webhook-Signature", "")
    assinatura_esperada = base64.b64encode(
        hmac.new(ABACATEPAY_PUBLIC_KEY.encode(), request.get_data(), hashlib.sha256).digest()
    ).decode()
    if not assinatura_recebida or not hmac.compare_digest(assinatura_recebida, assinatura_esperada):
        abort(401)

    payload = request.get_json(silent=True) or {}
    evento = payload.get("event")
    dados = payload.get("data", {})

    if evento == "transparent.completed":
        # O id da cobrança vem aninhado em data.transparent.id, não em
        # data.id — conferido com um payload real da AbacatePay via ngrok.
        pix_id = dados.get("transparent", {}).get("id")
        inscricao = Inscricao.query.filter_by(pix_id=pix_id).first()
        if inscricao and inscricao.status != "pago":
            recusa = _motivo_para_recusar_vaga(inscricao)
            inscricao.status = "pago"
            inscricao.pago_em = datetime.utcnow()
            db.session.commit()

            if recusa:
                # Pagou depois de fechar/lotar: devolve na hora em vez de
                # ficar com o dinheiro de uma vaga que não existe mais.
                _, erro = _reembolsar_inscricao(inscricao)
                db.session.commit()
                if erro:
                    app.logger.error(f"Reembolso automático falhou (inscrição {inscricao.id}): {erro}")

    return jsonify({"recebido": True})


FASES_POR_TAMANHO = {
    2: "Final",
    4: "Semifinal",
    8: "Quartas de final",
    16: "Oitavas de final",
    32: "Fase de 32",
}


def _vencedor_confronto(confronto):
    if confronto.status == "validado":
        return confronto.vencedor
    if confronto.status == "wo":
        return confronto.vencedor_wo
    return None


def _avancar_chaveamento(confronto):
    """Depois que um confronto termina (placar ou W.O.), decide se precisa
    avançar alguma coisa — despacha para fase de grupos ou para o mata-mata
    dependendo de qual fase esse confronto pertence."""
    if confronto.fase.startswith("Grupo "):
        _avancar_grupo(confronto)
    else:
        _avancar_mata_mata(confronto)


def _avancar_mata_mata(confronto):
    """Depois que um confronto do mata-mata termina (placar ou W.O.), confere
    se a fase inteira já foi decidida. Se sim, sorteia os vencedores em pares
    para próxima fase — ou, se essa era a última (só 1 confronto), declara o
    campeão automaticamente.

    Se algum confronto da fase terminou em W.O. duplo (sem vencedor), a fase
    fica travada até o organizador resolver isso manualmente.
    """
    torneio = confronto.torneio

    # A disputa de 3º lugar não alimenta fase nenhuma: terminar ela só pode
    # (no máximo) liberar o encerramento do campeonato.
    if confronto.eh_disputa_3_lugar:
        _tentar_finalizar_torneio(torneio)
        return

    confrontos_fase = (
        Confronto.query
        .filter_by(torneio_id=torneio.id, fase=confronto.fase, eh_disputa_3_lugar=False)
        .order_by(Confronto.ordem)
        .all()
    )
    if any(c.status not in ("validado", "wo") for c in confrontos_fase):
        return

    vencedores = [_vencedor_confronto(c) for c in confrontos_fase]
    if any(v is None for v in vencedores):
        return

    if len(vencedores) == 1:
        # Final decidida: o campeão fica conhecido na hora, mas o campeonato
        # só encerra quando a disputa de 3º lugar (se existir) também
        # terminar — ver _tentar_finalizar_torneio.
        torneio.time_campeao_id = vencedores[0].id
        _tentar_finalizar_torneio(torneio)
        return

    proxima_fase = FASES_POR_TAMANHO.get(len(vencedores), f"Fase de {len(vencedores)}")

    # Se a próxima fase já foi montada antes, não monta de novo — senão um
    # resultado corrigido depois (W.O. revisto, placar resolvido na mão)
    # criaria uma SEGUNDA final/semi em paralelo com a primeira, e os dois
    # jogos valeriam ao mesmo tempo.
    if Confronto.query.filter_by(torneio_id=torneio.id, fase=proxima_fase).first():
        return

    for ordem, i in enumerate(range(0, len(vencedores), 2)):
        db.session.add(Confronto(
            torneio_id=torneio.id,
            fase=proxima_fase,
            ordem=ordem,
            time1_id=vencedores[i].id,
            time2_id=vencedores[i + 1].id,
        ))

    if proxima_fase == "Final":
        _criar_disputa_terceiro_lugar(torneio, confrontos_fase)


FASE_DISPUTA_TERCEIRO = "Disputa de 3º lugar"


def _perdedor_confronto(confronto):
    """O time que perdeu — ou None se o confronto não teve adversário (bye)
    ou não tem vencedor definido (W.O. duplo)."""
    vencedor = _vencedor_confronto(confronto)
    if vencedor is None or confronto.time1_id is None or confronto.time2_id is None:
        return None
    return confronto.time2 if vencedor.id == confronto.time1_id else confronto.time1


def _criar_disputa_terceiro_lugar(torneio, semifinais):
    """Monta o jogo de 3º lugar entre os dois perdedores da semifinal, junto
    com a final.

    Só existe quando as DUAS semifinais tiveram um perdedor de verdade. Num
    campeonato de 3 times numa chave de 4, uma semifinal é bye (o time que
    sobrou nunca jogou) — aí só tem um perdedor real, que já fica em 3º sem
    precisar jogar (ver _colocacoes_do_torneio).
    """
    perdedores = [_perdedor_confronto(c) for c in semifinais]
    if len(perdedores) != 2 or any(p is None for p in perdedores):
        return
    if Confronto.query.filter_by(torneio_id=torneio.id, eh_disputa_3_lugar=True).first():
        return
    db.session.add(Confronto(
        torneio_id=torneio.id,
        fase=FASE_DISPUTA_TERCEIRO,
        ordem=0,
        time1_id=perdedores[0].id,
        time2_id=perdedores[1].id,
        eh_disputa_3_lugar=True,
    ))


def _tentar_finalizar_torneio(torneio):
    """Encerra o campeonato quando não sobra jogo nenhum: final decidida E
    disputa de 3º lugar (se existir) terminada. Encerrar é o que libera o
    pagamento do prêmio, apaga os prints (campeonato grátis) e registra os
    pontos do ranking — tudo isso precisa que todas as colocações já
    estejam definidas."""
    if torneio.status == "finalizado" or not torneio.time_campeao_id:
        return
    disputa = Confronto.query.filter_by(torneio_id=torneio.id, eh_disputa_3_lugar=True).first()
    if disputa and disputa.status not in ("validado", "wo"):
        return

    torneio.status = "finalizado"
    notificar_federacao(
        torneio,
        "campeonato_finalizado",
        f'O campeonato "{torneio.nome}" foi finalizado — {torneio.time_campeao.nome} é o campeão!',
    )
    _registrar_pontos_ranking(torneio)
    _creditar_xp(torneio)
    # Campeonato sem dinheiro envolvido não tem tela de "confirmar
    # pagamentos" — então é aqui mesmo que os prints saem, senão eles
    # ficariam acumulando no volume pra sempre.
    if torneio.valor_inscricao <= 0 and not torneio.premios:
        _limpar_prints_do_torneio(torneio)


def _destaques_do_torneio(torneio):
    """Melhor ataque (mais gols marcados) e melhor defesa (menos gols
    sofridos) do campeonato, contando só partidas validadas com placar
    real (W.O. não tem gols). Empate leva os dois. Devolve
    {"ataque": [(time, gols)], "defesa": [(time, gols_sofridos)]} — listas
    vazias se ninguém jogou partida validada."""
    gols_pro, gols_contra = Counter(), Counter()
    jogaram = set()
    for confronto in torneio.confrontos:
        if confronto.status != "validado" or confronto.time1 is None or confronto.time2 is None:
            continue
        if confronto.placar_time1 is None or confronto.placar_time2 is None:
            continue
        for time_obj, feitos, sofridos in (
            (confronto.time1, confronto.placar_time1, confronto.placar_time2),
            (confronto.time2, confronto.placar_time2, confronto.placar_time1),
        ):
            jogaram.add(time_obj)
            gols_pro[time_obj.id] += feitos
            gols_contra[time_obj.id] += sofridos
    if not jogaram:
        return {"ataque": [], "defesa": []}
    maior = max(gols_pro[t.id] for t in jogaram)
    menor = min(gols_contra[t.id] for t in jogaram)
    return {
        "ataque": sorted(((t, gols_pro[t.id]) for t in jogaram if gols_pro[t.id] == maior), key=lambda x: x[0].nome),
        "defesa": sorted(((t, gols_contra[t.id]) for t in jogaram if gols_contra[t.id] == menor), key=lambda x: x[0].nome),
    }


def _creditar_xp(torneio):
    """XP de carreira (ver PLANO_XP_NIVEL.md), creditado ao time e a cada
    jogador do elenco naquele momento. Idempotente via Torneio.xp_creditado.

    - Prêmio: 1 real de prêmio = 1 XP, pra cada colocação premiada.
    - Destaque: melhor ataque e melhor defesa ganham XP_DESTAQUE cada — só
      em campeonato com pelo menos 4 times, pra não dar pra farmar XP com
      campeonato grátis de 2 times.
    """
    if torneio.xp_creditado:
        return
    torneio.xp_creditado = True

    def creditar(time_obj, xp):
        time_obj.xp_total = (time_obj.xp_total or 0) + xp
        for membro in time_obj.membros:
            membro.xp_total = (membro.xp_total or 0) + xp

    colocacoes = _colocacoes_do_torneio(torneio)
    for colocacao, valor in torneio.premios.items():
        time_obj = colocacoes.get(colocacao)
        if time_obj is not None:
            creditar(time_obj, int(round(valor)))

    if len(torneio.times_inscritos) >= 4:
        destaques = _destaques_do_torneio(torneio)
        for time_obj, _ in destaques["ataque"] + destaques["defesa"]:
            creditar(time_obj, XP_DESTAQUE)


def _colocacoes_do_torneio(torneio):
    """{colocacao: time} com campeão, vice e terceiro de um campeonato
    encerrado. Qualquer um pode faltar (chave de 2 não tem 3º; W.O. duplo
    na disputa de 3º deixa a vaga vazia)."""
    colocacoes = {}
    if torneio.time_campeao:
        colocacoes["campeao"] = torneio.time_campeao

    final = Confronto.query.filter_by(torneio_id=torneio.id, fase="Final", eh_disputa_3_lugar=False).first()
    if final is not None:
        vice = _perdedor_confronto(final)
        if vice is not None:
            colocacoes["vice"] = vice

    disputa = Confronto.query.filter_by(torneio_id=torneio.id, eh_disputa_3_lugar=True).first()
    if disputa is not None:
        terceiro = _vencedor_confronto(disputa)
        if terceiro is not None:
            colocacoes["terceiro"] = terceiro
    else:
        # Sem disputa: só rola quando uma semifinal foi bye. O único
        # perdedor real de semifinal é o 3º.
        semifinais = Confronto.query.filter_by(torneio_id=torneio.id, fase="Semifinal", eh_disputa_3_lugar=False).all()
        perdedores = [p for p in (_perdedor_confronto(c) for c in semifinais) if p is not None]
        if len(perdedores) == 1:
            colocacoes["terceiro"] = perdedores[0]

    return colocacoes


def _registrar_pontos_ranking(torneio):
    """Credita os pontos da temporada (campeão 3, vice 2, terceiro 1) num
    campeonato elegível recém-encerrado. Idempotente: se já tem ponto
    registrado pra esse campeonato, não registra de novo. Temporada já
    encerrada não recebe ponto — o prêmio dela já foi decidido."""
    if not torneio.elegivel_ranking or not torneio.temporada_id:
        return
    if torneio.temporada is None or torneio.temporada.status != "aberta":
        return
    if PontuacaoRanking.query.filter_by(torneio_id=torneio.id).first():
        return
    for colocacao, time_obj in _colocacoes_do_torneio(torneio).items():
        db.session.add(PontuacaoRanking(
            temporada_id=torneio.temporada_id,
            torneio_id=torneio.id,
            time_id=time_obj.id,
            colocacao=colocacao,
            pontos=PONTOS_POR_COLOCACAO[colocacao],
        ))


def _temporada_aberta():
    return Temporada.query.filter_by(status="aberta").order_by(Temporada.numero.desc()).first()


def _classificacao_grupo(grupo):
    """Tabela de classificação de um grupo: pontos (3 por vitória, 1 por
    empate — só os jogos de grupo podem empatar), saldo de gols e gols
    marcados, nessa ordem de desempate. Cada item é um dict com o time e as
    estatísticas."""
    confrontos = Confronto.query.filter_by(torneio_id=grupo.torneio_id, fase=grupo.nome).all()
    inscricoes = Inscricao.query.filter_by(grupo_id=grupo.id).all()

    tabela = {
        i.time_id: {"time": i.time, "pontos": 0, "vitorias": 0, "empates": 0, "gols_pro": 0, "gols_contra": 0}
        for i in inscricoes if i.time is not None
    }

    for confronto in confrontos:
        if confronto.status == "validado":
            if confronto.time1_id in tabela:
                tabela[confronto.time1_id]["gols_pro"] += confronto.placar_time1
                tabela[confronto.time1_id]["gols_contra"] += confronto.placar_time2
            if confronto.time2_id in tabela:
                tabela[confronto.time2_id]["gols_pro"] += confronto.placar_time2
                tabela[confronto.time2_id]["gols_contra"] += confronto.placar_time1
            if confronto.vencedor_id in tabela:
                tabela[confronto.vencedor_id]["pontos"] += 3
                tabela[confronto.vencedor_id]["vitorias"] += 1
            elif confronto.vencedor_id is None:
                if confronto.time1_id in tabela:
                    tabela[confronto.time1_id]["pontos"] += 1
                    tabela[confronto.time1_id]["empates"] += 1
                if confronto.time2_id in tabela:
                    tabela[confronto.time2_id]["pontos"] += 1
                    tabela[confronto.time2_id]["empates"] += 1
        elif confronto.status == "wo" and confronto.vencedor_wo_id in tabela:
            tabela[confronto.vencedor_wo_id]["pontos"] += 3
            tabela[confronto.vencedor_wo_id]["vitorias"] += 1

    linhas = list(tabela.values())
    for linha in linhas:
        linha["saldo"] = linha["gols_pro"] - linha["gols_contra"]
    linhas.sort(key=lambda linha: (-linha["pontos"], -linha["saldo"], -linha["gols_pro"]))
    return linhas


def _grupo_encerrado(grupo):
    confrontos = Confronto.query.filter_by(torneio_id=grupo.torneio_id, fase=grupo.nome).all()
    return bool(confrontos) and all(c.status in ("validado", "wo") for c in confrontos)


def _gerar_grupos(torneio, times):
    """Sorteia os times em grupos de 4 e cria os confrontos de todos-contra-
    todos (6 jogos por grupo) de cada um."""
    random.shuffle(times)
    letras = string.ascii_uppercase
    num_grupos = len(times) // 4

    for i in range(num_grupos):
        grupo = Grupo(torneio_id=torneio.id, nome=f"Grupo {letras[i]}")
        db.session.add(grupo)
        db.session.flush()

        times_grupo = times[i * 4:(i + 1) * 4]
        for time in times_grupo:
            inscricao = Inscricao.query.filter_by(torneio_id=torneio.id, time_id=time.id).first()
            inscricao.grupo_id = grupo.id

        ordem = 0
        for a in range(4):
            for b in range(a + 1, 4):
                db.session.add(Confronto(
                    torneio_id=torneio.id,
                    fase=grupo.nome,
                    ordem=ordem,
                    time1_id=times_grupo[a].id,
                    time2_id=times_grupo[b].id,
                ))
                ordem += 1


def _avancar_grupo(confronto):
    """Depois que um jogo de grupo termina, confere se TODOS os grupos do
    campeonato já encerraram. Se sim, pega os 2 primeiros de cada grupo e
    monta a primeira fase do mata-mata, cruzando os grupos (1º de um grupo
    contra 2º do grupo seguinte) para evitar reencontro imediato entre times
    do mesmo grupo.
    """
    torneio = confronto.torneio
    grupos = Grupo.query.filter_by(torneio_id=torneio.id).order_by(Grupo.nome).all()
    if not all(_grupo_encerrado(grupo) for grupo in grupos):
        return

    primeiros, segundos = [], []
    for grupo in grupos:
        tabela = _classificacao_grupo(grupo)
        if len(tabela) < 2:
            return
        primeiros.append(tabela[0]["time"])
        segundos.append(tabela[1]["time"])

    n = len(grupos)
    if n == 1:
        pares = [(primeiros[0], segundos[0])]
    else:
        pares = [(primeiros[i], segundos[(i + 1) % n]) for i in range(n)]

    fase_mata_mata = FASES_POR_TAMANHO.get(len(pares) * 2, f"Fase de {len(pares) * 2}")
    for ordem, (time1, time2) in enumerate(pares):
        db.session.add(Confronto(
            torneio_id=torneio.id,
            fase=fase_mata_mata,
            ordem=ordem,
            time1_id=time1.id,
            time2_id=time2.id,
        ))

    notificar_federacao(
        torneio,
        "campeonato_iniciado",
        f'A fase de grupos do "{torneio.nome}" terminou — mata-mata começou ({fase_mata_mata}).',
    )


PREMIO_CAMPOS = {
    # colocação -> (campo do valor, campo do status, campo do id da transferência, rótulo)
    "campeao": ("valor_premio", "premio_status", "premio_transferencia_id", "campeão"),
    "vice": ("valor_premio_vice", "premio_vice_status", "premio_vice_transferencia_id", "vice"),
    "terceiro": ("valor_premio_terceiro", "premio_terceiro_status", "premio_terceiro_transferencia_id", "3º lugar"),
}


def _custo_premios(valores):
    """Quanto sai da conta pra pagar esses prêmios: cada um custa valor +
    taxa de envio da AbacatePay. Prêmio zero não custa nada."""
    return sum(v + TAXA_ABACATEPAY_ESTIMADA for v in valores if v and v > 0)


def _status_premios(torneio):
    """{colocacao: (valor, status)} dos prêmios acima de zero."""
    return {
        c: (getattr(torneio, campos[0]) or 0, getattr(torneio, campos[1]))
        for c, campos in PREMIO_CAMPOS.items()
        if (getattr(torneio, campos[0]) or 0) > 0
    }


def _premios_pendentes(torneio):
    """Colocações com prêmio ainda não resolvido (nem pago, nem a caminho,
    nem sem ninguém pra receber)."""
    return [
        c for c, (valor, status) in _status_premios(torneio).items()
        if status not in ("concluido", "sem_colocado")
    ]


def _premios_ajustados_para(torneio, num_times):
    """Prêmios possíveis com `num_times` pagando, em vez das vagas todas.

    Os prêmios foram calibrados na criação pra sobrar o lucro mínimo da
    plataforma com o campeonato CHEIO. Começando com menos times entra menos
    dinheiro, e manter os prêmios cheios faria a plataforma pagar a
    diferença do próprio bolso. Aqui a gente refaz a mesma conta da criação,
    só que com o número real de times, e reduz os três prêmios na mesma
    proporção até caber no piso (nunca aumenta — no máximo mantém o que já
    estava combinado). Prêmio que cair abaixo do mínimo de Pix vira zero.

    Devolve {colocacao: valor} só com os prêmios acima de zero.
    """
    premios = dict(torneio.premios)
    if not premios or num_times >= torneio.max_times:
        return premios

    leituras = (num_times - 1) * 2 * 1.2
    custo_ia = leituras * CUSTO_IA_POR_LEITURA_ESTIMADO
    arrecadacao = num_times * max(torneio.valor_inscricao - TAXA_ABACATEPAY_ESTIMADA, 0)

    # Da fórmula do lucro da plataforma:
    #   (arrecadacao - custo_premios) * (1 - fatia) - taxa - ia = lucro_minimo
    # com custo_premios = soma(premio + taxa) — a taxa de envio de cada
    # prêmio entra no custo, então sai do "teto" antes de dividir.
    base_necessaria = (LUCRO_MINIMO_PLATAFORMA + TAXA_ABACATEPAY_ESTIMADA + custo_ia) / (1 - _fatia_federacao(torneio.federacao))
    teto_total = arrecadacao - base_necessaria - len(premios) * TAXA_ABACATEPAY_ESTIMADA
    total_atual = sum(premios.values())
    if teto_total >= total_atual:
        return premios

    fator = max(teto_total, 0) / total_atual
    ajustados = {}
    for colocacao, valor in premios.items():
        novo = round(valor * fator, 2)
        if novo >= VALOR_MINIMO_PIX_ABACATEPAY:
            ajustados[colocacao] = novo
    return ajustados


def _estrutura_bracket(torneio):
    """Monta a chave inteira dividida em dois lados que se encontram na
    final — do jeito de chave de campeonato de verdade (metade dos times de
    cada lado), mesmo pras rodadas que ainda não foram sorteadas.

    A divisão por lado usa a metade do `ordem` de cada rodada: como
    `_avancar_chaveamento` sempre junta os vencedores em pares consecutivos
    (0+1 -> nova rodada 0, 2+3 -> nova rodada 1, ...), a primeira metade dos
    confrontos de uma rodada sempre desce para primeira metade da próxima —
    ou seja, um lado nunca "vaza" para o outro em nenhuma rodada.

    Retorna um dict com "esquerda" e "direita" (listas de (fase, [confronto
    ou None]), da rodada mais cheia até a penúltima) e "final" (a última
    rodada, sempre com 1 confronto só). Para campeonato de 2 times, esquerda
    e direita ficam vazios — só tem a final mesmo.
    """
    confrontos_por_fase = {}
    for confronto in torneio.confrontos:
        confrontos_por_fase.setdefault(confronto.fase, {})[confronto.ordem] = confronto

    if torneio.formato == "fase_grupos":
        tamanho = Grupo.query.filter_by(torneio_id=torneio.id).count() * 2
    else:
        # O tamanho real da chave vem da rodada mais cheia que existe, não de
        # max_times: o campeonato pode ter começado com menos times do que as
        # vagas, e aí a chave é menor (ex: 16 vagas, 6 times -> chave de 8).
        maior_rodada = max((len(c) for c in confrontos_por_fase.values()), default=0)
        tamanho = maior_rodada * 2 if maior_rodada else torneio.max_times
    if tamanho < 2:
        return None

    rodadas = []
    while tamanho >= 2:
        fase = FASES_POR_TAMANHO.get(tamanho, f"Fase de {tamanho}")
        existentes = confrontos_por_fase.get(fase, {})
        vagas = [existentes.get(i) for i in range(tamanho // 2)]
        rodadas.append((fase, vagas))
        tamanho //= 2

    final = rodadas[-1]
    esquerda, direita = [], []
    for fase, vagas in rodadas[:-1]:
        meio = len(vagas) // 2
        esquerda.append((fase, vagas[:meio]))
        direita.append((fase, vagas[meio:]))

    return {"esquerda": esquerda, "final": final, "direita": direita}


@app.route("/campeonatos/<int:torneio_id>/gerar-chaveamento", methods=["POST"])
@login_required
def gerar_chaveamento(torneio_id):
    torneio = Torneio.query.get_or_404(torneio_id)

    if not _pode_administrar_torneio(torneio):
        flash("Só um admin da federação pode gerar o chaveamento.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if torneio.status != "aberto":
        flash("O chaveamento desse campeonato já foi gerado.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    times = list(torneio.times_inscritos)
    if len(times) < 2:
        flash(
            f"Precisa de pelo menos 2 times pagos para começar o campeonato (hoje tem {len(times)}).",
            "erro",
        )
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if torneio.formato == "fase_grupos" and len(times) % 4 != 0:
        flash(
            f"Campeonato de fase de grupos precisa de um número de times múltiplo de 4 "
            f"(hoje tem {len(times)}). Complete as vagas ou remova algum time.",
            "erro",
        )
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    # Começando com menos times do que as vagas, entra menos dinheiro — então
    # o prêmio cai junto, senão a plataforma banca a diferença do próprio
    # bolso. O organizador é avisado disso antes de confirmar (ver o popup na
    # tela do campeonato).
    premios_antigos = torneio.premios
    premios_novos = _premios_ajustados_para(torneio, len(times))
    if premios_novos != premios_antigos:
        for colocacao, campos in PREMIO_CAMPOS.items():
            setattr(torneio, campos[0], premios_novos.get(colocacao, 0))

    # Ranking: o campeonato entra na temporada aberta neste momento (é
    # quando ele de fato acontece) e a elegibilidade fica travada aqui —
    # pago e cheio. Depois disso não muda mais, mesmo que a temporada
    # feche ou um time seja removido.
    temporada = _temporada_aberta()
    torneio.temporada_id = temporada.id if temporada else None
    torneio.elegivel_ranking = bool(
        temporada and torneio.valor_inscricao > 0 and len(times) >= torneio.max_times
    )

    if torneio.formato == "fase_grupos":
        _gerar_grupos(torneio, times)
        descricao_fase = f"{len(times) // 4} grupo(s) sorteado(s)"
        mensagem_sucesso = f"Grupos sorteados! {descricao_fase}."
    else:
        random.shuffle(times)
        # A chave precisa ser potência de 2. Com menos times que isso, as
        # vagas que sobram ficam vazias e quem cai nelas passa de fase por
        # W.O., sem jogar.
        tamanho_chave = 2
        while tamanho_chave < len(times):
            tamanho_chave *= 2

        # Monta os pares na mão em vez de fatiar a lista com buracos: assim
        # nunca sai uma vaga vazia contra outra vazia (com 5 times numa chave
        # de 8, fatiar deixaria o último confronto sem nenhum time).
        qtd_byes = tamanho_chave - len(times)
        pares = [(time, None) for time in times[:qtd_byes]]
        jogam = times[qtd_byes:]
        pares += [(jogam[i], jogam[i + 1]) for i in range(0, len(jogam), 2)]
        random.shuffle(pares)  # espalha os byes pelos dois lados da chave

        fase = FASES_POR_TAMANHO.get(tamanho_chave, f"Fase de {tamanho_chave}")
        for ordem, (time1, time2) in enumerate(pares):
            confronto = Confronto(
                torneio_id=torneio.id,
                fase=fase,
                ordem=ordem,
                time1_id=time1.id if time1 else None,
                time2_id=time2.id if time2 else None,
            )
            # Sem adversário: já nasce decidido por W.O. pra quem está lá.
            if time2 is None:
                confronto.status = "wo"
                confronto.vencedor_wo_id = time1.id
            db.session.add(confronto)
        byes = qtd_byes

        descricao_fase = f"chaveamento sorteado ({fase})"
        mensagem_sucesso = f"Chaveamento sorteado! Fase inicial: {fase}."
        if byes:
            mensagem_sucesso += (
                f" {len(times)} time(s) para uma chave de {tamanho_chave} — "
                f"{byes} time(s) passam direto de fase por W.O."
            )

    if premios_novos != premios_antigos:
        total_antigo = round(sum(premios_antigos.values()), 2)
        total_novo = round(sum(premios_novos.values()), 2)
        rotulo = "Prêmios ajustados" if len(premios_antigos) > 1 else "Prêmio ajustado"
        mensagem_sucesso += f" {rotulo} de R$ {total_antigo:.2f} para R$ {total_novo:.2f} (começou com menos times)."
        notificar_federacao(
            torneio,
            "campeonato_iniciado",
            f'{rotulo} do "{torneio.nome}": de R$ {total_antigo:.2f} para R$ {total_novo:.2f}, '
            f'porque o campeonato começou com {len(times)} de {torneio.max_times} times.',
        )

    torneio.status = "em_andamento"
    notificar_federacao(
        torneio,
        "campeonato_iniciado",
        f'O campeonato "{torneio.nome}" começou — {descricao_fase}.',
    )
    db.session.commit()

    flash(mensagem_sucesso, "sucesso")
    return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))


FATIA_FEDERACAO_NO_LUCRO = 0.8  # padrão; cada federação pode ter a sua (Federacao.fatia_lucro)


def _fatia_federacao(federacao):
    """Fatia do lucro que vai pra essa federação (o resto é da plataforma)."""
    if federacao is None or federacao.fatia_lucro is None:
        return FATIA_FEDERACAO_NO_LUCRO
    return federacao.fatia_lucro

# Quanto fica retido num reembolso de inscrição: são as DUAS taxas fixas da
# AbacatePay que a plataforma já pagou ou vai pagar nessa ida e volta — a de
# receber o Pix da inscrição e a de enviar o Pix da devolução. Sem descontar
# isso, cada time reembolsado daria prejuízo direto pra plataforma.
TAXA_REEMBOLSO = 1.60


def _tem_contestacao_aberta(torneio):
    """Se alguma partida desse campeonato foi contestada por um dos times."""
    return bool(
        Notificacao.query
        .filter(Notificacao.torneio_id == torneio.id, Notificacao.tipo == "placar_contestado")
        .first()
    )


def _limpar_prints_do_torneio(torneio):
    """Apaga os prints das partidas de um campeonato já encerrado e pago.

    Print é prova de contestação, então só sai depois que o campeonato
    fechou e o dinheiro foi pago — a partir daí não tem mais o que
    contestar (ver Termos de uso: contestação é durante o campeonato).
    Sem essa limpeza, os prints iam se acumulando no volume até enchê-lo,
    e aí nenhum upload do site funcionaria mais.
    """
    apagados = 0
    for confronto in torneio.confrontos:
        for lado in (1, 2):
            nome = confronto.print_time1 if lado == 1 else confronto.print_time2
            if not nome:
                continue
            _apagar_upload("prints", nome)
            if lado == 1:
                confronto.print_time1 = None
            else:
                confronto.print_time2 = None
            apagados += 1
    return apagados


def _motivo_para_recusar_vaga(inscricao):
    """Devolve o motivo (texto) se essa inscrição não pode mais virar vaga, ou None.

    Serve pro caso do Pix cair atrasado: o time gerou a cobrança, demorou, e
    quando pagou o campeonato já tinha começado ou as vagas já tinham
    acabado. Antes disso o pagamento era aceito assim mesmo e o time ficava
    pagando por uma vaga que não existia.
    """
    torneio = inscricao.torneio
    if torneio.status != "aberto":
        return "Esse campeonato não está mais com inscrições abertas."
    # Ignora a própria inscrição: ela já conta como vaga pendente, e sem isso
    # o último time a fechar o campeonato seria recusado na própria vaga.
    if _vagas_ocupadas(torneio, ignorar_inscricao_id=inscricao.id) >= torneio.max_times:
        return "As vagas desse campeonato foram preenchidas antes do seu pagamento cair."
    return None


def _reembolsar_inscricao(inscricao):
    """Devolve a inscrição paga via Pix, descontando TAXA_REEMBOLSO.

    Devolve (mensagem_ou_None, erro_ou_None), igual às outras funções de
    dinheiro. Não faz nada se a inscrição não chegou a ser paga ou se já foi
    reembolsada antes (não pode devolver duas vezes).
    """
    if inscricao.status != "pago" or inscricao.reembolso_status in ("concluido", "pendente"):
        return None, None

    valor = round(inscricao.torneio.valor_inscricao - TAXA_REEMBOLSO, 2)
    if valor < VALOR_MINIMO_PIX_ABACATEPAY:
        # Inscrição barata demais: depois de descontar as taxas não sobra nem
        # o mínimo que a AbacatePay aceita enviar num Pix.
        inscricao.reembolso_status = "sem_saldo"
        inscricao.reembolso_valor = 0
        return None, (
            f"Não deu para reembolsar {inscricao.time.nome}: depois de descontar as taxas "
            f"(R$ {TAXA_REEMBOLSO:.2f}) sobraria menos que o mínimo de Pix da AbacatePay."
        )

    capitao = inscricao.time.capitao
    if not capitao.chave_pix or not capitao.tipo_chave_pix:
        inscricao.reembolso_status = "falhou"
        return None, (
            f"Reembolso de {inscricao.time.nome}: {capitao.nickname} (capitão) ainda não "
            "cadastrou uma chave Pix para receber."
        )

    try:
        transferencia = pagamentos.enviar_pix(
            valor_reais=valor,
            chave_pix=capitao.chave_pix,
            tipo_chave=capitao.tipo_chave_pix,
            descricao=f"Reembolso {inscricao.torneio.nome} - {inscricao.time.nome}",
            external_id=f"reembolso-inscricao-{inscricao.id}",
        )
    except pagamentos.PagamentoIndisponivel as erro:
        inscricao.reembolso_status = "falhou"
        app.logger.error(
            f"Reembolso falhou (inscrição {inscricao.id}, time {inscricao.time.nome}, "
            f"chave {capitao.tipo_chave_pix} {capitao.chave_pix!r}, R$ {valor:.2f}): {erro}"
        )
        return None, f"Reembolso de {inscricao.time.nome}: não deu para transferir agora ({erro})."

    inscricao.reembolso_valor = valor
    inscricao.reembolso_transferencia_id = transferencia.get("id")
    status_abacatepay = (transferencia.get("status") or "").upper()
    inscricao.reembolso_status = "concluido" if status_abacatepay in ("COMPLETED", "PAID") else "pendente"

    return (
        f"Reembolso de R$ {valor:.2f} enviado para {capitao.nickname} ({inscricao.time.nome}) — "
        f"R$ {TAXA_REEMBOLSO:.2f} retidos das taxas de Pix.",
        None,
    )


def _pagar_premio_colocacao(torneio, colocacao, colocacoes):
    """Tenta transferir o prêmio de uma colocação (campeao/vice/terceiro)
    pro capitão do time que ficou nela. Devolve (mensagem_ou_None,
    erro_ou_None) — só um dos dois vem preenchido.

    Não faz nada (None, None) se não tem prêmio pra essa colocação, se já
    foi pago, ou se já tem uma transferência "pendente" — nesse caso a
    AbacatePay já aceitou o envio e só está confirmando; tentar de novo
    criaria um segundo envio real em cima do primeiro. Se a colocação não
    existe (ex.: W.O. duplo na disputa de 3º), marca "sem_colocado" e o
    valor fica na conta — entra no lucro dividido com a federação.
    """
    campo_valor, campo_status, campo_transferencia, rotulo = PREMIO_CAMPOS[colocacao]
    valor = getattr(torneio, campo_valor) or 0
    status = getattr(torneio, campo_status)
    if valor <= 0 or status in ("concluido", "pendente", "sem_colocado"):
        return None, None

    time_obj = colocacoes.get(colocacao)
    if time_obj is None:
        setattr(torneio, campo_status, "sem_colocado")
        return None, None

    capitao = time_obj.capitao
    if capitao is None or not capitao.chave_pix or not capitao.tipo_chave_pix:
        setattr(torneio, campo_status, "falhou")
        quem = capitao.nickname if capitao else "O capitão"
        return None, f"{quem} (capitão do {time_obj.nome}, {rotulo}) ainda não cadastrou uma chave Pix para receber o prêmio."

    # A AbacatePay cobra a mesma taxa fixa também em cima de QUALQUER envio
    # (não só no recebimento) — manda-se valor + taxa pra o time receber o
    # valor cheio anunciado, e quem absorve a taxa é a fatia da plataforma
    # (nunca o prêmio), como prometido nos termos de uso.
    try:
        transferencia = pagamentos.enviar_pix(
            valor_reais=valor + TAXA_ABACATEPAY_ESTIMADA,
            chave_pix=capitao.chave_pix,
            tipo_chave=capitao.tipo_chave_pix,
            descricao=f"Premio {rotulo} {torneio.nome} - {time_obj.nome}",
            external_id=f"premio-{colocacao}-torneio-{torneio.id}" if colocacao != "campeao" else f"premio-torneio-{torneio.id}",
        )
    except pagamentos.PagamentoIndisponivel as erro:
        setattr(torneio, campo_status, "falhou")
        app.logger.error(
            f"Prêmio do {rotulo} falhou (torneio {torneio.id}, time {time_obj.nome}, "
            f"chave {capitao.tipo_chave_pix} {capitao.chave_pix!r}, R$ {valor:.2f}): {erro}"
        )
        return None, f"Prêmio do {rotulo}: não deu para pagar agora ({erro})."

    setattr(torneio, campo_transferencia, transferencia.get("id"))
    status_abacatepay = (transferencia.get("status") or "").upper()
    novo_status = "concluido" if status_abacatepay in ("COMPLETED", "PAID") else "pendente"
    setattr(torneio, campo_status, novo_status)

    if novo_status == "concluido":
        return f"Prêmio do {rotulo} (R$ {valor:.2f}) pago para {time_obj.nome}!", None
    return f"Transferência do prêmio do {rotulo} enviada — status ainda pendente na AbacatePay.", None


def _repassar_lucro_federacao(torneio):
    """Tenta transferir 80% do lucro (arrecadação das inscrições pagas menos
    o prêmio) para o admin dono da federação. Os outros 20% não são
    transferidos — ficam parados na própria conta AbacatePay, que é a
    parte da plataforma. Devolve (mensagem_ou_None, erro_ou_None), igual
    _pagar_premio_campeao. Mesma regra de não reenviar em cima de uma
    transferência "pendente" (já aceita pela AbacatePay, só aguardando
    confirmação)."""
    if torneio.repasse_federacao_status in ("concluido", "sem_lucro", "abaixo_minimo", "pendente"):
        return None, None

    # Lucro calculado em cima do valor que REALMENTE entrou na conta — cada
    # inscrição paga já perdeu a taxa fixa que a AbacatePay cobra pra
    # receber um Pix. Calcular em cima do valor bruto (sem descontar isso)
    # prometia repassar mais dinheiro do que existia de verdade na conta.
    #
    # O prêmio também custa (valor_premio + taxa) pra sair da conta — a
    # AbacatePay cobra a mesma taxa fixa em qualquer envio, não só no
    # recebimento — então essa taxa extra também precisa ser descontada
    # antes de calcular o que sobra pra dividir entre federação e plataforma.
    times_inscritos = len(torneio.times_inscritos)
    total_arrecadado_liquido = times_inscritos * max(torneio.valor_inscricao - TAXA_ABACATEPAY_ESTIMADA, 0)
    # Todos os prêmios (campeão, vice, 3º) entram como custo — menos os que
    # ficaram sem ninguém pra receber (colocação inexistente): esse dinheiro
    # nunca saiu, então volta pro lucro dividido.
    custo_premio = _custo_premios([
        valor for c, (valor, status) in _status_premios(torneio).items() if status != "sem_colocado"
    ])
    lucro = total_arrecadado_liquido - custo_premio
    if lucro <= 0:
        torneio.repasse_federacao_status = "sem_lucro"
        return None, None

    # Valor que a federação deve efetivamente RECEBER (líquido). Pra isso
    # acontecer, precisa se enviar valor_repasse + taxa — a taxa desse envio
    # sai da fatia de 20% da plataforma, nunca do valor repassado.
    valor_repasse = round(lucro * _fatia_federacao(torneio.federacao), 2)
    if valor_repasse < VALOR_MINIMO_PIX_ABACATEPAY:
        # Lucro pequeno demais pra virar um Pix (a AbacatePay recusa qualquer
        # envio abaixo do mínimo dela) — não é erro, só não dá pra repassar
        # esse valor. Fica retido junto com os 20% da plataforma mesmo.
        torneio.repasse_federacao_status = "abaixo_minimo"
        return None, None

    dono = torneio.federacao.dono
    if not dono.chave_pix or not dono.tipo_chave_pix:
        torneio.repasse_federacao_status = "falhou"
        return None, f"Repasse da federação: {dono.nickname} (admin de {torneio.federacao.nome}) ainda não cadastrou uma chave Pix."

    try:
        transferencia = pagamentos.enviar_pix(
            valor_reais=valor_repasse + TAXA_ABACATEPAY_ESTIMADA,
            chave_pix=dono.chave_pix,
            tipo_chave=dono.tipo_chave_pix,
            descricao=f"Repasse {torneio.nome} - {torneio.federacao.nome}",
            external_id=f"repasse-federacao-torneio-{torneio.id}",
        )
    except pagamentos.PagamentoIndisponivel as erro:
        torneio.repasse_federacao_status = "falhou"
        app.logger.error(
            f"Repasse falhou (torneio {torneio.id}, federação {torneio.federacao.nome}, "
            f"chave {dono.tipo_chave_pix} {dono.chave_pix!r}, R$ {valor_repasse:.2f}): {erro}"
        )
        return None, f"Repasse da federação: não deu para transferir agora ({erro})."

    torneio.repasse_federacao_valor = valor_repasse
    torneio.repasse_federacao_transferencia_id = transferencia.get("id")
    status_abacatepay = (transferencia.get("status") or "").upper()
    torneio.repasse_federacao_status = "concluido" if status_abacatepay in ("COMPLETED", "PAID") else "pendente"

    if torneio.repasse_federacao_status == "concluido":
        return f"Repasse de R$ {valor_repasse:.2f} enviado para {dono.nickname} ({torneio.federacao.nome}).", None
    return "Repasse da federação enviado — status ainda pendente na AbacatePay.", None


@app.route("/campeonatos/<int:torneio_id>/pagar-premio", methods=["POST"])
@login_required
def pagar_premio(torneio_id):
    torneio = Torneio.query.get_or_404(torneio_id)

    if not _pode_administrar_torneio(torneio):
        flash("Só um admin da federação pode confirmar o pagamento do prêmio.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if not torneio.time_campeao_id:
        flash("Declare o campeão antes de pagar o prêmio.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if torneio.status != "finalizado":
        flash("A disputa de 3º lugar ainda não terminou — o campeonato só encerra (e o prêmio só sai) depois dela.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    premio_ja_feito = not _premios_pendentes(torneio)
    repasse_ja_feito = torneio.repasse_federacao_status in ("concluido", "sem_lucro")
    if premio_ja_feito and repasse_ja_feito:
        flash("Não tem mais nada pendente para pagar nesse campeonato.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    # Cada envio salva seu resultado no banco IMEDIATAMENTE, antes de tentar
    # o próximo — se o dinheiro já saiu de verdade pela AbacatePay, isso
    # não pode ficar só na memória. Um erro no meio (rede, bug, timeout)
    # não pode apagar o registro de uma transferência que já aconteceu.
    #
    # A AbacatePay recusa dois saques feitos rápido demais em sequência
    # ("aguarde alguns segundos antes de solicitar outro saque") — a pausa
    # entre um envio e o próximo evita esbarrar nesse limite ao mandar
    # prêmios e repasse no mesmo clique.
    colocacoes = _colocacoes_do_torneio(torneio)
    mensagens, erros = [], []
    for colocacao in PREMIO_CAMPOS:
        mensagem, erro = _pagar_premio_colocacao(torneio, colocacao, colocacoes)
        db.session.commit()
        if mensagem or erro:
            mensagens.append(mensagem)
            erros.append(erro)
            _dormir(2)
    msg_repasse, erro_repasse = _repassar_lucro_federacao(torneio)
    db.session.commit()
    mensagens.append(msg_repasse)
    erros.append(erro_repasse)

    # O admin que apertou o botão precisa saber o resultado (deu certo ou
    # deu errado) de cada transferência — dinheiro de verdade não pode
    # ficar silencioso.
    for mensagem in filter(None, mensagens):
        notificar_federacao(torneio, "repasse_pagamento", mensagem)
    for erro in filter(None, erros):
        notificar_federacao(torneio, "repasse_pagamento", f"⚠ {erro}")
    db.session.commit()

    for mensagem in filter(None, mensagens):
        flash(mensagem, "sucesso")
    for erro in filter(None, erros):
        flash(erro, "erro")
    if not any(mensagens) and not any(erros):
        flash("Já tem uma transferência pendente de confirmação na AbacatePay — aguarde antes de tentar de novo.", "sucesso")

    # Campeonato encerrado e sem pendência de pagamento: os prints já
    # cumpriram o papel deles (prova em caso de contestação) e podem sair,
    # pra não acumular arquivo no volume sem fim.
    premio_resolvido = all(
        status in ("concluido", "pendente", "sem_colocado") for _, status in _status_premios(torneio).values()
    )
    repasse_resolvido = torneio.repasse_federacao_status in ("concluido", "pendente", "sem_lucro", "abaixo_minimo")
    if premio_resolvido and repasse_resolvido:
        apagados = _limpar_prints_do_torneio(torneio)
        db.session.commit()
        if apagados:
            flash(f"{apagados} print(s) da competição foram apagados — o campeonato está encerrado e pago.", "sucesso")

    return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))


@app.route("/campeonatos/<int:torneio_id>/inscricoes/<int:inscricao_id>/remover", methods=["POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def remover_time_do_campeonato(torneio_id, inscricao_id):
    """Tira um time do campeonato e devolve a inscrição (menos as taxas).

    Só antes do chaveamento ser sorteado — depois que os confrontos existem,
    tirar um time deixaria a chave com um buraco no meio.
    """
    torneio = Torneio.query.get_or_404(torneio_id)
    inscricao = Inscricao.query.get_or_404(inscricao_id)

    if inscricao.torneio_id != torneio.id:
        abort(404)

    if not _pode_administrar_torneio(torneio):
        flash("Só um admin da federação pode remover um time do campeonato.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if torneio.status != "aberto":
        flash("O campeonato já começou — não dá mais para remover um time.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    nome_time = inscricao.time.nome if inscricao.time else "time removido"
    mensagem, erro = _reembolsar_inscricao(inscricao)
    db.session.commit()

    if erro:
        # Não apaga a inscrição se o dinheiro não voltou — senão o registro
        # do que precisa ser devolvido some junto.
        flash(erro, "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    db.session.delete(inscricao)
    db.session.commit()

    flash(f"{nome_time} removido do campeonato. {mensagem or ''}".strip(), "sucesso")
    return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))


@app.route("/campeonatos/<int:torneio_id>/excluir", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def excluir_campeonato(torneio_id):
    torneio = Torneio.query.get_or_404(torneio_id)

    if not _pode_administrar_torneio(torneio):
        flash("Só um admin da federação pode excluir o campeonato.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    # Excluir com inscrição paga em aberto apagava o registro do pagamento
    # junto — o time ficava sem vaga E sem o dinheiro de volta. Agora precisa
    # reembolsar (ou remover) cada time pago antes.
    pagas_sem_reembolso = [
        i for i in torneio.inscricoes
        if i.status == "pago" and not i.foi_reembolsada and i.reembolso_status != "sem_saldo"
    ]
    if pagas_sem_reembolso and torneio.status != "finalizado":
        flash(
            f"Esse campeonato tem {len(pagas_sem_reembolso)} inscrição(ões) paga(s) sem reembolso. "
            "Remova e reembolse esses times antes de excluir o campeonato.",
            "erro",
        )
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if request.method == "POST":
        senha = request.form.get("senha", "")
        if not check_password_hash(current_user.senha_hash, senha):
            flash("Senha incorreta. O campeonato não foi excluído.", "erro")
            return redirect(url_for("excluir_campeonato", torneio_id=torneio_id))

        nome = torneio.nome
        # Os prints das partidas saem junto — senão os arquivos ficavam no
        # volume para sempre, sem nem existir mais um confronto apontando
        # para eles.
        _limpar_prints_do_torneio(torneio)
        Notificacao.query.filter_by(torneio_id=torneio.id).delete()
        PontuacaoRanking.query.filter_by(torneio_id=torneio.id).delete()
        Confronto.query.filter_by(torneio_id=torneio.id).delete()
        Inscricao.query.filter_by(torneio_id=torneio.id).delete()
        Grupo.query.filter_by(torneio_id=torneio.id).delete()
        db.session.delete(torneio)
        db.session.commit()

        flash(f'Campeonato "{nome}" excluído.', "sucesso")
        return redirect(url_for("campeonatos"))

    return render_template("excluir_campeonato.html", torneio=torneio)


def time_do_confronto(confronto, usuario):
    """Retorna 1 se o time do usuário é o time1 do confronto, 2 se é o time2, senão None."""
    if not usuario.is_authenticated or not usuario.time_id:
        return None
    if usuario.time_id == confronto.time1_id:
        return 1
    if usuario.time_id == confronto.time2_id:
        return 2
    return None


@app.route("/campeonatos/<int:torneio_id>/confrontos/<int:confronto_id>")
def detalhe_confronto(torneio_id, confronto_id):
    confronto = Confronto.query.get_or_404(confronto_id)
    if confronto.torneio_id != torneio_id:
        abort(404)

    meu_lado = time_do_confronto(confronto, current_user)
    eh_organizador = _pode_administrar_torneio(confronto.torneio)
    ja_contestado = bool(
        Notificacao.query.filter_by(confronto_id=confronto.id, tipo="placar_contestado").first()
    )

    return render_template(
        "confronto_detalhe.html",
        confronto=confronto,
        meu_lado=meu_lado,
        eh_organizador=eh_organizador,
        ja_contestado=ja_contestado,
    )


@app.route("/campeonatos/<int:torneio_id>/confrontos/<int:confronto_id>/contestar", methods=["POST"])
@login_required
@limiter.limit("5 per minute", methods=["POST"])
def contestar_placar(torneio_id, confronto_id):
    confronto = Confronto.query.get_or_404(confronto_id)
    if confronto.torneio_id != torneio_id:
        abort(404)

    if confronto.status != "validado":
        flash("Só dá para contestar uma partida já validada.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    # Sem isso, dava pra ficar clicando de novo (uma vez por minuto, sem
    # limite de quantas vezes) e floodar o e-mail de suporte com a mesma
    # contestação repetida. Uma vez contestada, essa partida trava —
    # organizador e suporte já foram avisados na primeira vez.
    ja_contestado = Notificacao.query.filter_by(confronto_id=confronto.id, tipo="placar_contestado").first()
    if ja_contestado:
        flash("Esse placar já foi contestado antes — o organizador e o suporte já estão cientes.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    meu_lado = time_do_confronto(confronto, current_user)
    meu_time = confronto.time1 if meu_lado == 1 else confronto.time2 if meu_lado == 2 else None
    if not meu_time or current_user.id != meu_time.capitao_id:
        flash("Só o capitão de um dos dois times pode contestar o placar.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    torneio = confronto.torneio
    corpo = (
        f"Contestação de placar em {torneio.nome} ({torneio.federacao.nome}).\n\n"
        f"Partida: {confronto.nome_time1} {confronto.placar_time1} x {confronto.placar_time2} {confronto.nome_time2}\n"
        f"Fase: {confronto.fase}\n"
        f"Contestado por: {current_user.nickname} ({current_user.email}), capitão do {meu_time.nome}\n"
    )
    try:
        email_util.enviar_email(
            "suporte@prochampions.com.br",
            f"Contestação de placar — {torneio.nome}",
            corpo,
        )
    except email_util.EmailIndisponivel as erro:
        app.logger.error(f"Falha ao enviar e-mail de contestação (confronto {confronto.id}): {erro}")

    notificar_federacao(
        torneio,
        "placar_contestado",
        f'{meu_time.nome} contestou o placar de {confronto.nome_time1} {confronto.placar_time1} x '
        f'{confronto.placar_time2} {confronto.nome_time2} ({torneio.nome}).',
        confronto=confronto,
    )
    db.session.commit()

    flash("Contestação enviada! O organizador foi avisado e o suporte vai revisar.", "sucesso")
    return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))


@app.route("/campeonatos/<int:torneio_id>/confrontos/<int:confronto_id>/nick", methods=["POST"])
@login_required
@limiter.limit("20 per minute", methods=["POST"])
def salvar_nick_fc(torneio_id, confronto_id):
    """Capitão publica o nick dele no FC 26 pra essa partida.

    Os dois times passam a ver os dois nicks e combinam entre eles quem
    manda o convite dentro do jogo. O site não usa esse nome pra mais nada.
    """
    confronto = Confronto.query.get_or_404(confronto_id)
    if confronto.torneio_id != torneio_id:
        abort(404)

    meu_lado = time_do_confronto(confronto, current_user)
    meu_time = confronto.time1 if meu_lado == 1 else confronto.time2 if meu_lado == 2 else None
    if not meu_time or current_user.id != meu_time.capitao_id:
        flash("Só o capitão de um dos dois times pode informar o nick da partida.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if confronto.status in ("validado", "wo"):
        flash("Essa partida já foi encerrada.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    nick = request.form.get("nick_fc", "").strip()[:40]

    if meu_lado == 1:
        confronto.nick_fc_time1 = nick or None
    else:
        confronto.nick_fc_time2 = nick or None
    db.session.commit()

    flash(
        f"Nick salvo! O {confronto.nome_time2 if meu_lado == 1 else confronto.nome_time1} já consegue te achar no jogo."
        if nick else "Nick removido.",
        "sucesso",
    )
    return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))


@app.route("/campeonatos/<int:torneio_id>/confrontos/<int:confronto_id>/resolver", methods=["POST"])
@login_required
@limiter.limit("20 per minute", methods=["POST"])
def resolver_confronto_manual(torneio_id, confronto_id):
    """Deixa o admin da federação cravar o placar de uma partida na mão.

    É a saída para os casos em que a leitura automática não resolve sozinha:
    times com nomes parecidos demais (a IA não sabe qual placar é de quem),
    divergência que não acaba, ou W.O. duplo — que antes travava a fase
    inteira para sempre, sem nenhuma forma de destravar pelo site.
    """
    confronto = Confronto.query.get_or_404(confronto_id)
    if confronto.torneio_id != torneio_id:
        abort(404)

    if not _pode_administrar_torneio(confronto.torneio):
        flash("Só um admin da federação pode resolver a partida manualmente.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if confronto.status == "validado":
        flash("Essa partida já tem um resultado confirmado.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if confronto.eh_bye:
        flash("Essa vaga não teve adversário — o time passou direto de fase, não tem placar para definir.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    try:
        placar1 = int(request.form["placar_time1"])
        placar2 = int(request.form["placar_time2"])
        if placar1 < 0 or placar2 < 0 or placar1 > 99 or placar2 > 99:
            raise ValueError
    except (KeyError, ValueError):
        flash("Placar inválido. Use números de 0 a 99 nos dois campos.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if placar1 == placar2 and not confronto.fase.startswith("Grupo "):
        flash("Empate não decide mata-mata — coloque o placar do desempate para alguém avançar.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    confronto.placar_time1 = placar1
    confronto.placar_time2 = placar2
    confronto.vencedor_id = None if placar1 == placar2 else (
        confronto.time1_id if placar1 > placar2 else confronto.time2_id
    )
    confronto.vencedor_wo_id = None
    confronto.status = "validado"

    resultado = (
        f"{confronto.vencedor.nome} venceu {max(placar1, placar2)} a {min(placar1, placar2)}"
        if confronto.vencedor_id
        else f"Empate em {placar1} a {placar2}"
    )
    notificar_federacao(
        confronto.torneio,
        "partida_finalizada",
        f"A partida {confronto.nome_time1} x {confronto.nome_time2} ({confronto.torneio.nome}) "
        f"foi resolvida manualmente pelo admin: {resultado}.",
        confronto=confronto,
    )
    _avancar_chaveamento(confronto)
    db.session.commit()

    flash(f"Resultado definido manualmente: {resultado}.", "sucesso")
    return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))


@app.route("/campeonatos/<int:torneio_id>/confrontos/<int:confronto_id>/iniciar", methods=["POST"])
@login_required
def iniciar_confronto(torneio_id, confronto_id):
    confronto = Confronto.query.get_or_404(confronto_id)
    if confronto.torneio_id != torneio_id:
        abort(404)

    if not _pode_administrar_torneio(confronto.torneio):
        flash("Só um admin da federação pode marcar o início da partida.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if confronto.status in ("validado", "wo"):
        flash("Essa partida já foi encerrada.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if confronto.hora_inicio:
        flash("A partida já tinha sido marcada como iniciada.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    confronto.hora_inicio = datetime.utcnow()
    notificar_federacao(
        confronto.torneio,
        "partida_iniciada",
        f'A partida {confronto.nome_time1} x {confronto.nome_time2} ({confronto.torneio.nome}) começou.',
        confronto=confronto,
    )
    db.session.commit()

    flash(f"Partida marcada como iniciada às {confronto.hora_inicio.strftime('%H:%M')}.", "sucesso")
    return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))


OPCOES_WO = {"time1", "time2", "duplo"}


@app.route("/campeonatos/<int:torneio_id>/confrontos/<int:confronto_id>/wo")
@login_required
def escolher_wo(torneio_id, confronto_id):
    confronto = Confronto.query.get_or_404(confronto_id)
    if confronto.torneio_id != torneio_id:
        abort(404)

    if not _pode_administrar_torneio(confronto.torneio):
        flash("Só um admin da federação pode declarar W.O.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if confronto.status in ("validado", "wo"):
        flash("Essa partida já foi encerrada.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if not confronto.hora_inicio:
        flash("Marque o início da partida antes de declarar W.O.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    return render_template("escolher_wo.html", confronto=confronto)


@app.route("/campeonatos/<int:torneio_id>/confrontos/<int:confronto_id>/wo/<opcao>", methods=["GET", "POST"])
@login_required
def confirmar_wo(torneio_id, confronto_id, opcao):
    confronto = Confronto.query.get_or_404(confronto_id)
    if confronto.torneio_id != torneio_id:
        abort(404)

    if opcao not in OPCOES_WO:
        abort(404)

    if not _pode_administrar_torneio(confronto.torneio):
        flash("Só um admin da federação pode declarar W.O.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if confronto.status in ("validado", "wo"):
        flash("Essa partida já foi encerrada.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if not confronto.hora_inicio:
        flash("Marque o início da partida antes de declarar W.O.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    time_vencedor = confronto.time1 if opcao == "time1" else confronto.time2 if opcao == "time2" else None

    if request.method == "POST":
        confronto.status = "wo"
        confronto.vencedor_wo_id = time_vencedor.id if time_vencedor else None

        if time_vencedor:
            resultado = f"{time_vencedor.nome} venceu por W.O. (ausência do adversário)."
        else:
            resultado = "W.O. duplo — nenhum time compareceu."

        notificar_federacao(
            confronto.torneio,
            "partida_finalizada",
            f'A partida {confronto.nome_time1} x {confronto.nome_time2} ({confronto.torneio.nome}) finalizou: {resultado}',
            confronto=confronto,
        )
        _avancar_chaveamento(confronto)
        db.session.commit()

        flash(f"W.O. declarado — {resultado}" if time_vencedor else resultado, "sucesso")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    return render_template(
        "confirmar_wo.html",
        confronto=confronto,
        opcao=opcao,
        time_vencedor=time_vencedor,
    )


SUFIXOS_CLUBE_IGNORADOS = {"fc", "ec", "sc", "ac", "cf", "afc", "cfc", "cd", "ce"}


def _normalizar_nome_time(nome):
    """Tira acento, deixa minúsculo e ignora sufixos genéricos de clube
    (FC, EC, SC...) — o nick usado dentro do jogo raramente bate letra por
    letra com o nome cadastrado no site."""
    sem_acento = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    palavras = [p for p in sem_acento.lower().split() if p not in SUFIXOS_CLUBE_IGNORADOS]
    return " ".join(palavras).strip()


def _nomes_parecidos(nome_a, nome_b):
    """Compara dois nomes de time com folga: ignora maiúsculas/minúsculas,
    acentos, espaço nas pontas e sufixos genéricos tipo FC/EC/SC."""
    a, b = _normalizar_nome_time(nome_a), _normalizar_nome_time(nome_b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _pares_leitura(leitura):
    """Converte {time_casa, placar_casa, time_visitante, placar_visitante} numa
    lista [(nome, placar), (nome, placar)], sem depender de quem é casa/visitante."""
    return [
        (leitura["time_casa"], int(leitura["placar_casa"])),
        (leitura["time_visitante"], int(leitura["placar_visitante"])),
    ]


def _leituras_batem(leitura1, leitura2):
    """Compara as duas leituras casando cada time pelo nome parecido (não exige
    string idêntica — a IA pode ler "Corinthians" numa e "Corinthians FC" na
    outra) e exigindo o mesmo placar para o par casado."""
    pares1, pares2 = _pares_leitura(leitura1), _pares_leitura(leitura2)
    restantes = list(pares2)
    for nome1, placar1 in pares1:
        par_casado = next(
            (p for p in restantes if _nomes_parecidos(nome1, p[0]) and placar1 == p[1]),
            None,
        )
        if not par_casado:
            return False
        restantes.remove(par_casado)
    return True


def _tentar_finalizar_confronto(confronto):
    """Se as duas leituras da IA estiverem prontas, compara e decide o status.

    Confirmada (placares e nomes batendo entre as duas leituras): marca
    "validado" e preenche o placar. Se só uma leitura chegou ainda, não faz
    nada (fica "pendente"). Do contrário marca "divergente" e devolve o
    motivo ("nao_bateu" ou "empate"), para dar um aviso mais preciso.

    Empate: só é aceito nos jogos da fase de grupos (onde faz sentido ter
    empate na tabela de pontos). No mata-mata precisa de um vencedor para
    avançar de fase, então lá o empate continua sendo tratado como
    divergência — precisa jogar o desempate e reenviar o print.
    """
    if not (confronto.leitura_time1_ok and confronto.leitura_time2_ok):
        return None

    leitura1 = json.loads(confronto.leitura_time1)
    leitura2 = json.loads(confronto.leitura_time2)

    if not _leituras_batem(leitura1, leitura2):
        confronto.status = "divergente"
        return "nao_bateu"

    nomes_lidos = _pares_leitura(leitura1)
    nome0, placar0 = nomes_lidos[0]
    nome1, placar1 = nomes_lidos[1]

    bate_direto = _nomes_parecidos(nome0, confronto.nome_time1) or _nomes_parecidos(nome1, confronto.nome_time2)
    bate_invertido = _nomes_parecidos(nome1, confronto.nome_time1) or _nomes_parecidos(nome0, confronto.nome_time2)

    if bate_direto and not bate_invertido:
        placar_time1, placar_time2 = placar0, placar1
    elif bate_invertido and not bate_direto:
        placar_time1, placar_time2 = placar1, placar0
    else:
        # Nem um nome do print bateu com nenhum dos dois times do confronto
        # (ou os dois bateram ao mesmo tempo, o que não devia acontecer) —
        # sem saber com confiança qual placar é de qual lado, não dá para
        # adivinhar. Melhor pedir para reenviar do que arriscar inverter o
        # resultado.
        confronto.status = "divergente"
        return "nomes_nao_identificados"

    if placar_time1 == placar_time2 and not confronto.fase.startswith("Grupo "):
        # As duas leituras bateram, mas deu empate — no mata-mata precisa de
        # vencedor para avançar, então precisa jogar o desempate e reenviar.
        confronto.status = "divergente"
        return "empate"

    confronto.placar_time1 = placar_time1
    confronto.placar_time2 = placar_time2
    confronto.vencedor_id = None if placar_time1 == placar_time2 else (
        confronto.time1_id if placar_time1 > placar_time2 else confronto.time2_id
    )
    confronto.status = "validado"
    return "ok"


@app.route("/campeonatos/<int:torneio_id>/confrontos/<int:confronto_id>/print", methods=["POST"])
@login_required
# Cada envio custa uma leitura de IA (dinheiro) e grava um arquivo no disco —
# sem limite, dava pra ficar reenviando print e queimar o saldo da API.
@limiter.limit("10 per minute", methods=["POST"])
def enviar_print(torneio_id, confronto_id):
    confronto = Confronto.query.get_or_404(confronto_id)
    if confronto.torneio_id != torneio_id:
        abort(404)

    meu_lado = time_do_confronto(confronto, current_user)
    if meu_lado is None:
        flash("Só os capitães dos dois times desse confronto podem enviar o print.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    time_atual = confronto.time1 if meu_lado == 1 else confronto.time2
    if current_user.id != time_atual.capitao_id:
        flash("Só o capitão do time pode enviar o print da partida.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    # "wo" também conta como encerrado: sem isso, os dois capitães podiam
    # combinar prints e derrubar o W.O. que o organizador tinha declarado —
    # trocando o vencedor e duplicando a fase seguinte do chaveamento.
    if confronto.status in ("validado", "wo"):
        flash("Essa partida já foi encerrada.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    arquivo = request.files.get("print")
    if not arquivo or not arquivo.filename:
        flash("Selecione uma imagem do print antes de enviar.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    extensao = arquivo.filename.rsplit(".", 1)[-1].lower() if "." in arquivo.filename else ""
    if extensao not in EXTENSOES_PRINT_PERMITIDAS:
        flash("Formato inválido. Envie uma imagem (png, jpg ou webp).", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    nome_arquivo = secure_filename(f"confronto{confronto.id}-time{meu_lado}-{uuid.uuid4().hex}.{extensao}")
    caminho_arquivo = os.path.join(app.config["PASTA_PRINTS"], nome_arquivo)
    arquivo.save(caminho_arquivo)

    # Reenvio (divergência) troca o print: apaga o anterior pra não deixar
    # arquivo órfão ocupando o volume pra sempre.
    if meu_lado == 1:
        _apagar_upload("prints", confronto.print_time1)
        confronto.print_time1 = nome_arquivo
    else:
        _apagar_upload("prints", confronto.print_time2)
        confronto.print_time2 = nome_arquivo

    # Manda para IA ler o placar desse print. Qualquer falha (timeout, rede,
    # resposta que não é JSON válido) vira aviso para tentar de novo, sem
    # derrubar a aplicação.
    try:
        leitura = validacao_ia.ler_placar(caminho_arquivo)
    except validacao_ia.LeituraIndisponivel as erro:
        if meu_lado == 1:
            confronto.leitura_time1_ok = False
        else:
            confronto.leitura_time2_ok = False
        db.session.commit()
        flash(f"Não deu para ler o print agora ({erro}). Tenta reenviar.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if "erro" in leitura:
        if meu_lado == 1:
            confronto.leitura_time1_ok = False
        else:
            confronto.leitura_time2_ok = False
        db.session.commit()
        flash("Essa imagem não parece ser um print do placar final da partida. Envie a tela com o resultado.", "erro")
        return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))

    if meu_lado == 1:
        confronto.leitura_time1 = json.dumps(leitura)
        confronto.leitura_time1_ok = True
    else:
        confronto.leitura_time2 = json.dumps(leitura)
        confronto.leitura_time2_ok = True

    if confronto.status != "validado":
        confronto.status = "pendente"
    motivo_divergencia = _tentar_finalizar_confronto(confronto)

    if confronto.status == "validado":
        if confronto.vencedor_id:
            placar_vencedor, placar_perdedor = (
                (confronto.placar_time1, confronto.placar_time2)
                if confronto.vencedor_id == confronto.time1_id
                else (confronto.placar_time2, confronto.placar_time1)
            )
            resultado_texto = f'{confronto.vencedor.nome} venceu {placar_vencedor} a {placar_perdedor}.'
        else:
            resultado_texto = f'Empate em {confronto.placar_time1} a {confronto.placar_time2}.'
        notificar_federacao(
            confronto.torneio,
            "partida_finalizada",
            f'A partida {confronto.nome_time1} x {confronto.nome_time2} ({confronto.torneio.nome}) finalizou: {resultado_texto}',
            confronto=confronto,
        )
        _avancar_chaveamento(confronto)
    db.session.commit()

    if confronto.status == "validado":
        if confronto.vencedor_id:
            placar_vencedor, placar_perdedor = (
                (confronto.placar_time1, confronto.placar_time2)
                if confronto.vencedor_id == confronto.time1_id
                else (confronto.placar_time2, confronto.placar_time1)
            )
            flash(f"Print lido e confirmado! {confronto.vencedor.nome} venceu {placar_vencedor} a {placar_perdedor}.", "sucesso")
        else:
            flash(f"Print lido e confirmado! Empate em {confronto.placar_time1} a {confronto.placar_time2}.", "sucesso")
    elif motivo_divergencia == "empate":
        flash("As duas leituras bateram, mas deu empate — essa fase não aceita empate. Reenviem o print (do desempate, se já tiver rolado).", "erro")
    elif motivo_divergencia == "nao_bateu":
        flash("A leitura dos dois prints não bateu. Confiram o resultado combinado e reenviem o print.", "erro")
    elif motivo_divergencia == "nomes_nao_identificados":
        flash("Não deu para reconhecer o nome de nenhum dos dois times no print. Confira se enviou a tela certa e reenvie.", "erro")
    else:
        flash("Print lido! Falta o outro time enviar o dele para confirmar o resultado.", "sucesso")

    return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))


@app.cli.command("promover-organizador")
@click.argument("email")
def promover_organizador(email):
    """Marca um usuário como organizador, liberando ele para criar uma federação."""
    usuario = User.query.filter_by(email=email.strip().lower()).first()
    if not usuario:
        click.echo(f"Nenhum usuário encontrado com o e-mail {email}.")
        return

    usuario.is_organizador = True
    db.session.commit()
    click.echo(f"{usuario.nickname} agora é organizador.")


# Fora do "if __main__" de propósito — precisa rodar também quando o
def _garantir_colunas_novas():
    """Adiciona colunas novas em tabelas que já existem, no boot.

    db.create_all() só cria tabela que ainda não existe — ele NUNCA altera
    uma tabela existente. Sem isso, toda vez que a gente adiciona um campo
    novo num model, o banco de produção fica sem a coluna e qualquer INSERT
    naquela tabela quebra (foi exatamente o que derrubou o cadastro quando
    adicionamos o link de WhatsApp).

    Rodar aqui, no boot, resolve antes do app atender qualquer requisição —
    e o "IF NOT EXISTS" faz ser um no-op nas próximas vezes. No SQLite local
    o create_all() já cria a tabela completa, então o erro de sintaxe do
    ALTER é ignorado de propósito.
    """
    colunas = [
        ("inscricoes", "reembolso_valor", "DOUBLE PRECISION"),
        ("inscricoes", "reembolso_transferencia_id", "VARCHAR(80)"),
        ("inscricoes", "reembolso_status", "VARCHAR(20)"),
        ("confrontos", "nick_fc_time1", "VARCHAR(40)"),
        ("confrontos", "nick_fc_time2", "VARCHAR(40)"),
        # NOT NULL DEFAULT preenche as linhas que já existem com 'fc26' na
        # hora — sem isso a coluna nasceria nula nos campeonatos antigos.
        ("torneios", "jogo", f"VARCHAR(10) NOT NULL DEFAULT '{JOGO_PADRAO}'"),
        # Ranking por temporada. A tabela temporadas nasce no create_all()
        # logo acima, então a FK já existe quando esse ALTER roda.
        ("torneios", "temporada_id", "INTEGER REFERENCES temporadas(id)"),
        ("torneios", "elegivel_ranking", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("confrontos", "eh_disputa_3_lugar", "BOOLEAN NOT NULL DEFAULT FALSE"),
        # Prêmios de vice/terceiro e XP.
        ("torneios", "valor_premio_vice", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("torneios", "valor_premio_terceiro", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
        ("torneios", "premio_vice_transferencia_id", "VARCHAR(80)"),
        ("torneios", "premio_vice_status", "VARCHAR(20)"),
        ("torneios", "premio_terceiro_transferencia_id", "VARCHAR(80)"),
        ("torneios", "premio_terceiro_status", "VARCHAR(20)"),
        ("torneios", "xp_creditado", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("users", "xp_total", "INTEGER NOT NULL DEFAULT 0"),
        ("teams", "xp_total", "INTEGER NOT NULL DEFAULT 0"),
        # Mercado de transferências.
        ("teams", "recrutando", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("teams", "mensagem_recrutamento", "VARCHAR(160)"),
        ("federacoes", "fatia_lucro", "DOUBLE PRECISION NOT NULL DEFAULT 0.8"),
        # Escalação e posições.
        ("users", "posicao_preferida", "VARCHAR(4)"),
        ("users", "posicao_escalada", "VARCHAR(6)"),
    ]
    for tabela, coluna, tipo in colunas:
        try:
            db.session.execute(db.text(f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS {coluna} {tipo}"))
            db.session.commit()
        except Exception as erro:  # SQLite não tem "IF NOT EXISTS" em ADD COLUMN
            db.session.rollback()
            app.logger.info(f"Migração de coluna {tabela}.{coluna} ignorada: {erro}")


# gunicorn importa esse arquivo em produção (ele nunca executa como
# "__main__"). db.create_all() só cria as tabelas que ainda não existem,
# então rodar de novo a cada início não faz nada se elas já estiverem lá.
with app.app_context():
    db.create_all()
    _garantir_colunas_novas()

if __name__ == "__main__":
    # Debug SÓ fica ligado se você setar FLASK_DEBUG=1 no ambiente. Com
    # debug ligado, qualquer erro abre um console que executa código
    # Python — isso não pode estar ligado se o site for para o ar de verdade.
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
