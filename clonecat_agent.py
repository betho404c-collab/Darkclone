import asyncio
import json
import logging
import os
import threading
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

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

from clonecat_engine import run_clone, CloneStopped

BASE_DIR = Path(os.getenv("DATA_DIR", "/data"))
BASE_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = BASE_DIR / "session.txt"
CONFIG_FILE = BASE_DIR / "config.json"
RESUME_FILE = BASE_DIR / "resume_forum.json"
LOG_FILE = BASE_DIR / "agent.log"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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
    "content_choice": None,
    "resume_pending": False,
    "job_task": None,
    "stop_event": None,
    "status_message": None,
    "last_progress": {},
}

tg_client = None
pending_phone = None
pending_phone_code_hash = None


def authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ADMIN_USER_ID and user.id == ADMIN_USER_ID)


async def deny(update: Update):
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Acesso não autorizado.")


def write_config():
    CONFIG_FILE.write_text(json.dumps({"api_id": API_ID, "api_hash": API_HASH}), encoding="utf-8")


def load_resume(origin):
    if not origin or not RESUME_FILE.exists():
        return None
    try:
        data = json.loads(RESUME_FILE.read_text(encoding="utf-8"))
        return data.get(str(origin))
    except Exception:
        logging.exception("Erro lendo resume")
        return None


def content_types_for(choice):
    if choice in (1, 8):
        return ["text", "photo", "video", "audio", "document", "sticker"]
    return {2:["photo"], 3:["video"], 4:["audio"], 5:["document"], 6:["text"], 7:["sticker"]}[choice]


async def ensure_client():
    global tg_client
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Configure API_ID e API_HASH no Railway.")
    session = SESSION_FILE.read_text(encoding="utf-8").strip() if SESSION_FILE.exists() else ""
    if tg_client is None:
        tg_client = TelegramClient(
            StringSession(session), API_ID, API_HASH,
            connection_retries=20, request_retries=20,
            retry_delay=5, auto_reconnect=True,
        )
    if not tg_client.is_connected():
        await tg_client.connect()
    return tg_client


async def account_connected():
    try:
        client = await ensure_client()
        return await client.is_user_authorized()
    except Exception:
        return False


async def save_session():
    if tg_client and tg_client.is_connected():
        SESSION_FILE.write_text(tg_client.session.save(), encoding="utf-8")


async def get_chat_info(chat_id):
    client = await ensure_client()
    entity = await client.get_entity(int(chat_id))
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


async def list_topics(destination):
    client = await ensure_client()
    entity = await client.get_entity(int(destination))
    if not isinstance(entity, Channel) or not getattr(entity, "megagroup", False):
        raise ValueError("O destino precisa ser um supergrupo.")
    if not getattr(entity, "forum", False):
        raise ValueError("O destino precisa ter Fórum/Tópicos ativados.")
    result = await client(GetForumTopicsRequest(
        peer=entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
    ))
    topics = []
    for topic in list(getattr(result, "topics", []) or []):
        if getattr(topic, "id", None) is None or topic.__class__.__name__ == "ForumTopicDeleted":
            continue
        tid = getattr(topic, "id", 0)
        topics.append({
            "id": tid,
            "title": getattr(topic, "title", None) or ("Geral" if tid == 1 else f"Tópico {tid}"),
            "closed": bool(getattr(topic, "closed", False)),
            "anchor": None if tid == 1 else getattr(topic, "top_message", None),
        })
    return entity, topics


def main_keyboard():
    running = state["job_task"] is not None and not state["job_task"].done()
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
    await update.message.reply_text(f"🤖 **CloneCat Agent**\n\n{status}\n\nEscolha uma ação:", parse_mode="Markdown", reply_markup=main_keyboard())


async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    if await account_connected():
        await update.effective_message.reply_text("🟢 Sua conta já está conectada.")
        return
    state["step"] = "phone"
    await update.effective_message.reply_text("📱 Envie o número da sua conta Telegram com código do país, por exemplo `+258...`.", parse_mode="Markdown")


