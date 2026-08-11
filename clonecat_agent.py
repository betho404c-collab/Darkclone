import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    FloodWaitError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.tl.types import Channel, Chat

BASE_DIR = Path(os.getenv("DATA_DIR", "/data"))
BASE_DIR.mkdir(parents=True, exist_ok=True)
SCRIPT = Path(os.getenv("SCRIPT_PATH", "/app/clonecat_forum_selecionar_topico.py"))
SESSION_FILE = BASE_DIR / "session.txt"
CONFIG_FILE = BASE_DIR / "config.json"
RESUME_FILE = BASE_DIR / "resume_forum.json"
LOG_FILE = BASE_DIR / "agent.log"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "").strip()

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

CONTENT_LABELS = {
    1: "📦 Tudo",
    2: "🖼 Imagens",
    3: "🎬 Vídeos",
    4: "🎵 Áudios",
    5: "📄 Documentos",
    6: "📝 Texto",
    7: "🎭 Stickers",
    8: "📦 Tudo (incluindo mensagens)",
}

# Estado simples, pois o bot foi projetado para um único administrador.
state = {
    "step": "idle",
    "origin": None,
    "origin_title": None,
    "destination": None,
    "destination_title": None,
    "topics": [],
    "topic_index": None,
    "topic_id": None,
    "topic_title": None,
    "topic_anchor": None,
    "content_choice": None,
    "content_types": None,
    "resume_pending": False,
    "process": None,
    "job_task": None,
    "status_message": None,
    "stop_requested": False,
    "restart_count": 0,
}

# Cliente MTProto usado somente pelo agente para autenticar/consultar.
# O motor de clonagem continua sendo executado pelo processo original.
tg_client = None
pending_phone = None
pending_phone_code_hash = None
pending_code_sent_at = None


def authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ADMIN_USER_ID and user.id == ADMIN_USER_ID)


async def deny(update: Update):
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Acesso não autorizado.")


def load_resume(origin: int):
    if not RESUME_FILE.exists():
        return None
    try:
        data = json.loads(RESUME_FILE.read_text(encoding="utf-8"))
        return data.get(str(origin))
    except Exception as exc:
        logging.exception("Erro lendo resume: %s", exc)
        return None


def write_config():
    CONFIG_FILE.write_text(
        json.dumps({"api_id": API_ID, "api_hash": API_HASH}),
        encoding="utf-8",
    )


def content_types_for(choice: int):
    if choice in (1, 8):
        return ["text", "photo", "video", "audio", "document", "sticker"]
    return {
        2: ["photo"],
        3: ["video"],
        4: ["audio"],
        5: ["document"],
        6: ["text"],
        7: ["sticker"],
    }[choice]


async def ensure_client():
    global tg_client
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Configure API_ID e API_HASH no Railway.")

    session = SESSION_FILE.read_text(encoding="utf-8").strip() if SESSION_FILE.exists() else ""
    if tg_client is None:
        tg_client = TelegramClient(
            StringSession(session), API_ID, API_HASH,
            connection_retries=20,
            request_retries=20,
            retry_delay=5,
            auto_reconnect=True,
        )

    if not tg_client.is_connected():
        await tg_client.connect()
    return tg_client


async def account_connected() -> bool:
    try:
        client = await ensure_client()
        return await client.is_user_authorized()
    except Exception:
        return False


async def save_session():
    if tg_client and tg_client.is_connected():
        SESSION_FILE.write_text(tg_client.session.save(), encoding="utf-8")


async def get_chat_info(chat_id: int):
    client = await ensure_client()
    if not isinstance(chat_id, int):
        raise ValueError("O ID deve ser um número inteiro.")
    # IDs de supergrupos/canais normalmente aparecem como -100xxxxxxxxxx.
    # Não removemos o sinal: ele faz parte do identificador.
    entity = await client.get_entity(chat_id)
    if isinstance(entity, Chat):
        kind = "Grupo"
    elif isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            kind = "Fórum" if getattr(entity, "forum", False) else "Supergrupo"
        else:
            kind = "Canal"
    else:
        kind = type(entity).__name__
    title = getattr(entity, "title", None) or getattr(entity, "first_name", "Sem nome")
    return entity, title, kind


