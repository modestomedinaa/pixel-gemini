"""
Telegram Bot entry point for the Pixel 10 Pro Google One Gemini Bot.

Commands:
  /start        – Show welcome message and available commands
  /login        – Begin credential capture flow (email → password)
  /check_offer  – Run Google One automation and look for Gemini Pro offer
  /get_link     – Show the last captured offer link
  /status       – Show current session status and device profile
"""

import asyncio
import logging
import os
import sys
import requests as _requests

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ApplicationHandlerStop,
)

import config
from device_simulator import create_device_profile
from google_automation import check_gemini_offer, GoogleAutomationError

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)


def _reset_webhook(token: str) -> None:
    """Delete any webhook and drop pending updates before polling starts."""
    url = f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=true"
    try:
        r = _requests.get(url, timeout=10)
        logger.info("deleteWebhook: %s", r.json())
    except Exception as e:
        logger.warning("Could not reset webhook: %s", e)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle telegram errors — especially 409 Conflict."""
    from telegram.error import Conflict, NetworkError
    err = context.error
    if isinstance(err, Conflict):
        logger.warning("409 Conflict detected — waiting 15s for other instance to die...")
        await asyncio.sleep(15)
    elif isinstance(err, NetworkError):
        logger.warning("Network error: %s — retrying...", err)
    else:
        logger.exception("Unhandled error: %s", err, exc_info=err)

# ── Conversation states ───────────────────────────────────────────────────────
AWAIT_EMAIL, AWAIT_PASSWORD, AWAIT_TOTP = range(3)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_session(chat_id: int) -> dict:
    """Return (creating if absent) the session dict for *chat_id*."""
    if chat_id not in config.SESSION_STORE:
        config.SESSION_STORE[chat_id] = {}
    return config.SESSION_STORE[chat_id]


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message with command menu."""
    await update.message.reply_text(
        "🤖 *Pixel 10 Pro Google One Bot*\n\n"
        "This bot simulates a Google Pixel 10 Pro (Android 16) device, "
        "logs into your Google account, and retrieves the *12-month free "
        "Gemini Pro* offer link from Google One.\n\n"
        "📋 *Available Commands:*\n"
        "• /login – Enter your Gmail credentials\n"
        "• /check\\_offer – Detect the Gemini Pro offer\n"
        "• /get\\_link – Show the last captured offer link\n"
        "• /status – View current session & device info\n\n"
        "⚠️ *Privacy Note:* Credentials are held in memory only for the "
        "duration of the session and never stored persistently.",
        parse_mode="Markdown",
    )


# ── /login conversation ───────────────────────────────────────────────────────

