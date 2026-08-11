# CloneCat Bot V3

Esta versão usa o motor de clonagem diretamente no mesmo processo Python do bot.
Não executa `clonecat_forum_selecionar_topico.py` por subprocess.

## Variáveis Railway
- BOT_TOKEN
- ADMIN_USER_ID
- API_ID
- API_HASH

## Volume
Monte um Volume em `/data`.
A sessão, resume, config e mídia temporária ficam em `/data`.

## Verificação
No log do Railway deve aparecer:
`CLONECAT BOT V3 - MOTOR DIRETO (SEM SUBPROCESS DO CLONECAT)`

Se aparecer qualquer mensagem `O processo terminou com código 1`, o Railway ainda está executando uma versão antiga do projeto.
