# CloneCat Bot V4

Fluxo: origem -> destino -> tópico -> conteúdo -> histórico completo -> total -> resume -> clonagem.

Variables: BOT_TOKEN, ADMIN_USER_ID, API_ID, API_HASH.
Railway Volume: /data.
O resume não é consultado pelo bot antes do histórico. O motor primeiro executa iter_messages(limit=None), mostra o total e só depois carrega o resume existente.
