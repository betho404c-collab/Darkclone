import os
import json
import logging
import re
import shutil
import subprocess
import time
import random
from concurrent.futures import ThreadPoolExecutor
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, GetFullChatRequest, GetForumTopicsRequest
from telethon.tl.functions.messages import SendMessageRequest, SendMediaRequest
from telethon.errors.rpcerrorlist import FloodWaitError, RPCError, ChatAdminRequiredError
from telethon.tl.types import MessageService, Channel, Chat
from colorama import Fore, Style, init
from tqdm import tqdm

# Inicializa cores no terminal
init(autoreset=True)

# Configuração do log para salvar erros
logging.basicConfig(filename="erros.log", level=logging.WARNING, format="%(asctime)s - %(message)s")

# Diretório temporário para salvar mídias
TEMP_DIR = os.getenv("CLONECAT_TEMP_DIR", "temp_media")
os.makedirs(TEMP_DIR, exist_ok=True)

# Arquivo de retomada (resume)
RESUME_FILE = "resume_forum.json"

# Intervalo entre envios (em segundos) - ajustado para nao disparar flood
MEDIA_DELAY = 2    # 2s entre midias
TEXT_DELAY = 0.5   # 0.5s entre textos

# Formata bytes para exibicao humana
def fmt_size(b):
    if b >= 1024 * 1024:
        return f"{b / (1024 * 1024):.1f}MB"
    return f"{b / 1024:.0f}KB"

# Função para exibir ASCII Art
def print_ascii_art():
    ascii_art = rf"""{Fore.GREEN}
  ______  __        ______   .__   __.  _______   ______      ___      .___________.
 /      ||  |      /  __  \  |  \ |  | |   ____| /      |    /   \     |           |
|  ,----'|  |     |  |  |  | |   \|  | |  |__   |  ,----'   /  ^  \    `---|  |----`
|  |     |  |     |  |  |  | |  . `  | |   __|  |  |       /  /_\  \       |  |     
|  `----.|  `----.|  `--'  | |  |\   | |  |____ |  `----. /  _____  \      |  |     
 \______||_______| \______/  |__| \__| |_______| \______|/__/     \__\     |__|     
    """
    print(ascii_art + Style.RESET_ALL)

# Função para coletar ou carregar API ID e Hash
def get_api_credentials():
    config_path = "config.json"

    if os.path.exists(config_path):
        with open(config_path, "r") as file:
            credentials = json.load(file)
            print("Credenciais carregadas com sucesso.")
            return credentials["api_id"], credentials["api_hash"]

    api_id = input("Digite seu API ID: ").strip()
    api_hash = input("Digite seu API Hash: ").strip()
    if not api_id or not api_hash:
        print("API ID ou API Hash inválidos!")
        exit()

    with open(config_path, "w") as file:
        json.dump({"api_id": int(api_id), "api_hash": api_hash}, file)
        print("Credenciais salvas com sucesso.")

    return int(api_id), api_hash

# ============================================================
# Retomada (resume): salva e carrega progresso
# ============================================================

def save_resume(data):
    """Salva estado da clonagem para retomada futura."""
    try:
        all_resumes = {}
        if os.path.exists(RESUME_FILE):
            with open(RESUME_FILE, "r") as f:
                all_resumes = json.load(f)
        all_resumes[str(data["origin_chat"])] = {
            "destination_chat": data["destination_chat"],
            "chat_type": data["chat_type"],
            "origin_title": data.get("origin_title", ""),
            "topic_map": {str(k): v for k, v in data.get("topic_map", {}).items()},
            "cloned_msg_ids": sorted(data.get("cloned_msg_ids", [])),
            "total_msgs": data.get("total_msgs", 0),
            "skipped_count": data.get("skipped_count", 0),
            "cloned_count": data.get("cloned_count", 0),
            "failed_count": data.get("failed_count", 0),
            "content_types": data.get("content_types", []),
            "destination_topic_title": data.get("destination_topic_title", ""),
        }
        with open(RESUME_FILE, "w") as f:
            json.dump(all_resumes, f)
    except Exception as e:
        tqdm.write(f"  ⚠ Erro ao salvar resume: {e}")

def load_resume(origin_chat):
    """Carrega estado salvo para o chat de origem, ou None se não existir."""
    if not os.path.exists(RESUME_FILE):
        return None
    try:
        with open(RESUME_FILE, "r") as f:
            all_resumes = json.load(f)
        key = str(origin_chat)
        if key not in all_resumes:
            return None
        data = all_resumes[key]
        data["origin_chat"] = origin_chat
        data["topic_map"] = {int(k): v for k, v in data.get("topic_map", {}).items()}
        data["cloned_msg_ids"] = set(data.get("cloned_msg_ids", []))
        return data
    except Exception as e:
        print(f"  ⚠ Erro ao carregar resume: {e}")
        return None

def delete_resume(origin_chat):
    """Remove o resume de um chat específico."""
    if not os.path.exists(RESUME_FILE):
        return
    try:
        with open(RESUME_FILE, "r") as f:
            all_resumes = json.load(f)
        all_resumes.pop(str(origin_chat), None)
        with open(RESUME_FILE, "w") as f:
            json.dump(all_resumes, f)
    except Exception:
        pass

# ============================================================
# SEÇÃO 1: Detecção do tipo de chat de origem
# ============================================================

