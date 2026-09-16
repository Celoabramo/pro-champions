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
from models import db, User, Team, Torneio, Confronto, Inscricao, Notificacao, Federacao, Grupo
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
    return render_template("time.html", usuario=current_user, formacoes=FORMACOES_VALIDAS)


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

    current_user.time_id = None
    db.session.commit()

    # Se o time ficou vazio, apaga ele — e junto as inscrições em
    # campeonato que ele tinha, senão sobra referência quebrada por aí.
    if time_atual and len(time_atual.membros) == 0:
        Inscricao.query.filter_by(time_id=time_atual.id).delete()
        db.session.delete(time_atual)
        db.session.commit()

    flash("Você saiu do time.", "sucesso")
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


FORMACOES_VALIDAS = ["3-3-4", "3-4-3", "3-5-2", "4-2-4", "4-3-3", "4-4-2", "4-5-1", "5-3-2", "5-4-1"]


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
    db.session.commit()
    flash(f"Esquema tático atualizado para {formacao}.", "sucesso")
    return redirect(url_for("time"))


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
        premios_pagos = sum(t.valor_premio for t in torneios if t.premio_status == "concluido")

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

        premio_texto = request.form.get("valor_premio", "").strip().replace(",", ".")
        try:
            valor_premio = float(premio_texto) if premio_texto else 0
            if valor_premio < 0:
                raise ValueError
        except ValueError:
            flash("Valor do prêmio inválido.", "erro")
            return redirect(url_for("criar_campeonato"))

        if 0 < valor_premio < VALOR_MINIMO_PIX_ABACATEPAY:
            flash(
                f"Prêmio precisa ser de pelo menos R$ {VALOR_MINIMO_PIX_ABACATEPAY:.2f} (mínimo exigido pela "
                "AbacatePay pra qualquer Pix) ou R$ 0,00 (sem prêmio).",
                "erro",
            )
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
            custo_premio_maximo = (valor_premio + TAXA_ABACATEPAY_ESTIMADA) if valor_premio > 0 else 0
            base_para_divisao = arrecadacao_liquida_maxima - custo_premio_maximo
            lucro_plataforma_estimado = (
                base_para_divisao * (1 - FATIA_FEDERACAO_NO_LUCRO)
                - TAXA_ABACATEPAY_ESTIMADA  # taxa de envio do repasse pra federação
                - custo_ia_estimado
            )

            if lucro_plataforma_estimado < LUCRO_MINIMO_PLATAFORMA:
                flash(
                    f"Com {max_times} times a R$ {valor:.2f} e prêmio de R$ {valor_premio:.2f}, o lucro "
                    f"da plataforma fica em torno de R$ {lucro_plataforma_estimado:.2f} — abaixo do mínimo "
                    f"de R$ {LUCRO_MINIMO_PLATAFORMA:.2f}. Aumente o valor da inscrição, reduza o prêmio ou "
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

        novo_campeonato = Torneio(
            nome=nome,
            descricao=descricao,
            valor_inscricao=valor,
            max_times=max_times,
            valor_premio=valor_premio,
            data_campeonato=data_campeonato,
            hora_campeonato=hora_campeonato,
            formato=formato,
            geracao=geracao,
            federacao_id=current_user.federacao_id,
            criado_por_id=current_user.id,
        )
        db.session.add(novo_campeonato)
        db.session.commit()

        flash("Campeonato criado!", "sucesso")
        return redirect(url_for("detalhe_campeonato", torneio_id=novo_campeonato.id))

    return render_template("criar_campeonato.html", tamanhos=TAMANHOS_VALIDOS, hoje=date.today().isoformat())


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

    eh_organizador = current_user.is_authenticated and current_user.federacao_id == torneio.federacao_id
    pode_gerar_chaveamento = (
        eh_organizador
        and torneio.status == "aberto"
        and len(torneio.times_inscritos) == torneio.max_times
    )
    premio_pendente = torneio.valor_premio > 0 and torneio.premio_status != "concluido"
    repasse_pendente = torneio.repasse_federacao_status not in ("concluido", "sem_lucro", "abaixo_minimo")
    pode_pagar_premio = eh_organizador and torneio.time_campeao_id and (premio_pendente or repasse_pendente)
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
        pode_pagar_premio=pode_pagar_premio,
        eh_membro_campeao=eh_membro_campeao,
        grupos_info=grupos_info,
        rodadas_bracket=_estrutura_bracket(torneio) if torneio.status != "aberto" else None,
    )