async def list_topics(destination: int):
    client = await ensure_client()
    entity = await client.get_entity(destination)
    if not isinstance(entity, Channel) or not getattr(entity, "megagroup", False):
        raise ValueError("O destino precisa ser um supergrupo.")
    if not getattr(entity, "forum", False):
        raise ValueError("O destino precisa ter Fórum/Tópicos ativados.")

    result = await client(GetForumTopicsRequest(
        peer=entity,
        offset_date=0,
        offset_id=0,
        offset_topic=0,
        limit=100,
    ))

    topics = []
    for topic in list(getattr(result, "topics", []) or []):
        if getattr(topic, "id", None) is None:
            continue
        if topic.__class__.__name__ == "ForumTopicDeleted":
            continue
        tid = getattr(topic, "id", 0)
        title = getattr(topic, "title", None) or ("Geral" if tid == 1 else f"Tópico {tid}")
        topics.append({
            "id": tid,
            "title": title,
            "closed": bool(getattr(topic, "closed", False)),
            "anchor": None if tid == 1 else getattr(topic, "top_message", None),
        })
    return entity, topics


def main_keyboard():
    running = state["process"] is not None and state["process"].returncode is None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Nova clonagem", callback_data="new")],
        [InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("🛑 Parar", callback_data="stop") if running else InlineKeyboardButton("🔄 Reconectar conta", callback_data="connect")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    connected = await account_connected()
    status = "🟢 Conta conectada" if connected else "🔴 Conta não conectada"
    await update.message.reply_text(
        f"🤖 **CloneCat Agent**\n\n{status}\n\nEscolha uma ação:",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )


async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    if await account_connected():
        await update.effective_message.reply_text("🟢 Sua conta já está conectada.")
        return
    global pending_phone, pending_phone_code_hash, pending_code_sent_at
    pending_phone = None
    pending_phone_code_hash = None
    pending_code_sent_at = None
    state["step"] = "phone"
    await update.effective_message.reply_text("📱 Envie o número da sua conta Telegram com código do país, por exemplo `+258...`.", parse_mode="Markdown")


async def new_clone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    if state["process"] is not None and state["process"].returncode is None:
        await update.effective_message.reply_text("⚠️ Já existe uma clonagem em execução.")
        return
    if not await account_connected():
        await update.effective_message.reply_text("🔴 Primeiro conecte sua conta com /connect.")
        return
    state.update({
        "step": "origin",
        "origin": None,
        "destination": None,
        "topics": [],
        "topic_index": None,
        "content_choice": None,
    })
    await update.effective_message.reply_text("📥 Envie o **ID numérico do grupo de origem**.", parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    origin = state.get("origin")
    resume = load_resume(origin) if origin else None
    if resume:
        await update.effective_message.reply_text(
            f"📊 **Status**\n\n"
            f"Origem: `{resume.get('origin_chat')}`\n"
            f"Destino: `{resume.get('destination_chat')}`\n"
            f"Processadas: {len(resume.get('cloned_msg_ids', []))}/{resume.get('total_msgs', 0)}\n"
            f"Enviadas: {resume.get('cloned_count', 0)}\n"
            f"Falhas: {resume.get('failed_count', 0)}",
            parse_mode="Markdown",
        )
    else:
        await update.effective_message.reply_text("ℹ️ Nenhum progresso salvo para a clonagem atual.")


async def stop_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    proc = state.get("process")
    if not proc or proc.returncode is not None:
        await update.effective_message.reply_text("ℹ️ Não há clonagem em execução.")
        return
    state["stop_requested"] = True
    proc.terminate()
    await update.effective_message.reply_text("🛑 Processo de clonagem solicitado para parar. O resume fica salvo para continuar depois.")


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "new":
        return await new_clone(update, context)
    if data == "connect":
        return await connect_start(update, context)
    if data == "status":
        return await status(update, context)
    if data == "stop":
        return await stop_job(update, context)

    if data.startswith("topic:"):
        idx = int(data.split(":", 1)[1])
        if idx < 0 or idx >= len(state["topics"]):
            return await q.edit_message_text("❌ Tópico inválido. Faça /start novamente.")
        topic = state["topics"][idx]
        if topic["closed"]:
            return await q.answer("Esse tópico está fechado.", show_alert=True)
        if topic["id"] != 1 and not topic["anchor"]:
            return await q.answer("Não foi possível obter a âncora desse tópico.", show_alert=True)
        state["topic_index"] = idx + 1  # o script original usa 1..N
        state["topic_id"] = topic["id"]
        state["topic_title"] = topic["title"]
        state["topic_anchor"] = topic["anchor"]
        state["step"] = "content"
        kb = [
            [InlineKeyboardButton(CONTENT_LABELS[1], callback_data="content:1")],
            [InlineKeyboardButton(CONTENT_LABELS[2], callback_data="content:2"), InlineKeyboardButton(CONTENT_LABELS[3], callback_data="content:3")],
            [InlineKeyboardButton(CONTENT_LABELS[4], callback_data="content:4"), InlineKeyboardButton(CONTENT_LABELS[5], callback_data="content:5")],
            [InlineKeyboardButton(CONTENT_LABELS[6], callback_data="content:6"), InlineKeyboardButton(CONTENT_LABELS[7], callback_data="content:7")],
        ]
        return await q.edit_message_text(
            f"📂 Tópico: **{topic['title']}**\n\nO que deseja clonar?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    if data.startswith("content:"):
        choice = int(data.split(":", 1)[1])
        state["content_choice"] = choice
        state["content_types"] = content_types_for(choice)
        resume = load_resume(state["origin"])
        state["resume_pending"] = bool(resume)
        if resume:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Continuar do resume", callback_data="resume:yes")],
                [InlineKeyboardButton("🆕 Começar novamente", callback_data="resume:no")],
            ])
            return await q.edit_message_text(
                f"⚠️ Existe um progresso salvo para esta origem.\n\n"
                f"Já clonadas: {len(resume.get('cloned_msg_ids', []))}/{resume.get('total_msgs', 0)}\n\n"
                f"Continuar do ponto salvo?",
                reply_markup=kb,
            )
        return await show_confirmation(q)

    if data == "resume:yes":
        state["resume_pending"] = True
        return await show_confirmation(q, resume=True)
    if data == "resume:no":
        state["resume_pending"] = False
        return await show_confirmation(q, resume=False)
    if data == "confirm":
        return await launch_clone(q, resume=state["resume_pending"])
    if data == "cancel":
        state["step"] = "idle"
        return await q.edit_message_text("❌ Operação cancelada.")


async def show_confirmation(message, resume=False):
    text = (
        "⚠️ **CONFIRMAR CLONAGEM**\n\n"
        f"Origem: `{state['origin']}` — {state['origin_title']}\n"
        f"Destino: `{state['destination']}` — {state['destination_title']}\n"
        f"Tópico: **{state['topic_title']}**\n"
        f"Conteúdo: **{CONTENT_LABELS[state['content_choice']]}**\n"
    )
    if resume:
        text += "\n♻️ O processo continuará usando o `resume_forum.json`."
    else:
        text += "\n🆕 Será iniciada uma nova clonagem para essa origem."
    text += "\n\nProsseguir?"
    return await message.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ CONFIRMAR", callback_data="confirm")],
            [InlineKeyboardButton("❌ CANCELAR", callback_data="cancel")],
        ]),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    text = (update.message.text or "").strip()

    global pending_phone, pending_phone_code_hash

    try:
        if state["step"] == "phone":
            client = await ensure_client()
            try:
                sent = await client.send_code_request(text)
            except PhoneNumberInvalidError:
                return await update.message.reply_text("❌ Número inválido. Envie o número com código do país, por exemplo +258...")
            except FloodWaitError as exc:
                return await update.message.reply_text(f"⏳ O Telegram pediu para aguardar {exc.seconds}s antes de solicitar outro código.")
            pending_phone = text
            pending_phone_code_hash = sent.phone_code_hash
            pending_code_sent_at = asyncio.get_running_loop().time()
            state["step"] = "code"
            await update.message.reply_text(
                "🔐 Envie o código de login **novo** que o Telegram acabou de enviar.\n\n"
                "Se ele expirar, envie /connect para iniciar uma nova tentativa.",
                parse_mode="Markdown",
            )
            return

        if state["step"] == "code":
            client = await ensure_client()
            try:
                if not pending_phone or not pending_phone_code_hash:
                    state["step"] = "phone"
                    return await update.message.reply_text("⚠️ A tentativa de login expirou. Envie novamente o seu número.")
                await client.sign_in(phone=pending_phone, code=text, phone_code_hash=pending_phone_code_hash)
            except PhoneCodeExpiredError:
                pending_phone = None
                pending_phone_code_hash = None
                pending_code_sent_at = None
                state["step"] = "phone"
                return await update.message.reply_text(
                    "⌛ O código expirou. Envie novamente o número para receber **um código novo**.",
                    parse_mode="Markdown",
                )
            except PhoneCodeInvalidError:
                return await update.message.reply_text("❌ Código incorreto. Envie o código atual novamente.")
            except SessionPasswordNeededError:
                state["step"] = "password"
                await update.message.reply_text("🔑 Sua conta usa verificação em duas etapas. Envie a senha 2FA.")
                return
            await save_session()
            pending_phone = None
            pending_phone_code_hash = None
            pending_code_sent_at = None
            state["step"] = "idle"
            await update.message.reply_text("🟢 Conta conectada e sessão salva no Volume do Railway.", reply_markup=main_keyboard())
            return

        if state["step"] == "password":
            client = await ensure_client()
            await client.sign_in(password=text)
            await save_session()
            state["step"] = "idle"
            await update.message.reply_text("🟢 Conta conectada e sessão salva.", reply_markup=main_keyboard())
            return

        if state["step"] == "origin":
            try:
                origin = int(text, 10)
            except ValueError:
                return await update.message.reply_text("❌ ID inválido. Envie somente o número, incluindo o sinal - se houver (ex.: -1001234567890).")
            try:
                entity, title, kind = await get_chat_info(origin)
            except Exception as exc:
                logging.exception("Erro acessando origem %s: %s", origin, exc)
                return await update.message.reply_text(f"❌ Não consegui acessar o grupo de origem `{origin}`.\n\nDetalhes: `{exc}`", parse_mode="Markdown")
            state["origin"] = origin
            state["origin_title"] = title
            state["step"] = "destination"
            await update.message.reply_text(
                f"✅ Origem encontrada.\n\n**{title}**\nTipo: {kind}\nID: `{origin}`\n\n"
                "Agora envie o **ID numérico do grupo de destino**.",
                parse_mode="Markdown",
            )
            return

        if state["step"] == "destination":
            try:
                destination = int(text, 10)
            except ValueError:
                return await update.message.reply_text("❌ ID inválido. Envie somente o número, incluindo o sinal - se houver (ex.: -1001234567890).")
            try:
                entity, topics = await list_topics(destination)
            except Exception as exc:
                logging.exception("Erro acessando destino %s: %s", destination, exc)
                return await update.message.reply_text(f"❌ Não consegui acessar/listar os tópicos do destino `{destination}`.\n\nDetalhes: `{exc}`", parse_mode="Markdown")
            state["destination"] = entity.id
            state["destination_title"] = getattr(entity, "title", "Sem nome")
            state["topics"] = topics
            state["step"] = "topic"
            rows = []
            for i, topic in enumerate(topics):
                status = " 🔒" if topic["closed"] else ""
                rows.append([InlineKeyboardButton(f"{i+1}. {topic['title']}{status}", callback_data=f"topic:{i}")])
            if not rows:
                raise ValueError("Nenhum tópico encontrado.")
            await update.message.reply_text(
                f"✅ Fórum de destino: **{state['destination_title']}**\n\nSelecione o tópico:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        await update.message.reply_text("Use /start para abrir o menu.")
    except Exception as exc:
        logging.exception("Erro no fluxo: %s", exc)
        await update.message.reply_text(f"❌ Erro: {exc}")


async def launch_clone(message, resume=False):
    if state["process"] is not None and state["process"].returncode is None:
        return await message.edit_message_text("⚠️ Já existe uma clonagem em execução.")
    if not SCRIPT.exists():
        return await message.edit_message_text(f"❌ Script não encontrado: {SCRIPT}")

    # O motor original lê API ID/HASH do config.json e sessão de session.txt.
    write_config()

    inputs = [str(state["origin"])]
    if resume:
        inputs.append("s")
    else:
        inputs += ["n" if load_resume(state["origin"]) else "", str(state["destination"]), str(state["topic_index"]), str(state["content_choice"])]
        # Se não havia resume, não existe prompt para "n".
        if inputs[1] == "":
            inputs.pop(1)

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(BASE_DIR),
        )
        state["process"] = proc
        state["step"] = "running"
        state["stop_requested"] = False

        proc.stdin.write(("\n".join(inputs) + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        status_msg = await message.edit_message_text("🚀 **Clonagem iniciada!**\n\nConsultando progresso salvo...", parse_mode="Markdown")
        state["status_message"] = status_msg
        state["job_task"] = asyncio.create_task(monitor_job(proc, status_msg))
    except Exception as exc:
        logging.exception("Falha iniciando clonecat: %s", exc)
        state["process"] = None
        state["step"] = "idle"
        await message.edit_message_text(f"❌ Não foi possível iniciar a clonagem: {exc}")


async def drain_process_output(proc):
    """Consome stdout do clonecat para evitar bloqueio do buffer do subprocesso."""
    lines = []
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                lines.append(line)
                if len(lines) > 80:
                    lines.pop(0)
                logging.info("clonecat: %s", line)
    except Exception as exc:
        logging.exception("Erro lendo saída do clonecat: %s", exc)
    return lines


async def monitor_job(proc, message):
    last_text = ""
    output_task = asyncio.create_task(drain_process_output(proc))
    while proc.returncode is None:
        await asyncio.sleep(15)
        resume = load_resume(state.get("origin")) if state.get("origin") else None
        if resume:
            done = len(resume.get("cloned_msg_ids", []))
            total = resume.get("total_msgs", 0)
            failed = resume.get("failed_count", 0)
            text = (
                "🚀 **Clonagem em execução**\n\n"
                f"Origem: `{resume.get('origin_chat')}`\n"
                f"Destino: `{resume.get('destination_chat')}`\n"
                f"Progresso salvo: **{done}/{total}**\n"
                f"Enviadas: {resume.get('cloned_count', 0)}\n"
                f"Falhas: {failed}"
            )
            if text != last_text:
                try:
                    await message.edit_text(text, parse_mode="Markdown")
                except Exception:
                    pass
                last_text = text

    rc = await proc.wait()
    try:
        output_lines = await output_task
    except Exception:
        output_lines = []

    # Se o processo morreu inesperadamente, aproveita o resume e tenta
    # continuar automaticamente. Uma parada solicitada pelo usuário não é reiniciada.
    if rc != 0 and not state.get("stop_requested") and state.get("restart_count", 0) < 5:
        resume = load_resume(state.get("origin")) if state.get("origin") else None
        if resume:
            state["restart_count"] = state.get("restart_count", 0) + 1
            state["process"] = None
            try:
                await message.edit_text(
                    f"⚠️ O processo terminou inesperadamente (código {rc}).\n"
                    f"♻️ Reiniciando com o resume... tentativa {state['restart_count']}/5",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            await asyncio.sleep(10)
            try:
                new_proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", str(SCRIPT),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(BASE_DIR),
                )
                state["process"] = new_proc
                new_proc.stdin.write((str(state["origin"]) + "\ns\n").encode())
                await new_proc.stdin.drain()
                new_proc.stdin.close()
                return await monitor_job(new_proc, message)
            except Exception as exc:
                logging.exception("Falha no reinício automático: %s", exc)

    state["process"] = None
    state["job_task"] = None
    state["step"] = "idle"

    resume = load_resume(state.get("origin")) if state.get("origin") else None
    if rc == 0:
        summary = "✅ **Clonagem concluída.**"
    else:
        tail = "\n".join(output_lines[-12:]) if output_lines else "(sem saída capturada)"
        summary = (
            f"⚠️ **O processo terminou com código {rc}.**\n"
            "O `resume_forum.json` permanece salvo para continuar.\n\n"
            f"**Últimas linhas do processo:**\n```\n{tail}\n```"
        )
    if resume:
        summary += (
            f"\n\nEnviadas: {resume.get('cloned_count', 0)}"
            f"\nFalhas: {resume.get('failed_count', 0)}"
            f"\nProcessadas/salvas: {len(resume.get('cloned_msg_ids', []))}/{resume.get('total_msgs', 0)}"
        )
    try:
        await message.edit_text(summary, parse_mode="Markdown", reply_markup=main_keyboard())
    except Exception:
        pass


async def post_init(application: Application):
    if API_ID and API_HASH:
        write_config()
    try:
        if SESSION_FILE.exists():
            await ensure_client()
            logging.info("Sessão existente conectada.")
    except Exception as exc:
        logging.warning("Sessão existente não pôde ser conectada: %s", exc)


async def post_shutdown(application: Application):
    global tg_client
    try:
        if tg_client:
            await save_session()
            await tg_client.disconnect()
    except Exception:
        logging.exception("Erro fechando Telethon")


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN não configurado")
    if not ADMIN_USER_ID:
        raise SystemExit("ADMIN_USER_ID não configurado")
    if not API_ID or not API_HASH:
        raise SystemExit("API_ID/API_HASH não configurados")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect_start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop_job))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