def get_chat_info(client, chat_id):
    """Detecta tipo do chat (canal, grupo, supergrupo, forum), proteção e atributos."""
    try:
        entity = client.get_entity(chat_id)
    except Exception as e:
        print(f"\n❌ Não foi possível acessar o chat {chat_id}")
        print(f"Detalhes: {e}")
        exit()

    info = {
        "entity": entity,
        "title": getattr(entity, 'title', None) or getattr(entity, 'first_name', ''),
        "id": entity.id,
        "is_protected": False,
        "is_forum": False,
        "type": "channel",
        "type_label": "Canal",
    }

    if isinstance(entity, Chat):
        info["type"] = "group"
        info["type_label"] = "Grupo Comum"
        try:
            full = client(GetFullChatRequest(chat_id=entity.id))
            fc = full.full_chat
            if hasattr(fc, 'noforwards') and fc.noforwards:
                info["is_protected"] = True
        except Exception:
            pass
    elif isinstance(entity, Channel):
        if getattr(entity, 'megagroup', False):
            if getattr(entity, 'forum', False):
                info["type"] = "forum"
                info["type_label"] = "Supergrupo com Tópicos (Fórum)"
                info["is_forum"] = True
            else:
                info["type"] = "supergroup"
                info["type_label"] = "Supergrupo"
        else:
            info["type"] = "channel"
            info["type_label"] = "Canal"

        try:
            full = client(GetFullChannelRequest(entity))
            fc = full.full_chat
            if hasattr(fc, 'noforwards') and fc.noforwards:
                info["is_protected"] = True
            if hasattr(fc, 'restricted') and fc.restricted:
                info["is_protected"] = True
            if hasattr(fc, 'protected') and fc.protected:
                info["is_protected"] = True
        except Exception:
            pass

    print(f"\n{Fore.CYAN}Chat detectado: {info['title']}{Style.RESET_ALL}")
    print(f"  Tipo: {info['type_label']}")
    print(f"  ID: {info['id']}")
    if info["is_protected"]:
        print(f"  {Fore.YELLOW}Proteção de conteúdo: ATIVADA{Style.RESET_ALL}")
    else:
        print(f"  Proteção de conteúdo: desativada")
    if info["is_forum"]:
        print(f"  Tópicos: SIM")

    return info

# ============================================================
# SEÇÃO 2: Seleção do grupo Fórum e do tópico de destino
# ============================================================

def select_destination_topic(client):
    """Pede o ID de um Fórum existente, lista seus tópicos e deixa o
    usuário escolher exatamente em qual tópico a clonagem será enviada.

    Retorna:
        destination_chat: ID do Fórum de destino
        topic_map: {0: top_message_do_topico}
        selected_topic_title: nome do tópico escolhido
    """
    print(f"\n{Fore.CYAN}=== Destino da clonagem ==={Style.RESET_ALL}")
    print("O grupo de destino deve ser um Supergrupo com Fórum/Tópicos.")

    raw_id = input("Digite o ID do grupo de destino (ID numérico): ").strip()
    try:
        destination_chat = int(raw_id)
    except ValueError:
        print(f"{Fore.RED}ID inválido. Digite um número inteiro.{Style.RESET_ALL}")
        return select_destination_topic(client)

    try:
        destination_entity = client.get_entity(destination_chat)
    except Exception as e:
        print(f"{Fore.RED}Não foi possível acessar o grupo de destino: {e}{Style.RESET_ALL}")
        return select_destination_topic(client)

    if not isinstance(destination_entity, Channel):
        print(f"{Fore.RED}O destino informado não é um Supergrupo/Fórum.{Style.RESET_ALL}")
        return select_destination_topic(client)

    if not getattr(destination_entity, "megagroup", False):
        print(f"{Fore.RED}O destino é um canal, não um Supergrupo.{Style.RESET_ALL}")
        return select_destination_topic(client)

    if not getattr(destination_entity, "forum", False):
        print(f"{Fore.RED}O destino não possui Fórum/Tópicos ativados.{Style.RESET_ALL}")
        return select_destination_topic(client)

    destination_chat = destination_entity.id
    print(f"\n{Fore.GREEN}Fórum de destino encontrado!{Style.RESET_ALL}")
    print(f"  Nome: {getattr(destination_entity, 'title', 'Sem nome')}")
    print(f"  ID: {destination_chat}")

    try:
        # Em Telethon 1.44, GetForumTopicsRequest usa 'peer'.
        result = client(GetForumTopicsRequest(
            peer=destination_entity,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100
        ))
        topics = list(getattr(result, "topics", []) or [])
    except FloodWaitError as e:
        print(f"{Fore.YELLOW}FloodWait: aguardando {e.seconds}s...{Style.RESET_ALL}")
        time.sleep(e.seconds)
        return select_destination_topic(client)
    except Exception as e:
        print(f"{Fore.RED}Não foi possível ler os tópicos do destino: {e}{Style.RESET_ALL}")
        logging.error(f"Erro ao listar tópicos do destino {destination_chat}: {e}")
        return select_destination_topic(client)

    # Remove tópicos que o Telegram devolva como apagados, quando aplicável.
    valid_topics = []
    for topic in topics:
        if getattr(topic, "id", None) is None:
            continue
        if topic.__class__.__name__ == "ForumTopicDeleted":
            continue
        valid_topics.append(topic)

    if not valid_topics:
        print(f"{Fore.RED}Nenhum tópico foi encontrado nesse Fórum.{Style.RESET_ALL}")
        return select_destination_topic(client)

    print(f"\n{Fore.CYAN}Tópicos disponíveis:{Style.RESET_ALL}")
    for number, topic in enumerate(valid_topics, 1):
        topic_id = getattr(topic, "id", 0)
        title = getattr(topic, "title", None) or ("Geral" if topic_id == 1 else f"Tópico {topic_id}")
        closed = getattr(topic, "closed", False)
        status = " [FECHADO]" if closed else ""
        print(f"  {number} - {title}{status} (ID: {topic_id})")

    while True:
        choice = input(f"\nEscolha o tópico de destino (1-{len(valid_topics)}): ").strip()
        try:
            index = int(choice) - 1
            if 0 <= index < len(valid_topics):
                break
        except ValueError:
            pass
        print(f"{Fore.YELLOW}Opção inválida. Escolha um número da lista.{Style.RESET_ALL}")

    selected = valid_topics[index]
    selected_id = getattr(selected, "id", 0)
    selected_title = getattr(selected, "title", None) or ("Geral" if selected_id == 1 else f"Tópico {selected_id}")

    # Para enviar diretamente a um tópico no Telethon, usamos a mensagem
    # âncora (top_message) como reply_to. No tópico Geral (ID 1), não
    # precisamos de reply_to.
    if selected_id == 1:
        topic_anchor = None
    else:
        topic_anchor = getattr(selected, "top_message", None)
        if not topic_anchor:
            print(f"{Fore.RED}Não foi possível obter a mensagem âncora do tópico selecionado.{Style.RESET_ALL}")
            print("Escolha outro tópico ou verifique se o tópico ainda existe.")
            return select_destination_topic(client)

    print(f"\n{Fore.GREEN}Destino selecionado!{Style.RESET_ALL}")
    print(f"  Fórum: {getattr(destination_entity, 'title', 'Sem nome')}")
    print(f"  Tópico: {selected_title}")
    print(f"  Topic ID: {selected_id}")
    print(f"  Âncora (reply_to): {topic_anchor}")

    # A chave 0 representa o ÚNICO tópico escolhido no destino.
    # Usamos '0 in topic_map' para também suportar o tópico Geral,
    # cujo anchor é None.
    topic_map = {0: topic_anchor}
    return destination_chat, topic_map, selected_title