@app.route("/campeonatos/<int:torneio_id>/inscrever", methods=["POST"])
@login_required
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
    if inscricao_existente and inscricao_existente.status == "pendente":
        return redirect(url_for("pagamento_pix", torneio_id=torneio_id, inscricao_id=inscricao_existente.id))

    if len(torneio.times_inscritos) >= torneio.max_times:
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
            inscricao.status = "pago"
            inscricao.pago_em = datetime.utcnow()
            db.session.commit()

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
    confrontos_fase = (
        Confronto.query
        .filter_by(torneio_id=torneio.id, fase=confronto.fase)
        .order_by(Confronto.ordem)
        .all()
    )
    if any(c.status not in ("validado", "wo") for c in confrontos_fase):
        return

    vencedores = [_vencedor_confronto(c) for c in confrontos_fase]
    if any(v is None for v in vencedores):
        return

    if len(vencedores) == 1:
        campeao = vencedores[0]
        torneio.time_campeao_id = campeao.id
        torneio.status = "finalizado"
        notificar_federacao(
            torneio,
            "campeonato_finalizado",
            f'O campeonato "{torneio.nome}" foi finalizado — {campeao.nome} é o campeão!',
        )
        return

    proxima_fase = FASES_POR_TAMANHO.get(len(vencedores), f"Fase de {len(vencedores)}")
    for ordem, i in enumerate(range(0, len(vencedores), 2)):
        db.session.add(Confronto(
            torneio_id=torneio.id,
            fase=proxima_fase,
            ordem=ordem,
            time1_id=vencedores[i].id,
            time2_id=vencedores[i + 1].id,
        ))


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
        tamanho = torneio.max_times
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

    if torneio.federacao_id != current_user.federacao_id:
        flash("Só um admin da federação pode gerar o chaveamento.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if torneio.status != "aberto":
        flash("O chaveamento desse campeonato já foi gerado.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    times = list(torneio.times_inscritos)
    if len(times) != torneio.max_times:
        flash(
            f"O campeonato precisa das {torneio.max_times} vagas preenchidas para sortear o chaveamento "
            f"(hoje tem {len(times)}).",
            "erro",
        )
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if torneio.formato == "fase_grupos":
        _gerar_grupos(torneio, times)
        descricao_fase = f"{len(times) // 4} grupo(s) sorteado(s)"
        mensagem_sucesso = f"Grupos sorteados! {descricao_fase}."
    else:
        random.shuffle(times)
        fase = FASES_POR_TAMANHO.get(len(times), f"Fase de {len(times)}")
        for ordem, i in enumerate(range(0, len(times), 2)):
            db.session.add(Confronto(
                torneio_id=torneio.id,
                fase=fase,
                ordem=ordem,
                time1_id=times[i].id,
                time2_id=times[i + 1].id,
            ))
        descricao_fase = f"chaveamento sorteado ({fase})"
        mensagem_sucesso = f"Chaveamento sorteado! Fase inicial: {fase}."

    torneio.status = "em_andamento"
    notificar_federacao(
        torneio,
        "campeonato_iniciado",
        f'O campeonato "{torneio.nome}" começou — {descricao_fase}.',
    )
    db.session.commit()

    flash(mensagem_sucesso, "sucesso")
    return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))


FATIA_FEDERACAO_NO_LUCRO = 0.8  # os outros 20% ficam parados na conta AbacatePay (é a parte da plataforma)


