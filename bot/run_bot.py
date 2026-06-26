import logging
import os
import sys
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
    handle_picks_callback,
    newbet,
    picks,
    resultado,
    start,
)
from services.database import initialize_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN no está definido en el entorno")

    initialize_db()

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("apuesta", apuesta))
    app.add_handler(CommandHandler("resultado", resultado))
    app.add_handler(CommandHandler("newbet", newbet))
    app.add_handler(CommandHandler("picks", picks))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CallbackQueryHandler(handle_picks_callback, pattern="^(?:picks_|match_|prematch_|live_)"))
    app.add_handler(CallbackQueryHandler(handle_bet_callback, pattern="^(?:bet_match_|res_|resw_|resl_)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