# ============================================================
# SEÇÃO 3: Compatibilidade de retomada
# ============================================================

def validate_saved_destination(client, destination_chat):
    """Valida se o destino salvo no resume continua sendo um Fórum."""
    try:
        entity = client.get_entity(int(destination_chat))
        if isinstance(entity, Channel) and getattr(entity, "megagroup", False) and getattr(entity, "forum", False):
            return entity.id
    except Exception:
        pass
    return None


# ============================================================
# SEÇÃO 4: Busca de menu (mensagem fixada/pinada)
# ============================================================

def get_menu_from_chat(client, chat_id, chat_info):
    """Busca mensagem fixada ou palavras-chave de menu no chat de origem."""
    try:
        if chat_info["type"] in ("channel", "supergroup", "forum"):
            full_channel = client(GetFullChannelRequest(chat_id))
            pinned_msg_id = getattr(full_channel.full_chat, 'pinned_msg_id', None)
        elif chat_info["type"] == "group":
            try:
                full_chat = client(GetFullChatRequest(chat_id=chat_info["entity"].id))
                pinned_msg_id = getattr(full_chat.full_chat, 'pinned_msg_id', None)
            except Exception:
                pinned_msg_id = None
        else:
            pinned_msg_id = None

        if pinned_msg_id:
            pinned_msg = client.get_messages(chat_id, ids=pinned_msg_id)
            if pinned_msg and (pinned_msg.text or pinned_msg.message):
                return (pinned_msg.text or pinned_msg.message, pinned_msg.id)

        keywords = ["menu", "navegação", "clique aqui", "#", "conteúdo"]
        for message in client.iter_messages(chat_id, limit=10):
            content = (message.text or message.message or "").lower()
            if any(kw in content for kw in keywords):
                if len(content) > 100 or content.count("#") > 3 or content.count("http") > 2:
                    return (message.text or message.message, message.id)
        return (None, None)
    except Exception as e:
        logging.error(f"Erro ao buscar menu: {e}")
        return (None, None)

# ============================================================
# SEÇÃO 5: Verificação de proteção de conteúdo
# ============================================================

def is_content_protected(client, chat_id):
    """Verifica se o chat possui proteção de conteúdo ativada."""
    try:
        info = get_chat_info(client, chat_id)
        return info["is_protected"]
    except Exception as e:
        logging.error(f"Erro ao verificar proteção de conteúdo: {e}")
        return False

# ============================================================
# SEÇÃO 6: Menu de seleção de tipo de conteúdo
# ============================================================

def select_content_type():
    print("\nO que deseja clonar?\n")
    print("1 - Todas as Mensagens")
    print("2 - Apenas Imagens")
    print("3 - Apenas Vídeos")
    print("4 - Apenas Áudios")
    print("5 - Apenas Documentos")
    print("6 - Apenas Texto")
    print("7 - Apenas Stickers")
    print("8 - Tudo (Mensagens, Imagens, Vídeos, Áudios, Stickers, Documentos...)")
    choice = input("\nEscolha uma opção (1-8): ").strip()
    if choice == "1":
        return ["text", "photo", "video", "audio", "document", "sticker"]
    elif choice == "2":
        return ["photo"]
    elif choice == "3":
        return ["video"]
    elif choice == "4":
        return ["audio"]
    elif choice == "5":
        return ["document"]
    elif choice == "6":
        return ["text"]
    elif choice == "7":
        return ["sticker"]
    elif choice == "8":
        return ["text", "photo", "video", "audio", "document", "sticker"]
    else:
        print("Escolha inválida! Tente novamente.")
        return select_content_type()

# ============================================================
# SEÇÃO 7: Função principal de clonagem
# ============================================================


def select_content_type_from_choice(choice):
    if choice in (1, 8):
        return ["text", "photo", "video", "audio", "document", "sticker"]
    return {2:["photo"],3:["video"],4:["audio"],5:["document"],6:["text"],7:["sticker"]}[choice]

class CloneStopped(Exception):
    pass