def _pagar_premio_campeao(torneio):
    """Tenta transferir o prêmio para o capitão do time campeão. Devolve
    (mensagem_ou_None, erro_ou_None) — só um dos dois vem preenchido.
    Não faz nada (devolve None, None) se já pago, sem prêmio, ou se já tem
    uma transferência "pendente" — nesse caso a AbacatePay já aceitou o
    envio e só está confirmando; tentar de novo criaria um segundo envio
    real em cima do primeiro."""
    if torneio.valor_premio <= 0 or torneio.premio_status in ("concluido", "pendente"):
        return None, None

    capitao = torneio.time_campeao.capitao
    if not capitao.chave_pix or not capitao.tipo_chave_pix:
        torneio.premio_status = "falhou"
        return None, f"{capitao.nickname} (capitão do {torneio.time_campeao.nome}) ainda não cadastrou uma chave Pix para receber o prêmio."

    # A AbacatePay cobra a mesma taxa fixa também em cima de QUALQUER envio
    # (não só no recebimento) — manda-se valor_premio + taxa pra o campeão
    # receber o valor cheio anunciado, e quem absorve a taxa é a fatia da
    # plataforma (nunca o prêmio), como prometido nos termos de uso.
    try:
        transferencia = pagamentos.enviar_pix(
            valor_reais=torneio.valor_premio + TAXA_ABACATEPAY_ESTIMADA,
            chave_pix=capitao.chave_pix,
            tipo_chave=capitao.tipo_chave_pix,
            descricao=f"Prêmio {torneio.nome} - {torneio.time_campeao.nome}",
            external_id=f"premio-torneio-{torneio.id}",
        )
    except pagamentos.PagamentoIndisponivel as erro:
        torneio.premio_status = "falhou"
        return None, f"Prêmio: não deu para pagar agora ({erro})."

    torneio.premio_transferencia_id = transferencia.get("id")
    status_abacatepay = (transferencia.get("status") or "").upper()
    torneio.premio_status = "concluido" if status_abacatepay in ("COMPLETED", "PAID") else "pendente"

    if torneio.premio_status == "concluido":
        return f"Prêmio de R$ {torneio.valor_premio:.2f} pago para {torneio.time_campeao.nome}!", None
    return "Transferência do prêmio enviada — status ainda pendente na AbacatePay.", None


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
    custo_premio = (torneio.valor_premio + TAXA_ABACATEPAY_ESTIMADA) if torneio.valor_premio > 0 else 0
    lucro = total_arrecadado_liquido - custo_premio
    if lucro <= 0:
        torneio.repasse_federacao_status = "sem_lucro"
        return None, None

    # Valor que a federação deve efetivamente RECEBER (líquido). Pra isso
    # acontecer, precisa se enviar valor_repasse + taxa — a taxa desse envio
    # sai da fatia de 20% da plataforma, nunca do valor repassado.
    valor_repasse = round(lucro * FATIA_FEDERACAO_NO_LUCRO, 2)
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

    if torneio.federacao_id != current_user.federacao_id:
        flash("Só um admin da federação pode confirmar o pagamento do prêmio.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if not torneio.time_campeao_id:
        flash("Declare o campeão antes de pagar o prêmio.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    premio_ja_feito = torneio.valor_premio <= 0 or torneio.premio_status == "concluido"
    repasse_ja_feito = torneio.repasse_federacao_status in ("concluido", "sem_lucro")
    if premio_ja_feito and repasse_ja_feito:
        flash("Não tem mais nada pendente para pagar nesse campeonato.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    # Cada envio salva seu resultado no banco IMEDIATAMENTE, antes de tentar
    # o próximo — se o dinheiro já saiu de verdade pela AbacatePay, isso
    # não pode ficar só na memória. Um erro no meio (rede, bug, timeout)
    # não pode apagar o registro de uma transferência que já aconteceu.
    msg_premio, erro_premio = _pagar_premio_campeao(torneio)
    db.session.commit()

    # A AbacatePay recusa dois saques feitos rápido demais em sequência
    # ("aguarde alguns segundos antes de solicitar outro saque") — essa
    # pequena pausa evita esbarrar nesse limite ao mandar prêmio e repasse
    # no mesmo clique.
    _dormir(2)
    msg_repasse, erro_repasse = _repassar_lucro_federacao(torneio)
    db.session.commit()

    # O admin que apertou o botão precisa saber o resultado (deu certo ou
    # deu errado) de cada transferência — dinheiro de verdade não pode
    # ficar silencioso.
    for mensagem in filter(None, [msg_premio, msg_repasse]):
        notificar_federacao(torneio, "repasse_pagamento", mensagem)
    for erro in filter(None, [erro_premio, erro_repasse]):
        notificar_federacao(torneio, "repasse_pagamento", f"⚠ {erro}")
    db.session.commit()

    for mensagem in filter(None, [msg_premio, msg_repasse]):
        flash(mensagem, "sucesso")
    for erro in filter(None, [erro_premio, erro_repasse]):
        flash(erro, "erro")
    if not any([msg_premio, erro_premio, msg_repasse, erro_repasse]):
        flash("Já tem uma transferência pendente de confirmação na AbacatePay — aguarde antes de tentar de novo.", "sucesso")

    return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))


@app.route("/campeonatos/<int:torneio_id>/excluir", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def excluir_campeonato(torneio_id):
    torneio = Torneio.query.get_or_404(torneio_id)

    if torneio.federacao_id != current_user.federacao_id:
        flash("Só um admin da federação pode excluir o campeonato.", "erro")
        return redirect(url_for("detalhe_campeonato", torneio_id=torneio_id))

    if request.method == "POST":
        senha = request.form.get("senha", "")
        if not check_password_hash(current_user.senha_hash, senha):
            flash("Senha incorreta. O campeonato não foi excluído.", "erro")
            return redirect(url_for("excluir_campeonato", torneio_id=torneio_id))

        nome = torneio.nome
        Notificacao.query.filter_by(torneio_id=torneio.id).delete()
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
    eh_organizador = current_user.is_authenticated and current_user.federacao_id == confronto.torneio.federacao_id
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
        f"Partida: {confronto.time1.nome} {confronto.placar_time1} x {confronto.placar_time2} {confronto.time2.nome}\n"
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
        f'{meu_time.nome} contestou o placar de {confronto.time1.nome} {confronto.placar_time1} x '
        f'{confronto.placar_time2} {confronto.time2.nome} ({torneio.nome}).',
        confronto=confronto,
    )
    db.session.commit()

    flash("Contestação enviada! O organizador foi avisado e o suporte vai revisar.", "sucesso")
    return redirect(url_for("detalhe_confronto", torneio_id=torneio_id, confronto_id=confronto_id))


@app.route("/campeonatos/<int:torneio_id>/confrontos/<int:confronto_id>/iniciar", methods=["POST"])
@login_required
def iniciar_confronto(torneio_id, confronto_id):
    confronto = Confronto.query.get_or_404(confronto_id)
    if confronto.torneio_id != torneio_id:
        abort(404)

    if confronto.torneio.federacao_id != current_user.federacao_id:
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
        f'A partida {confronto.time1.nome} x {confronto.time2.nome} ({confronto.torneio.nome}) começou.',
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

    if confronto.torneio.federacao_id != current_user.federacao_id:
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

    if confronto.torneio.federacao_id != current_user.federacao_id:
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
            f'A partida {confronto.time1.nome} x {confronto.time2.nome} ({confronto.torneio.nome}) finalizou: {resultado}',
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

    bate_direto = _nomes_parecidos(nome0, confronto.time1.nome) or _nomes_parecidos(nome1, confronto.time2.nome)
    bate_invertido = _nomes_parecidos(nome1, confronto.time1.nome) or _nomes_parecidos(nome0, confronto.time2.nome)

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

    if confronto.status == "validado":
        flash("Esse confronto já foi validado.", "erro")
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

    if meu_lado == 1:
        confronto.print_time1 = nome_arquivo
    else:
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
            f'A partida {confronto.time1.nome} x {confronto.time2.nome} ({confronto.torneio.nome}) finalizou: {resultado_texto}',
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
# gunicorn importa esse arquivo em produção (ele nunca executa como
# "__main__"). db.create_all() só cria as tabelas que ainda não existem,
# então rodar de novo a cada início não faz nada se elas já estiverem lá.
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    # Debug SÓ fica ligado se você setar FLASK_DEBUG=1 no ambiente. Com
    # debug ligado, qualquer erro abre um console que executa código
    # Python — isso não pode estar ligado se o site for para o ar de verdade.
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
