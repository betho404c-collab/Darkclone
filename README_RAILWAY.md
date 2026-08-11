# CloneCat Agent — Railway

Este projeto coloca uma interface de controle em um bot Telegram e mantém o motor de clonagem do `clonecat_forum_selecionar_topico.py` separado em um subprocesso.

## Arquitetura

- `clonecat_agent.py`: bot de controle.
- `clonecat_forum_selecionar_topico.py`: motor de clonagem original.
- `session.txt`, `config.json`, `resume_forum.json` e logs: ficam no volume montado em `/data`.
- FFmpeg: instalado pela imagem Docker.

O bot usa `python-telegram-bot` 22.8 e o motor usa Telethon 1.44.0.

## Variáveis do Railway

Configure no serviço:

- `BOT_TOKEN` = token do BotFather
- `ADMIN_USER_ID` = seu ID numérico do Telegram
- `API_ID` = API ID do Telegram
- `API_HASH` = API Hash do Telegram
- `DATA_DIR` = `/data`
- `SCRIPT_PATH` = `/app/clonecat_forum_selecionar_topico.py`

## Volume

Crie um Volume Railway e monte em:

`/data`

Isso preserva sessão e resume entre reinícios do container.

## Fluxo no bot

1. `/start`
2. `/connect` e login da conta que fará a operação.
3. `Nova clonagem`.
4. Envie ID da origem.
5. Envie ID do fórum de destino.
6. Escolha o tópico pelos botões.
7. Escolha o tipo de conteúdo.
8. Confirme.
9. O agente inicia o motor original e acompanha o `resume_forum.json`.

Se o processo do motor morrer inesperadamente, o agente tenta reiniciar até 5 vezes usando o resume salvo. Se o próprio serviço Railway reiniciar, o Volume mantém a sessão e o resume.

## Segurança

Não publique `BOT_TOKEN`, `API_HASH`, `session.txt` ou o conteúdo de `resume_forum.json` em um repositório público.

O bot deve ser usado apenas por `ADMIN_USER_ID`.
