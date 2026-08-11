import os, traceback
from clonecat_bot import create_client

print("== DIAGNÓSTICO TELEGRAM ==")
print("TELEGRAM_API_ID:", "OK" if (os.getenv("TELEGRAM_API_ID") or os.getenv("API_ID")) else "AUSENTE")
print("TELEGRAM_API_HASH:", "OK" if (os.getenv("TELEGRAM_API_HASH") or os.getenv("API_HASH")) else "AUSENTE")
try:
    c = create_client()
    print("create_client(): OK")
    c.connect()
    print("connect(): OK")
    print("is_user_authorized():", c.is_user_authorized())
    c.disconnect()
except Exception as e:
    print(type(e).__name__, ":", e)
    traceback.print_exc()
    raise