def run_clone(origin_chat, destination_chat, topic_index, content_choice, resume_decision="no", data_dir="/data", progress_callback=None, stop_event=None):
    print("\nIniciando a clonagem de mensagens...\n")

    data_dir = os.path.abspath(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    os.environ["CLONECAT_RESUME_FILE"] = os.path.join(data_dir, "resume_forum.json")
    os.environ["CLONECAT_TEMP_DIR"] = os.path.join(data_dir, "temp_media")
    global RESUME_FILE, TEMP_DIR
    RESUME_FILE = os.environ["CLONECAT_RESUME_FILE"]
    TEMP_DIR = os.environ["CLONECAT_TEMP_DIR"]
    os.makedirs(TEMP_DIR, exist_ok=True)

    config_path = os.path.join(data_dir, "config.json")
    session_file = os.path.join(data_dir, "session.txt")
    with open(config_path, "r", encoding="utf-8") as f:
        credentials = json.load(f)
    api_id, api_hash = int(credentials["api_id"]), credentials["api_hash"]

    session_str = ""
    if os.path.exists(session_file):
        with open(session_file, encoding="utf-8") as f:
            session_str = f.read().strip()

    client = TelegramClient(StringSession(session_str), api_id, api_hash,
                            connection_retries=20, request_retries=20,
                            auto_reconnect=True, retry_delay=5)
    client.start()
    session_str = client.session.save()
    with open(session_file, "w", encoding="utf-8") as f:
        f.write(session_str)
    print("Conexão estabelecida com sucesso.")

    executor = ThreadPoolExecutor(max_workers=1)
    origin_chat = int(origin_chat)
    destination_chat = int(destination_chat)

    # ================================================================
    # PASSO 1: Detecta tipo do chat, proteção e atributos
    # ================================================================
    chat_info = get_chat_info(client, origin_chat)

    if chat_info["is_protected"]:
        print(f"\n{Fore.YELLOW}Atenção: proteção de conteúdo detectada no chat de origem.")
        print("Mídias serão baixadas localmente e reenviadas. Caso o download falhe, apenas o texto será clonado.{Style.RESET_ALL}")

    # ================================================================
    # PASSO 1.5: Verifica se há retomada (resume) salva
    # ================================================================
    resume_data = load_resume(origin_chat)
    resuming = False
    if resume_data and str(resume_decision).lower() in ("s", "sim", "yes", "resume"):
        resuming = True
        destination_chat = int(resume_data["destination_chat"])
        topic_map = resume_data.get("topic_map", {})
        content_types = resume_data.get("content_types", [])
        print(f"\n{Fore.GREEN}Retomando clonagem...{Style.RESET_ALL}")
    elif resume_data:
        delete_resume(origin_chat)
        resume_data = None
        print("Iniciando nova clonagem.")

    # ================================================================
    # PASSO 2: Escolhe o Fórum de destino e o tópico existente
    # ================================================================
    selected_topic_title = ""
    if not resuming:
        destination_entity = client.get_entity(destination_chat)
        result = client(GetForumTopicsRequest(
            peer=destination_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
        ))
        valid_topics = [t for t in (getattr(result, "topics", []) or [])
                        if getattr(t, "id", None) is not None and t.__class__.__name__ != "ForumTopicDeleted"]
        index = int(topic_index) - 1
        if index < 0 or index >= len(valid_topics):
            raise ValueError(f"Tópico {topic_index} não existe no destino.")
        selected = valid_topics[index]
        selected_id = getattr(selected, "id", 0)
        selected_topic_title = getattr(selected, "title", None) or ("Geral" if selected_id == 1 else f"Tópico {selected_id}")
        topic_anchor = None if selected_id == 1 else getattr(selected, "top_message", None)
        if selected_id != 1 and not topic_anchor:
            raise ValueError("O tópico selecionado não possui mensagem âncora.")
        topic_map = {0: topic_anchor}
        content_types = select_content_type_from_choice(int(content_choice))
        print(f"\n{Fore.GREEN}Destino selecionado: {selected_topic_title}{Style.RESET_ALL}")
    else:
        selected_topic_title = resume_data.get("destination_topic_title", "") if resume_data else ""

    # ================================================================
    # PASSO 5: Obtém histórico de mensagens
    # ================================================================
    print("\nObtendo histórico de mensagens...")
    messages = list(client.iter_messages(origin_chat, limit=None))
    print(f"Total de mensagens encontradas: {len(messages)}")
    total_messages = len(messages)
    if progress_callback:
        progress_callback({"event":"history","total":total_messages,"origin":origin_chat,"destination":destination_chat,"topic":selected_topic_title})

    # Debug RAW: mostra estrutura do reply_to das primeiras msgs com reply
    if chat_info["is_forum"]:
        count = 0
        for m in messages:
            if getattr(m, 'reply_to', None) and count < 10:
                rt = m.reply_to
                tqdm.write(f"  [RAW] msg {m.id} reply_to type={type(rt).__name__} "
                           f"top_id={getattr(rt, 'reply_to_top_id', '??')} "
                           f"msg_id={getattr(rt, 'reply_to_msg_id', '??')}")
                count += 1
        tqdm.write(f"  [RAW] topic_map keys={list(topic_map.keys())[:5]}...")

    # Busca o menu antes de clonar as mensagens
    menu, menu_id = get_menu_from_chat(client, origin_chat, chat_info)

    # ================================================================
    # PASSO 6: Pipeline de clonagem
    # ================================================================

    # Contadores + estado
    if resuming and resume_data:
        cloned_ids = resume_data.get("cloned_msg_ids", set())
        cloned_count = resume_data.get("cloned_count", 0)
        skipped_count = resume_data.get("skipped_count", 0)
        failed_count = resume_data.get("failed_count", 0)
    else:
        cloned_ids = set()
        cloned_count = 0
        skipped_count = 0
        failed_count = 0
    chat_protected = chat_info["is_protected"]
    msg_id_map = {}  # {source_msg_id: dest_msg_id} para preservar reply chain

    ext_map = {"photo": ".jpg", "video": ".mp4", "audio": ".ogg", "document": "", "sticker": ".webp"}

    def _get_msg_text(msg):
        return (getattr(msg, 'text', None) or
                getattr(msg, 'message', None) or
                getattr(msg, 'caption', None) or "")

    def _has_media(msg, ct):
        return (("photo" in ct and msg.photo) or
                ("video" in ct and msg.video) or
                ("audio" in ct and msg.audio) or
                ("document" in ct and msg.document) or
                ("sticker" in ct and msg.sticker))

    def _get_media(msg, ct):
        if "photo" in ct and msg.photo:
            return "photo", msg.photo
        if "video" in ct and msg.video:
            return "video", msg.video
        if "audio" in ct and msg.audio:
            return "audio", msg.audio
        if "document" in ct and msg.document:
            return "document", msg.document
        if "sticker" in ct and msg.sticker:
            return "sticker", msg.sticker
        return None, None

    def _resolve_reply_target(msg, topic_map, msg_id_map):
        """Determina o reply_to correto para preservar tópicos e reply chain."""
        reply_to = None
        reply_info = getattr(msg, 'reply_to', None)

        if reply_info is None:
            return None

        reply_to_top_id = getattr(reply_info, 'reply_to_top_id', None)
        reply_to_msg_id = getattr(reply_info, 'reply_to_msg_id', None)

        # Se a mensagem responde a outra mensagem clonada, usa o ID mapeado
        if reply_to_msg_id and reply_to_msg_id in msg_id_map:
            return msg_id_map[reply_to_msg_id]

        # Se pertence a um tópico, usa o ID do tópico no destino
        if reply_to_top_id and reply_to_top_id in topic_map:
            return topic_map[reply_to_top_id]

        # Se responde a uma mensagem não clonada mas tem tópico, usa o tópico
        if reply_to_msg_id and reply_to_top_id and reply_to_top_id in topic_map:
            return topic_map[reply_to_top_id]

        return None

    # Pre-filtra mensagens nao-servico em ordem cronologica
    ordered = [m for m in reversed(messages) if not isinstance(m, MessageService)]
    skipped_count = total_messages - len(ordered)

    # O destino desta versão possui UM único tópico escolhido pelo usuário.
    # Portanto, independentemente de a origem ser canal, grupo ou fórum,
    # todas as mensagens serão roteadas para esse tópico.
    batches = []
    if 0 in topic_map:
        dest_topic_anchor = topic_map[0]
        batches = [("Destino: " + chat_info["title"], dest_topic_anchor, ordered)]
        tqdm.write(f"  lote único → tópico '{selected_topic_title or "Geral"}' (reply_to={dest_topic_anchor}): {len(ordered)} msgs")
    elif chat_info["is_forum"] and topic_map:
        topic_buckets = {}  # {source_topic_id: [msgs]}
        general_msgs = []
        for m in ordered:
            rt = getattr(m, 'reply_to', None)
            if rt is None:
                general_msgs.append(m)
                continue
            tid = getattr(rt, 'reply_to_top_id', None)
            mid = getattr(rt, 'reply_to_msg_id', None)
            # Alguns foruns usam reply_to_top_id, outros reply_to_msg_id
            # como identificador do topico
            matched = False
            if tid and tid in topic_map:
                topic_buckets.setdefault(tid, []).append(m)
                matched = True
            elif mid and mid in topic_map:
                topic_buckets.setdefault(mid, []).append(m)
                matched = True
            if not matched:
                general_msgs.append(m)

        if general_msgs:
            batches.append(("Geral", None, general_msgs))
            tqdm.write(f"  lote Geral: {len(general_msgs)} msgs (sem reply_to)")
        for tid in topic_map:
            if tid == 1:
                continue
            msgs = topic_buckets.get(tid, [])
            dest_target = topic_map[tid]
            batches.append((f"topic_{tid}", dest_target, msgs))
            tqdm.write(f"  lote topico {tid} → reply_to={dest_target}: {len(msgs)} msgs")
    else:
        batches = [(None, None, ordered)]

    total_msgs_to_process = sum(len(b[2]) for b in batches)
    tqdm.write(f"Total a processar: {total_msgs_to_process} msgs em {len(batches)} lote(s)")

    # Pipeline: future da proxima midia sendo baixada em background
    next_dl_future = None
    next_dl_info = None   # (filepath, media_type, text, msg_id)

    with tqdm(total=total_messages, desc="Clonando", bar_format="{l_bar}{bar} {n_fmt}/{total_fmt}",
              colour="magenta", initial=skipped_count) as progress:

        # ================================================================
        # Helper: salva progresso para retomada (debounced a cada 20 msgs)
        # ================================================================
        _save_counter = [0]

        def _save_resume_progress():
            nonlocal cloned_count, skipped_count, failed_count
            _save_counter[0] += 1
            if _save_counter[0] % 20 == 0:
                resume_info = {
                    "origin_chat": origin_chat,
                    "destination_chat": destination_chat,
                    "chat_type": chat_info["type"],
                    "origin_title": chat_info["title"],
                    "topic_map": topic_map,
                    "cloned_msg_ids": cloned_ids,
                    "total_msgs": total_messages,
                    "skipped_count": skipped_count,
                    "cloned_count": cloned_count,
                    "failed_count": failed_count,
                    "content_types": content_types,
                    "destination_topic_title": selected_topic_title,
                }
                save_resume(resume_info)
                if progress_callback:
                    progress_callback({"event":"progress","total":total_messages,"cloned":cloned_count,"skipped":skipped_count,"failed":failed_count})

        def _save_resume_now():
            """Força save imediato (usado no fim de cada lote)."""
            resume_info = {
                "origin_chat": origin_chat,
                "destination_chat": destination_chat,
                "chat_type": chat_info["type"],
                "origin_title": chat_info["title"],
                "topic_map": topic_map,
                "cloned_msg_ids": cloned_ids,
                "total_msgs": total_messages,
                "skipped_count": skipped_count,
                "cloned_count": cloned_count,
                "failed_count": failed_count,
                "content_types": content_types,
                "destination_topic_title": selected_topic_title,
            }
            save_resume(resume_info)
            if progress_callback:
                progress_callback({"event":"progress","total":total_messages,"cloned":cloned_count,"skipped":skipped_count,"failed":failed_count})

        # ================================================================
        # Funções internas do pipeline
        # ================================================================

        def _send_text(msg_id, text, reply_to=None):
            nonlocal cloned_count
            kwargs = {}
            if reply_to is not None:
                kwargs["reply_to"] = reply_to
            sent = client.send_message(destination_chat, text, **kwargs)
            cloned_ids.add(msg_id)
            cloned_count += 1
            _save_resume_progress()
            return sent.id if sent else None

        def _ffmpeg_to_streaming(input_path, output_path, msg_id, duration=None):
            if not os.path.exists(input_path):
                return False

            if not shutil.which("ffmpeg"):
                tqdm.write(f"  FFmpeg nao encontrado, enviando video original msg {msg_id}")
                return False

            tqdm.write(f"  Processando video msg {msg_id} (ffmpeg)...")

            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0",
                "-pix_fmt", "yuv420p",
                "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease",
                "-r", "30",
                "-crf", "23", "-preset", "fast",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2",
                "-movflags", "+faststart",
                output_path
            ]

            proc = None
            try:
                proc = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                                        universal_newlines=True, bufsize=1)
            except FileNotFoundError:
                tqdm.write(f"  FFmpeg nao encontrado, enviando original msg {msg_id}")
                return False
            time_pattern = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")

            total_sec = None
            if duration is not None:
                total_sec = duration

            for line in proc.stderr:
                match = time_pattern.search(line)
                if match:
                    h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
                    current_sec = h * 3600 + m * 60 + s
                    if total_sec is None:
                        total_sec = current_sec
                    pct = (current_sec / total_sec * 100) if total_sec > 0 else 0
                    progress.set_description(
                        f"Msg {msg_id} ffmpeg {pct:.0f}%"
                    )

            proc.wait()
            if proc.returncode != 0:
                tqdm.write(f"  FFmpeg falhou na msg {msg_id}, enviando original")
                return False
            return True

        def _download_sync(client_instance, media_obj, filepath, msg_id, media_type):
            dl_start = time.time()

            def dl_cb(current, total):
                elapsed = time.time() - dl_start or 0.001
                speed = current / elapsed
                pct = current / total * 100 if total else 0
                progress.set_description(
                    f"Msg {msg_id} {pct:.0f}% ({fmt_size(current)}/{fmt_size(total)})"
                    f" {fmt_size(speed)}/s"
                )

            client_instance.download_media(media_obj, file=filepath,
                                           progress_callback=dl_cb)

        def _upload(path, media_type, text, msg_id, reply_to=None):
            nonlocal cloned_count
            if not os.path.exists(path):
                return None
            file_sz = fmt_size(os.path.getsize(path))
            tqdm.write(f"  Enviando msg {msg_id} ({file_sz})...")

            up_start = time.time()

            def up_cb(current, total):
                elapsed = time.time() - up_start or 0.001
                speed = current / elapsed
                pct = current / total * 100 if total else 0
                progress.set_description(
                    f"Msg {msg_id} {pct:.0f}% ({fmt_size(current)}/{fmt_size(total)})"
                    f" {fmt_size(speed)}/s"
                )

            kwargs = {"caption": text, "progress_callback": up_cb} if text else {"progress_callback": up_cb}
            if media_type == "video":
                kwargs["supports_streaming"] = True
            if reply_to is not None:
                kwargs["reply_to"] = reply_to
            sent = client.send_file(destination_chat, path, **kwargs)
            cloned_ids.add(msg_id)
            cloned_count += 1
            _save_resume_progress()
            tqdm.write(f"  Msg {msg_id} enviada ({file_sz})")
            return sent.id if sent else None

        def _send_anonymous(msg, reply_to=None):
            """Envia mensagem anonimamente (sem forward) reaproveitando referência da mídia."""
            kwargs = {}
            if reply_to is not None:
                kwargs["reply_to"] = reply_to
            text_content = _get_msg_text(msg)

            try:
                if msg.photo and "photo" in content_types:
                    kwargs["caption"] = text_content
                    sent = client.send_file(destination_chat, msg.photo, **kwargs)
                elif msg.document and _has_media(msg, content_types):
                    kwargs["caption"] = text_content
                    if msg.video:
                        kwargs["supports_streaming"] = True
                    sent = client.send_file(destination_chat, msg.document, **kwargs)
                elif text_content:
                    sent = client.send_message(destination_chat, text_content, **kwargs)
                else:
                    return None

                if sent:
                    nonlocal cloned_count
                    cloned_ids.add(msg.id)
                    cloned_count += 1
                    _save_resume_progress()
                    return sent.id
            except RPCError as e:
                err = str(e).lower()
                if "protected" in err or "restricted" in err or "noforwards" in err:
                    nonlocal chat_protected
                    chat_protected = True
                    tqdm.write(f"  Msg {msg.id}: proteção detectada no envio, baixando midia localmente")
                else:
                    raise
            except FloodWaitError:
                raise
            except Exception:
                raise
            return None

        # ================================================================
        # LOOP PRINCIPAL: processa por lotes (topicos)
        # ================================================================

        try:
            for batch_name, batch_reply_to, batch_msgs in batches:
                if batch_name:
                    tqdm.write(f"\n--- Lote: {batch_name} (reply_to={batch_reply_to}) ---")

                for i, message in enumerate(batch_msgs):
                    if stop_event is not None and stop_event.is_set():
                        _save_resume_now()
                        raise CloneStopped("Clonagem interrompida pelo usuário.")
                    msg_id = message.id

                    # Pula mensagem ja clonada (retomada)
                    if msg_id in cloned_ids:
                        progress.update(1)
                        continue

                    temp_filepath = None
                    ffmpeg_out = None
                    text_content = _get_msg_text(message)

                    # Determina reply_to: se for forum, usa o reply_to do lote (topico)
                    # mas preserva reply chain se msg responde a outra ja clonada
                    if batch_reply_to is not None:
                        reply_to = batch_reply_to
                        rt = getattr(message, 'reply_to', None)
                        reply_msg_id = getattr(rt, 'reply_to_msg_id', None) if rt else None
                        if reply_msg_id and reply_msg_id in msg_id_map:
                            reply_to = msg_id_map[reply_msg_id]
                            tqdm.write(f"  [reply] msg {msg_id} responde a {reply_msg_id} → reply_to={reply_to}")
                    else:
                        reply_to = None

                    try:
                        media_type, media_obj = None, None
                        if "photo" in content_types and message.photo:
                            media_type, media_obj = "photo", message.photo
                        elif "video" in content_types and message.video:
                            media_type, media_obj = "video", message.video
                        elif "audio" in content_types and message.audio:
                            media_type, media_obj = "audio", message.audio
                        elif "document" in content_types and message.document:
                            media_type, media_obj = "document", message.document
                        elif "sticker" in content_types and message.sticker:
                            media_type, media_obj = "sticker", message.sticker

                        # ----------------------------------------------------------
                        # ESTRATÉGIA A: Chat NÃO protegido → envio anônimo (rápido)
                        # ----------------------------------------------------------
                        if not chat_protected:
                            sent_id = None
                            try:
                                sent_id = _send_anonymous(message, reply_to)
                            except FloodWaitError as e:
                                tqdm.write(f"  FloodWait {e.seconds}s msg {msg_id}")
                                time.sleep(e.seconds)
                                try:
                                    sent_id = _send_anonymous(message, reply_to)
                                except Exception:
                                    failed_count += 1
                                    progress.update(1)
                                    continue
                            except RPCError as e:
                                tqdm.write(f"  Erro RPC msg {msg_id}: {e}")
                                logging.error(f"Erro RPC msg {msg_id}: {e}")
                                failed_count += 1
                                progress.update(1)
                                continue
                            except Exception as e:
                                tqdm.write(f"  Erro msg {msg_id}: {e}")
                                logging.error(f"Erro inesperado msg {msg_id}: {e}")
                                failed_count += 1
                                progress.update(1)
                                continue

                            if sent_id is not None:
                                msg_id_map[msg_id] = sent_id
                                if media_obj:
                                    time.sleep(MEDIA_DELAY)
                                else:
                                    time.sleep(TEXT_DELAY)
                            else:
                                skipped_count += 1
                            progress.update(1)
                            continue

                        # ----------------------------------------------------------
                        # ESTRATÉGIA B: Chat protegido → download + reupload
                        # ----------------------------------------------------------

                        if media_obj is not None:
                            ext = ext_map.get(media_type, "")
                            filename = f"{origin_chat}_{msg_id}{ext}"
                            temp_filepath = os.path.join(TEMP_DIR, filename)

                            tqdm.write(f"  Baixando msg {msg_id} ({media_type})...")

                            try:
                                _download_sync(client, media_obj, temp_filepath, msg_id, media_type)
                            except FloodWaitError as e:
                                tqdm.write(f"  FloodWait {e.seconds}s download msg {msg_id}")
                                time.sleep(e.seconds)
                                try:
                                    _download_sync(client, media_obj, temp_filepath, msg_id, media_type)
                                except Exception:
                                    failed_count += 1
                                    if text_content:
                                        tqdm.write(f"  Falha download, enviando texto msg {msg_id}")
                                        sent_id = _send_text(msg_id, text_content, reply_to)
                                        if sent_id:
                                            msg_id_map[msg_id] = sent_id
                                    else:
                                        skipped_count += 1
                                    progress.update(1)
                                    if temp_filepath and os.path.exists(temp_filepath):
                                        os.remove(temp_filepath)
                                    continue
                            except Exception as dl_err:
                                err = str(dl_err).lower()
                                if "protected" in err or "restricted" in err or "noforwards" in err:
                                    chat_protected = True
                                    tqdm.write(f"  Download msg {msg_id} bloqueado (protegido)")
                                else:
                                    tqdm.write(f"  Erro download msg {msg_id}: {dl_err}")
                                    logging.error(f"Erro download msg {msg_id}: {dl_err}")

                                if media_type == "sticker":
                                    skipped_count += 1
                                elif text_content:
                                    sent_id = _send_text(msg_id, text_content, reply_to)
                                    if sent_id:
                                        msg_id_map[msg_id] = sent_id
                                    tqdm.write(f"  Texto msg {msg_id} enviado (fallback)")
                                else:
                                    skipped_count += 1
                                progress.update(1)
                                if temp_filepath and os.path.exists(temp_filepath):
                                    os.remove(temp_filepath)
                                continue

                            # -- FFMPEG: otimiza video para streaming --
                            upload_path = temp_filepath
                            ffmpeg_out = None
                            if media_type == "video" and os.path.exists(temp_filepath):
                                ffmpeg_out = temp_filepath + ".stream.mp4"
                                if _ffmpeg_to_streaming(temp_filepath, ffmpeg_out, msg_id):
                                    upload_path = ffmpeg_out
                                else:
                                    if os.path.exists(ffmpeg_out):
                                        os.remove(ffmpeg_out)
                                    ffmpeg_out = None

                            # -- PIPELINE: sobe a midia atual enquanto baixa a proxima em background --
                            next_media_idx = None
                            for j in range(i + 1, len(batch_msgs)):
                                nm = batch_msgs[j]
                                if _has_media(nm, content_types):
                                    next_media_idx = j
                                    break

                            if next_media_idx is not None:
                                nm = batch_msgs[next_media_idx]
                                nm_text = _get_msg_text(nm)
                                nm_mt, nm_mo = _get_media(nm, content_types)
                                nm_ext = ext_map.get(nm_mt, "")
                                nm_fp = os.path.join(TEMP_DIR, f"{origin_chat}_{nm.id}{nm_ext}")

                                tqdm.write(f"  [bg] Baixando proxima msg {nm.id} ({nm_mt})...")

                                def _bg_download(mo=nm_mo, fp=nm_fp, mid=nm.id, mt=nm_mt,
                                                 ss=session_str, aid=api_id, ah=api_hash):
                                    import asyncio
                                    loop = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loop)
                                    bg = TelegramClient(StringSession(ss), aid, ah)
                                    bg.connect()
                                    try:
                                        _download_sync(bg, mo, fp, mid, mt)
                                    finally:
                                        bg.disconnect()
                                        loop.close()

                                next_dl_future = executor.submit(_bg_download)
                                next_dl_info = (nm_fp, nm_mt, nm_text, nm.id)

                            sent_id = _upload(upload_path, media_type, text_content, msg_id, reply_to)
                            if sent_id:
                                msg_id_map[msg_id] = sent_id
                                os.remove(upload_path)
                                time.sleep(MEDIA_DELAY)
                            if ffmpeg_out and os.path.exists(ffmpeg_out):
                                os.remove(ffmpeg_out)
                            progress.update(1)

                            # Aguarda o download em background da proxima midia
                            if next_dl_future is not None:
                                try:
                                    next_dl_future.result()
                                except Exception as bg_err:
                                    tqdm.write(f"  Erro download bg: {bg_err}")
                                    if next_dl_info:
                                        fp = next_dl_info[0]
                                        if fp and os.path.exists(fp):
                                            os.remove(fp)
                                finally:
                                    next_dl_future = None
                                    next_dl_info = None

                        elif text_content:
                            sent_id = _send_text(msg_id, text_content, reply_to)
                            if sent_id:
                                msg_id_map[msg_id] = sent_id
                            progress.update(1)
                            time.sleep(TEXT_DELAY)
                        else:
                            skipped_count += 1
                            progress.update(1)

                    except FloodWaitError as e:
                        tqdm.write(f"  FloodWait {e.seconds}s, aguardando...")
                        time.sleep(e.seconds)
                        failed_count += 1
                        progress.update(1)
                    except RPCError as e:
                        err = str(e).lower()
                        if "protected" in err or "restricted" in err or "noforwards" in err:
                            chat_protected = True
                            if text_content:
                                try:
                                    sent_id = _send_text(msg_id, text_content, reply_to)
                                    if sent_id:
                                        msg_id_map[msg_id] = sent_id
                                    tqdm.write(f"  Msg {msg_id}: midia protegida, texto enviado")
                                except Exception:
                                    failed_count += 1
                            else:
                                skipped_count += 1
                        else:
                            tqdm.write(f"  Erro RPC msg {msg_id}: {e}")
                            logging.error(f"Erro RPC msg {msg_id}: {e}")
                            failed_count += 1
                        progress.update(1)
                    except Exception as e:
                        tqdm.write(f"  Erro msg {msg_id}: {e}")
                        logging.error(f"Erro inesperado msg {msg_id}: {e}")
                        failed_count += 1
                        progress.update(1)
                    finally:
                        if temp_filepath and os.path.exists(temp_filepath):
                            os.remove(temp_filepath)
                        if ffmpeg_out and os.path.exists(ffmpeg_out):
                            os.remove(ffmpeg_out)

        except KeyboardInterrupt:
            tqdm.write(f"\n{Fore.YELLOW}Interrompido pelo usuario.{Style.RESET_ALL}")
            save_resume({
                "origin_chat": origin_chat,
                "destination_chat": destination_chat,
                "chat_type": chat_info["type"],
                "origin_title": chat_info["title"],
                "topic_map": topic_map,
                "cloned_msg_ids": cloned_ids,
                "total_msgs": total_messages,
                "skipped_count": skipped_count,
                "cloned_count": cloned_count,
                "failed_count": failed_count,
                "content_types": content_types,
                "destination_topic_title": selected_topic_title,
            })
            print(f"\n{Fore.CYAN}Progresso salvo. Para retomar, execute novamente.{Style.RESET_ALL}")
            print(f"  Mensagens clonadas: {cloned_count}")
            print(f"  Falhas: {failed_count}")
            client.disconnect()
            executor.shutdown(wait=False)
            return

    # ================================================================
    # PASSO 7: Resumo final e menu
    # ================================================================

    # Salva estado final antes do menu
    save_resume({
        "origin_chat": origin_chat,
        "destination_chat": destination_chat,
        "chat_type": chat_info["type"],
        "origin_title": chat_info["title"],
        "topic_map": topic_map,
        "cloned_msg_ids": cloned_ids,
        "total_msgs": total_messages,
        "skipped_count": skipped_count,
        "cloned_count": cloned_count,
        "failed_count": failed_count,
        "content_types": content_types,
        "destination_topic_title": selected_topic_title,
    })

    if chat_protected:
        print(f"\n{Fore.YELLOW}Atenção: o chat de origem possui proteção de conteúdo ativada.")
        print("Apenas textos e legendas foram clonados. Mídias não puderam ser baixadas.{Style.RESET_ALL}")

    if menu and (menu_id not in cloned_ids):
        print("\nAdicionando menu ao final do tópico clonado...")
        menu_kwargs = {}
        if topic_map.get(0):
            menu_kwargs["reply_to"] = topic_map[0]
        client.send_message(destination_chat, menu, **menu_kwargs)
        cloned_ids.add(menu_id)
        cloned_count += 1
        print("Menu adicionado com sucesso!")
    elif menu:
        print("Menu já estava entre as mensagens clonadas, não foi adicionado novamente.")
    else:
        print("Nenhum menu encontrado para adicionar ao chat clonado.")

    # Resumo final
    print(f"\n{Fore.GREEN}Clonagem concluída!{Style.RESET_ALL}\n")
    print(f"  Mensagens clonadas: {cloned_count}")
    print(f"  Mensagens puladas (serviço/sem conteúdo): {skipped_count}")
    if failed_count > 0:
        print(f"  {Fore.RED}Mensagens com falha: {failed_count}{Style.RESET_ALL}")
    if msg_id_map:
        print(f"  Replies preservados: {len(msg_id_map)}")
    print(f"\nID do Destino: {destination_chat}")
    try:
        dest_entity_final = client.get_entity(destination_chat)
        dest_title_final = getattr(dest_entity_final, "title", str(destination_chat))
    except Exception:
        dest_title_final = str(destination_chat)
    print(f"Nome do destino: {dest_title_final}")
    print(f"Tópico: {selected_topic_title or 'Geral'}")
    print(f"Origem: {chat_info['title']} ({chat_info['type_label']})")

    # Clone completo — mantém resume para futuras execuções incrementais
    print(f"\n{Fore.CYAN}Estado salvo. Execute novamente para clonar apenas novas mensagens.{Style.RESET_ALL}")

    if progress_callback:
        progress_callback({"event":"done","total":total_messages,"cloned":cloned_count,"skipped":skipped_count,"failed":failed_count})

    # Cleanup
    client.disconnect()
    executor.shutdown(wait=False)

# Inicia o script
if __name__ == "__main__":
    raise SystemExit("Este arquivo é o motor do agente; execute clonecat_agent.py")
