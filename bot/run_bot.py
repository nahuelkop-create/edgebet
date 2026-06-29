import logging
import os
import sys
import threading
from dotenv import load_dotenv

from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.error import Conflict

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT_DIR)

load_dotenv()
from bot.handlers import (
    apuesta,
    balance,
    handle_bet_callback,
    handle_message,
    handle_photo,
    handle_picks_callback_v2,
    newbet,
    picks,
    resultado,
    start,
)
from services.database import initialize_db
from services.notifications import start_schedulers
from web.app import app as web_app
from db.migrations import initialize_postgres

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def _run_web_server():
    port = int(os.getenv("PORT", "10000"))
    # threaded=True so concurrent health checks don't block each other; the
    # reloader is disabled because we're not in the main thread.
    web_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


async def _log_handler_error(update, context):
    logging.exception("Excepción no controlada en handler de Telegram. update=%s", update, exc_info=context.error)
    message = getattr(update, "effective_message", None) if update else None
    if message:
        try:
            await message.reply_text("⚠️ Ocurrió un error procesando el comando. Revisá los logs.")
        except Exception:
            logging.exception("No se pudo enviar mensaje de error al usuario.")


def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN no está definido en el entorno")

    initialize_db()
    try:
        initialize_postgres()
    except Exception:
        logging.exception("No se pudo inicializar PostgreSQL; el bot sigue usando SQLite.")

    # Start the dashboard web server in a daemon thread while the Telegram bot
    # polls in the main thread.
    web_thread = threading.Thread(target=_run_web_server, daemon=True)
    web_thread.start()
    print(f"Dashboard web escuchando en el puerto {os.getenv('PORT', '10000')}.")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("apuesta", apuesta))
    app.add_handler(CommandHandler("resultado", resultado))
    app.add_handler(CommandHandler("newbet", newbet))
    app.add_handler(CommandHandler("picks", picks))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CallbackQueryHandler(handle_picks_callback_v2, pattern="^(?:picks_|league_|match_|prematch_|live_)"))
    app.add_handler(CallbackQueryHandler(handle_bet_callback, pattern="^(?:bet_match_|res_|resw_|resl_)"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(_log_handler_error)

    # Automatic notifications and platform collectors run in daemon threads.
    start_schedulers()
    print("Schedulers iniciados (threading): notificaciones + collectors en background.")

    print("Bot de Telegram iniciado. Presiona Ctrl+C para detener.")
    try:
        app.run_polling(drop_pending_updates=True)
    except Conflict:
        logging.error(
            "No se puede iniciar el bot: ya hay otra instancia escuchando getUpdates con este token. "
            "Detén la otra instancia o usa otro token."
        )
        raise


if __name__ == "__main__":
    main()