async def new_clone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    if state["job_task"] is not None and not state["job_task"].done():
        await update.effective_message.reply_text("⚠️ Já existe uma clonagem em execução.")
        return
    if not await account_connected():
        await update.effective_message.reply_text("🔴 Primeiro conecte sua conta com /connect.")
        return
    state.update({"step":"origin", "origin":None, "destination":None, "topics":[], "topic_index":None, "content_choice":None, "resume_pending":False})
    await update.effective_message.reply_text("📥 Envie o **ID do grupo de origem**. Ex.: `-1001234567890`", parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    origin = state.get("origin")
    resume = load_resume(origin) if origin else None
    p = state.get("last_progress", {})
    if resume:
        text = (
            f"📊 **Status**\n\nOrigem: `{resume.get('origin_chat')}`\n"
            f"Destino: `{resume.get('destination_chat')}`\n"
            f"Processadas: {len(resume.get('cloned_msg_ids', []))}/{resume.get('total_msgs', 0)}\n"
            f"Enviadas: {resume.get('cloned_count', 0)}\n"
            f"Falhas: {resume.get('failed_count', 0)}"
        )
    elif p:
        text = f"📊 **Status atual**\n\nTotal: {p.get('total', '?')}\nEnviadas: {p.get('cloned', 0)}\nFalhas: {p.get('failed', 0)}"
    else:
        text = "ℹ️ Nenhum progresso salvo."
    await update.effective_message.reply_text(text, parse_mode="Markdown")


async def stop_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    task = state.get("job_task")
    ev = state.get("stop_event")
    if not task or task.done() or not ev:
        await update.effective_message.reply_text("ℹ️ Não há clonagem em execução.")
        return
    ev.set()
    await update.effective_message.reply_text("🛑 Pedido de parada enviado. O motor salvará o resume antes de parar.")


async def show_topics(message):
    rows = []
    for i, topic in enumerate(state["topics"]):
        suffix = " 🔒" if topic["closed"] else ""
        rows.append([InlineKeyboardButton(f"{i+1}. {topic['title']}{suffix}", callback_data=f"topic:{i}")])
    return await message.reply_text(
        f"📂 **{state['destination_title']}**\n\nSelecione o tópico de destino:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )


async def show_confirmation(message):
    resume = load_resume(state["origin"])
    state["resume_pending"] = bool(resume)
    text = (
        "⚠️ **CONFIRMAR CLONAGEM**\n\n"
        f"Origem: `{state['origin']}` — {state['origin_title']}\n"
        f"Destino: `{state['destination']}` — {state['destination_title']}\n"
        f"Tópico: **{state['topic_title']}**\n"
        f"Conteúdo: **{CONTENT_LABELS[state['content_choice']]}**\n"
    )
    if resume:
        text += f"\n♻️ Existe um resume para esta origem: {len(resume.get('cloned_msg_ids', []))}/{resume.get('total_msgs', 0)}.\n"
        text += "O motor poderá continuar do ponto salvo."
        kb = [[InlineKeyboardButton("▶️ Continuar", callback_data="resume:yes")], [InlineKeyboardButton("🆕 Começar novamente", callback_data="resume:no")], [InlineKeyboardButton("❌ Cancelar", callback_data="cancel")]]
    else:
        text += "\n🆕 Não existe resume para esta origem."
        kb = [[InlineKeyboardButton("✅ CONFIRMAR", callback_data="resume:no")], [InlineKeyboardButton("❌ CANCELAR", callback_data="cancel")]]
    return await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "new": return await new_clone(update, context)
    if data == "connect": return await connect_start(update, context)
    if data == "status": return await status(update, context)
    if data == "stop": return await stop_job(update, context)
    if data == "cancel":
        state["step"] = "idle"
        return await q.edit_message_text("❌ Operação cancelada.")
    if data.startswith("topic:"):
        idx = int(data.split(":",1)[1])
        if idx < 0 or idx >= len(state["topics"]):
            return await q.edit_message_text("❌ Tópico inválido.")
        topic = state["topics"][idx]
        if topic["closed"]:
            return await q.answer("Esse tópico está fechado.", show_alert=True)
        if topic["id"] != 1 and not topic["anchor"]:
            return await q.answer("Não foi possível obter a âncora desse tópico.", show_alert=True)
        state["topic_index"] = idx + 1
        state["topic_id"] = topic["id"]
        state["topic_title"] = topic["title"]
        state["step"] = "content"
        kb = [
            [InlineKeyboardButton(CONTENT_LABELS[1], callback_data="content:1")],
            [InlineKeyboardButton(CONTENT_LABELS[2], callback_data="content:2"), InlineKeyboardButton(CONTENT_LABELS[3], callback_data="content:3")],
            [InlineKeyboardButton(CONTENT_LABELS[4], callback_data="content:4"), InlineKeyboardButton(CONTENT_LABELS[5], callback_data="content:5")],
            [InlineKeyboardButton(CONTENT_LABELS[6], callback_data="content:6"), InlineKeyboardButton(CONTENT_LABELS[7], callback_data="content:7")],
        ]
        return await q.edit_message_text(f"📂 Tópico: **{topic['title']}**\n\nO que deseja clonar?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    if data.startswith("content:"):
        choice = int(data.split(":",1)[1])
        state["content_choice"] = choice
        state["content_types"] = content_types_for(choice)
        return await show_confirmation(q)
    if data == "resume:yes":
        state["resume_pending"] = True
        return await launch_clone(q, True, context.bot)
    if data == "resume:no":
        state["resume_pending"] = False
        return await launch_clone(q, False, context.bot)


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
                return await update.message.reply_text("❌ Número inválido. Envie o número completo com código do país.")
            pending_phone, pending_phone_code_hash = text, sent.phone_code_hash
            state["step"] = "code"
            return await update.message.reply_text("🔐 Código enviado. Envie o código imediatamente.")
        if state["step"] == "code":
            client = await ensure_client()
            try:
                await client.sign_in(phone=pending_phone, code=text, phone_code_hash=pending_phone_code_hash)
            except PhoneCodeExpiredError:
                pending_phone = pending_phone_code_hash = None
                state["step"] = "phone"
                return await update.message.reply_text("⌛ O código expirou. Envie novamente o número para receber um código novo.")
            except PhoneCodeInvalidError:
                return await update.message.reply_text("❌ Código incorreto. Envie o código correto ou use /connect para iniciar novamente.")
            except SessionPasswordNeededError:
                state["step"] = "password"
                return await update.message.reply_text("🔑 Sua conta usa 2FA. Envie a senha de duas etapas.")
            await save_session()
            state["step"] = "idle"
            return await update.message.reply_text("🟢 Conta conectada e sessão salva.", reply_markup=main_keyboard())
        if state["step"] == "password":
            client = await ensure_client()
            await client.sign_in(password=text)
            await save_session()
            state["step"] = "idle"
            return await update.message.reply_text("🟢 Conta conectada e sessão salva.", reply_markup=main_keyboard())
        if state["step"] == "origin":
            try:
                origin = int(text)
            except ValueError:
                return await update.message.reply_text("❌ ID inválido. Use um número como `-1001234567890`.", parse_mode="Markdown")
            _, title, kind = await get_chat_info(origin)
            state["origin"], state["origin_title"] = origin, title
            state["step"] = "destination"
            return await update.message.reply_text(f"✅ Origem encontrada.\n\n**{title}**\nTipo: {kind}\nID: `{origin}`\n\n📤 Agora envie o **ID do grupo de destino**.", parse_mode="Markdown")
        if state["step"] == "destination":
            try:
                destination = int(text)
            except ValueError:
                return await update.message.reply_text("❌ ID inválido. Use um número como `-1001234567890`.", parse_mode="Markdown")
            entity, topics = await list_topics(destination)
            state["destination"] = destination  # preserva exatamente o ID fornecido
            state["destination_title"] = getattr(entity, "title", "Sem nome")
            state["topics"] = topics
            state["step"] = "topic"
            if not topics:
                raise ValueError("Nenhum tópico encontrado.")
            return await show_topics(update.message)
        await update.message.reply_text("Use /start para abrir o menu.")
    except FloodWaitError as e:
        await update.message.reply_text(f"⏳ Telegram pediu espera de {e.seconds}s. Tente novamente depois.")
    except Exception as exc:
        logging.exception("Erro no fluxo")
        state["step"] = "idle"
        await update.message.reply_text(f"❌ Erro: {exc}")


async def progress_watcher(bot, chat_id):
    last = None
    while True:
        task = state.get("job_task")
        if not task or task.done():
            return
        p = state.get("last_progress", {})
        if p != last:
            last = dict(p)
            try:
                total = p.get("total", 0)
                cloned = p.get("cloned", 0)
                failed = p.get("failed", 0)
                skipped = p.get("skipped", 0)
                await bot.edit_message_text(chat_id=chat_id, message_id=state["status_message"], text=(
                    f"🚀 **Clonagem em execução**\n\n"
                    f"📊 Histórico: {total:,} mensagens\n"
                    f"✅ Enviadas: {cloned:,}\n"
                    f"⏭ Puladas: {skipped:,}\n"
                    f"❌ Falhas: {failed:,}"
                ), parse_mode="Markdown")
            except Exception:
                pass
        await asyncio.sleep(5)


async def launch_clone(message, resume, bot):
    if state["job_task"] is not None and not state["job_task"].done():
        return await message.edit_message_text("⚠️ Já existe uma clonagem em execução.")
    write_config()
    state["step"] = "running"
    state["stop_event"] = threading.Event()
    state["last_progress"] = {"event":"starting", "total":0, "cloned":0, "failed":0, "skipped":0}
    status = await message.edit_message_text("🚀 **Clonagem iniciada.**\n\n🔎 O motor está consultando o histórico completo da origem...", parse_mode="Markdown")
    state["status_message"] = status.message_id

    # Evita dois clientes MTProto usando a mesma sessão simultaneamente.
    if tg_client and tg_client.is_connected():
        await tg_client.disconnect()

    def progress_cb(info):
        state["last_progress"] = dict(info)

    async def runner():
        try:
            return await asyncio.to_thread(
                run_clone,
                state["origin"], state["destination"], state["topic_index"],
                state["content_choice"], "s" if resume else "n",
                str(BASE_DIR), progress_cb, state["stop_event"]
            )
        except CloneStopped:
            return "stopped"
        finally:
            try:
                await ensure_client()
            except Exception:
                pass

    state["job_task"] = asyncio.create_task(runner())
    asyncio.create_task(progress_watcher(bot, message.message.chat_id if message.message else ADMIN_USER_ID))

    try:
        result = await state["job_task"]
        p = state.get("last_progress", {})
        if result == "stopped":
            await message.edit_text("🛑 **Clonagem interrompida.**\n\nO `resume_forum.json` foi salvo e pode ser retomado.", parse_mode="Markdown")
        else:
            await message.edit_text(
                f"✅ **Clonagem concluída.**\n\n📊 Total: {p.get('total', 0):,}\n"
                f"✅ Enviadas: {p.get('cloned', 0):,}\n⏭ Puladas: {p.get('skipped', 0):,}\n❌ Falhas: {p.get('failed', 0):,}",
                parse_mode="Markdown"
            )
    except Exception as exc:
        logging.exception("Motor de clonagem falhou")
        await message.edit_text(f"❌ **Erro no motor de clonagem:**\n`{str(exc)[:3500]}`\n\nO resume permanece salvo quando possível.", parse_mode="Markdown")
    finally:
        state["step"] = "idle"
        state["job_task"] = None
        state["stop_event"] = None


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN não configurado.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("connect", connect_start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("CloneCat Agent iniciado.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
