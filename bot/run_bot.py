import logging
import os
import sys
import threading
from dotenv import load_dotenv

from flask import Flask, jsonify
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
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
from services.notifications import (
    check_prematch_notifications,
    check_result_notifications,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


# Minimal web server so hosting platforms (e.g. Render) detect an open HTTP port
# and don't kill the process for "timing out" waiting for one. The Telegram bot
# itself runs in the main thread; this just answers health checks.
health_app = Flask(__name__)


@health_app.get("/")
def health():
    return jsonify({"status": "ok"})


def _run_web_server():
    port = int(os.getenv("PORT", "10000"))
    # threaded=True so concurrent health checks don't block each other; the
    # reloader is disabled because we're not in the main thread.
    health_app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


async def _prematch_job(context: ContextTypes.DEFAULT_TYPE):
    """Hourly: notify users about fixtures starting within the next 2 hours."""
    try:
        sent = await check_prematch_notifications(context.bot)
        if sent:
            logging.info("Notificaciones pre-partido enviadas: %s", sent)
    except Exception:
        logging.exception("Error en el job de notificaciones pre-partido")


async def _results_job(context: ContextTypes.DEFAULT_TYPE):
    """Every 10 min: resolve finished bets and notify win/loss + balance."""
    try:
        resolved = await check_result_notifications(context.bot)
        if resolved:
            logging.info("Apuestas resueltas automáticamente: %s", resolved)
    except Exception:
        logging.exception("Error en el job de notificaciones de resultado")


def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN no está definido en el entorno")

    initialize_db()

    # Start the health-check web server in a daemon thread so the process keeps
    # an HTTP port open while the Telegram bot polls in the main thread.
    web_thread = threading.Thread(target=_run_web_server, daemon=True)
    web_thread.start()
    print(f"Servidor web de salud escuchando en el puerto {os.getenv('PORT', '10000')}.")

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

    # Automatic notifications: pre-match analysis (hourly) + bet results (10 min).
    job_queue = app.job_queue
    if job_queue is not None:
        job_queue.run_repeating(_prematch_job, interval=3600, first=30)
        job_queue.run_repeating(_results_job, interval=600, first=60)
        print("Jobs de notificaciones programados (pre-partido cada 1h, resultados cada 10min).")
    else:
        logging.warning(
            "JobQueue no disponible: instalá 'python-telegram-bot[job-queue]' para las "
            "notificaciones automáticas."
        )

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
