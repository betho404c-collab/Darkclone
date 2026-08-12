# CloneCat V4 — correção do erro `connect was never awaited`

Esta versão corrige o erro observado no Railway:

`RuntimeWarning: coroutine 'TelegramBaseClient.connect' was never awaited`

## Correções

- `connect()` e `disconnect()` passam por um adaptador que aguarda a coroutine quando o Telethon devolve uma coroutine.
- A ponte `_run_engine_sync()` chama diretamente `clone_selected()`.
- O motor valida a sessão, origem e destino antes de iniciar a clonagem.
- O histórico continua sendo obtido **antes** da consulta do resume.
- Erros da clonagem são enviados ao bot e também aparecem nos logs do Railway com traceback.
- Compatibilidade com `API_ID/API_HASH/ADMIN_USER_ID` e com `TELEGRAM_API_ID/TELEGRAM_API_HASH/ADMIN_IDS`.

## Variáveis

Pode continuar usando as variáveis da V4:

```text
BOT_TOKEN
API_ID
API_HASH
ADMIN_USER_ID
```

ou:

```text
BOT_TOKEN
TELEGRAM_API_ID
TELEGRAM_API_HASH
ADMIN_IDS
```

O código aceita ambas.

## Volume

Monte o Volume Railway em `/data` para preservar:

- `/data/session.txt`
- `/data/resume_forum.json`
- `/data/temp_media`

## Importante

Não apague o `resume_forum.json` atual. Ele continua válido para a retomada.