async def login_start(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin the login conversation – ask for email."""
    await update.message.reply_text(
        "📧 Please enter your Gmail address:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return AWAIT_EMAIL


async def login_email(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the email and ask for password."""
    email = update.message.text.strip()
    context.user_data["pending_email"] = email
    await update.message.reply_text(
        f"✅ Email received: `{email}`\n\n🔒 Now enter your password:",
        parse_mode="Markdown",
    )
    return AWAIT_PASSWORD


async def login_password(update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store password, then ask for TOTP key (optional)."""
    password = update.message.text.strip()
    context.user_data["pending_password"] = password

    # Delete the message containing the password for security
    try:
        await update.message.delete()
    except Exception:
        pass

    await update.message.reply_text(
        "🔐 Password saved.\n\n"
        "Now enter your *TOTP key* (from Google Authenticator setup).\n"
        "This is the 32-character secret key.\n\n"
        "_Type `skip` if you don't have 2FA enabled._",
        parse_mode="Markdown",
    )
    return AWAIT_TOTP


async def login_totp(update: Update,
                     context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store TOTP key, create device profile, and finish."""
    chat_id = update.effective_chat.id
    totp_input = update.message.text.strip()
    email = context.user_data.pop("pending_email", "")
    password = context.user_data.pop("pending_password", "")

    session = _get_session(chat_id)
    session["email"] = email
    session["password"] = password
    session["device"] = create_device_profile()
    session["offer_link"] = None

    if totp_input.lower() == "skip":
        session["totp_key"] = ""
        totp_msg = "❌ No 2FA"
    else:
        # Clean up: remove spaces, take only valid base32 chars
        clean = "".join(c.upper() for c in totp_input if c.isalnum())
        session["totp_key"] = clean
        totp_msg = "✅ 2FA enabled"

    # Delete TOTP message for security
    try:
        await update.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ *Credentials saved* and a new Pixel 10 Pro device profile has "
            "been created for this session.\n\n"
            + totp_msg + "\n\n"
            + session["device"].summary()
            + "\n\nUse /check\\_offer to search for the Gemini Pro offer."
        ),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def login_cancel(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the login conversation."""
    context.user_data.pop("pending_email", None)
    await update.message.reply_text(
        "❌ Login cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── /check_offer ──────────────────────────────────────────────────────────────

async def check_offer(update: Update,
                      context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run Google One automation and report the result."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session.get("email") or not session.get("password"):
        await update.message.reply_text(
            "⚠️ No credentials found. Please use /login first."
        )
        return

    device = session.get("device")
    if not device:
        device = create_device_profile()
        session["device"] = device

    await update.message.reply_text(
        "⏳ Launching Pixel 10 Pro emulator…\n"
        "Logging into Google + searching for Gemini Pro offer.\n"
        "_This may take up to 90 seconds._"
    )

    try:
        offer_link = await asyncio.to_thread(
            check_gemini_offer,
            session["email"],
            session["password"],
            device,
            session.get("totp_key", ""),
            chat_id,
        )
    except Exception as exc:
        import os
        screenshot_path = "debug_login_error.png"
        if os.path.exists(screenshot_path):
            try:
                with open(screenshot_path, "rb") as f:
                    await update.message.reply_photo(
                        photo=f,
                        caption=f"❌ *Error:* {exc}\nHere is a screenshot of the login screen to help you debug.",
                        parse_mode="Markdown"
                    )
                os.remove(screenshot_path)
            except Exception as se:
                logger.warning("Failed to send screenshot: %s", se)
                await update.message.reply_text(f"❌ *Error:* {exc}", parse_mode="Markdown")
        else:
            if isinstance(exc, GoogleAutomationError):
                await update.message.reply_text(f"❌ *Error:* {exc}", parse_mode="Markdown")
            else:
                logger.exception("Unexpected error in check_offer for chat %s", chat_id)
                import traceback
                tb = traceback.format_exc()
                await update.message.reply_text(
                    f"❌ Error: {exc}\n\n```\n{tb[-500:]}\n```",
                    parse_mode="Markdown"
                )
        return

    if offer_link:
        session["offer_link"] = offer_link
        await update.message.reply_text(
            "🎉 Gemini Pro Offer Found!\n\n"
            "Click the link below to activate your 12-month free Gemini Pro:\n\n"
            f"{offer_link}\n\n"
            "Use /get_link to retrieve this link again."
        )
    else:
        await update.message.reply_text(
            "😔 No active Gemini Pro offer was detected on your Google One "
            "account at this time.\n\n"
            "The offer may not be available for your account region or may "
            "have already been activated. Try again later."
        )


# ── /get_link ─────────────────────────────────────────────────────────────────

async def get_link(update: Update,
                   context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the last captured offer link for this session."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    link = session.get("offer_link")

    if link:
        await update.message.reply_text(
            f"🔗 *Last captured offer link:*\n\n{link}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "ℹ️ No offer link has been captured yet. "
            "Use /check\\_offer to search for the Gemini Pro offer.",
            parse_mode="Markdown",
        )


# ── /status ───────────────────────────────────────────────────────────────────

async def status(update: Update,
                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current session and device profile summary."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if not session:
        await update.message.reply_text(
            "ℹ️ No active session. Use /login to get started."
        )
        return

    email = session.get("email", "—")
    has_creds = bool(session.get("email") and session.get("password"))
    has_totp = bool(session.get("totp_key"))
    offer_link = session.get("offer_link")
    device = session.get("device")

    lines = [
        "📊 *Session Status*\n",
        f"Account: `{email}`",
        f"Credentials loaded: {'✅' if has_creds else '❌'}",
        f"2FA (TOTP): {'✅' if has_totp else '❌'}",
        f"Offer link captured: {'✅' if offer_link else '❌'}",
    ]

    if device:
        lines.append("\n" + device.summary())

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


# ── Health check server for Cloud Deployments ───────────────────────────────────
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
        
    def log_message(self, format, *args):
        # Suppress logging to prevent cluttering the log output
        return

def start_health_check_server():
    port_str = os.environ.get("PORT", "8080")
    try:
        port = int(port_str)
    except ValueError:
        port = 8080
        
    def run_server():
        server_address = ('', port)
        httpd = HTTPServer(server_address, HealthCheckHandler)
        logger.info("Health check server started on port %d", port)
        httpd.serve_forever()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()


async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle interactive user inputs (like recovery phone or verification codes) while automation is running."""
    chat_id = update.effective_chat.id
    user_input = update.message.text.strip() if update.message and update.message.text else ""
    logger.info("handle_user_reply: chat_id=%s, message='%s', pending_keys=%s",
                chat_id, user_input, list(config.PENDING_INPUTS.keys()))
                
    if chat_id in config.PENDING_INPUTS:
        # Delete the user's input message for privacy/security
        try:
            await update.message.delete()
        except Exception:
            pass
            
        config.PENDING_INPUTS[chat_id]["value"] = user_input
        config.PENDING_INPUTS[chat_id]["event"].set()
        await update.message.reply_text("⏳ Processing verification input, please wait...")
        raise ApplicationHandlerStop()


# ── Application setup ─────────────────────────────────────────────────────────

def main() -> None:
    # Start health check server for cloud deployment
    start_health_check_server()

    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Set it in Replit Secrets and restart."
        )
        sys.exit(1)

    # Kill any lingering webhook / stale getUpdates before we start polling
    _reset_webhook(token)
    import time; time.sleep(3)  # grace period for other instances to stop

    app = Application.builder().token(token).build()
    app.add_error_handler(_error_handler)

    # /login conversation
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            AWAIT_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_email)
            ],
            AWAIT_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)
            ],
            AWAIT_TOTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_totp)
            ],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(login_conv)
    app.add_handler(CommandHandler("check_offer", check_offer))
    app.add_handler(CommandHandler("get_link", get_link))
    app.add_handler(CommandHandler("status", status))
    
    # Text message handler to intercept verification replies (group=-1 runs first)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_reply), group=-1)

    logger.info("Bot is running. Press Ctrl-C to stop.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
