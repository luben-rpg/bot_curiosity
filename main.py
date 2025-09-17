import os
import logging
import json
import random
import asyncio
import logging
import sys

from datetime import time, datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from contextlib import asynccontextmanager
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackContext
from telegram.error import Forbidden, NetworkError


# Cargar variables de entorno
load_dotenv()

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuración
CONFIG_FILE = 'config.json'
FACTS_FILE = 'facts.json'
LOCK_FILE = 'bot.lock'
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Ej: https://tudominio.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-secret-token")
PORT = int(os.getenv("PORT", 8001))

# Variable global para la aplicación de Telegram
telegram_app = None

class BotManager:
    def __init__(self):
        self.config = self.load_json(CONFIG_FILE)
        self.facts_data = self.load_json(FACTS_FILE)
        
    def load_json(self, file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            if file_path == FACTS_FILE:
                return {"facts": []}
            return {}
    
    def save_json(self, data, file_path):
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    
    async def is_user_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if update.effective_chat.type == 'private':
            return True
        
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        try:
            chat_admins = await context.bot.get_chat_administrators(chat_id)
            return any(admin.user.id == user_id for admin in chat_admins)
        except Exception as e:
            logger.error(f"Error al verificar administradores en {chat_id}: {e}")
            return False

    async def send_fact(self, context: CallbackContext):
        chat_id = self.config.get('chat_id')
        if not chat_id:
            logger.warning("Job ejecutado pero no hay chat_id configurado.")
            return

        facts = self.facts_data.get('facts', [])
        if not facts:
            logger.warning("No se encontraron curiosidades en facts.json")
            return

        fact = random.choice(facts)
        
        try:
            await context.bot.send_message(
                chat_id=chat_id, 
                text=f"📚 **Curiosidad sobre C**\n\n{fact}\n\n_🕐 {datetime.now().strftime('%H:%M')}_",
                parse_mode='Markdown'
            )
            logger.info(f"Curiosidad enviada al chat {chat_id}")
        except Forbidden:
            logger.error(f"Error: Bot bloqueado en el chat {chat_id}")
            self.config.pop('chat_id', None)
            self.save_json(self.config, CONFIG_FILE)
            await self.remove_all_jobs(context.application)
        except Exception as e:
            logger.error(f"Error al enviar mensaje: {e}")

    async def remove_all_jobs(self, application: Application):
        """Elimina todos los jobs programados"""
        for job in application.job_queue.jobs():
            if job.name and job.name.startswith("daily_fact_"):
                job.schedule_removal()

    async def setup_daily_jobs(self, application: Application):
        """Configura los jobs diarios"""
        await self.remove_all_jobs(application)
        
        # Programar 8 envíos diarios
        times = [
            time(hour=9, minute=0),   # 9:00 AM
            time(hour=12, minute=0),  # 12:00 PM
            time(hour=15, minute=0),  # 3:00 PM
            time(hour=18, minute=0),  # 6:00 PM
            time(hour=21, minute=0),  # 9:00 PM
            time(hour=0, minute=0),   # 12:00 AM
            time(hour=3, minute=0),   # 3:00 AM
            time(hour=6, minute=0)    # 6:00 AM
        ]
        
        for i, t in enumerate(times, 1):
            application.job_queue.run_daily(
                self.send_fact, 
                time=t, 
                name=f"daily_fact_{i}"
            )

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo los administradores pueden configurar este bot.")
            return

        chat_id = update.effective_chat.id
        
        if self.config.get('chat_id') == chat_id:
            await update.message.reply_text(
                "✅ El bot ya está activo en este chat.\n\n"
                "📅 Enviando 8 curiosidades diarias sobre C a las:\n"
                "• 12:00 AM / PM 🌅\n"
                "• 6:00 AM / PM  ☀️\n"
                "• 3:00 PM / PM ☀️\n" 
                "• 9:00 PM / AM 🌙\n\n"
                "Usa /stop para detener el bot."
            )
            return

        self.config['chat_id'] = chat_id
        self.config['admin_id'] = update.effective_user.id
        self.config['setup_date'] = datetime.now().isoformat()
        self.save_json(self.config, CONFIG_FILE)

        await self.setup_daily_jobs(context.application)

        await update.message.reply_text(
            "🚀 **Bot activado exitosamente!**\n\n"
            "📚 A partir de ahora enviaré 8 curiosidades diarias sobre el lenguaje C:\n"
            "• 12:00 AM / PM 🌅\n"
            "• 6:00 AM / PM  ☀️\n"
            "• 3:00 PM / PM ☀️\n" 
            "• 9:00 PM / AM 🌙\n\n"
            "💡 _Solo administradores pueden modificar esta configuración._\n"
            "❌ Usa /stop para detener el bot.",
            parse_mode='Markdown'
        )
        logger.info(f"Bot configurado por admin {update.effective_user.id} en chat {chat_id}")

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo los administradores pueden detener este bot.")
            return

        chat_id = update.effective_chat.id
        
        if self.config.get('chat_id') != chat_id:
            await update.message.reply_text("ℹ️ El bot no está activo en este chat.")
            return

        self.config.pop('chat_id', None)
        self.config.pop('admin_id', None)
        self.save_json(self.config, CONFIG_FILE)

        await self.remove_all_jobs(context.application)

        await update.message.reply_text(
            "🛑 **Bot detenido exitosamente!**\n\n"
            "📚 Ya no enviaré curiosidades diarias sobre C.\n"
            "🚀 Usa /start para reactivar cuando quieras.",
            parse_mode='Markdown'
        )
        logger.info(f"Bot detenido por admin {update.effective_user.id} en chat {chat_id}")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo los administradores pueden ver el estado.")
            return

        chat_id = update.effective_chat.id
        is_active = self.config.get('chat_id') == chat_id
        
        status_text = "✅ **ACTIVO**" if is_active else "❌ **INACTIVO**"
        setup_date = self.config.get('setup_date', 'No configurado')
        
        await update.message.reply_text(
            f"📊 **Estado del Bot**\n\n"
            f"• Estado: {status_text}\n"
            f"• Chat ID: {chat_id}\n"
            f"• Fecha configuración: {setup_date}\n"
            f"• Curiosidades disponibles: {len(self.facts_data.get('facts', []))}\n\n"
            f"🛠️ _Comandos disponibles:_\n"
            f"/start - Activar bot\n"
            f"/stop - Detener bot\n"
            f"/status - Ver estado\n"
            f"/addfact [curiosidad] - Añadir curiosidad",
            parse_mode='Markdown'
        )

    async def addfact_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo los administradores pueden añadir curiosidades.")
            return

        try:
            fact_text = context.args
            if not fact_text:
                raise IndexError
            
            fact_text = " ".join(fact_text)
            
            # Reload facts data to avoid overwriting
            self.facts_data = self.load_json(FACTS_FILE)
            
            if 'facts' not in self.facts_data:
                self.facts_data['facts'] = []

            self.facts_data['facts'].append(fact_text)
            self.save_json(self.facts_data, FACTS_FILE)

            await update.message.reply_text("✅ ¡Curiosidad añadida con éxito!")
            logger.info(f"Nueva curiosidad añadida por {update.effective_user.id}: {fact_text}")

        except IndexError:
            await update.message.reply_text("⚠️ Por favor, proporciona un dato curioso después del comando.\nEjemplo: `/addfact C es un lenguaje compilado.`", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error en addfact_command: {e}")
            await update.message.reply_text("❌ Ocurrió un error al añadir la curiosidad.")

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Error: {context.error}", exc_info=context.error)
        
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Ocurrió un error inesperado. Por favor, intenta nuevamente."
            )

# Lifespan events para FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.path.exists(LOCK_FILE):
        logger.error("Lock file exists. Another instance is likely running. Exiting.")
        raise RuntimeError("Lock file exists, exiting.")
    
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    # Startup
    global telegram_app
    logger.info("Iniciando aplicación FastAPI + Telegram Bot...")
    
    if not TELEGRAM_TOKEN:
        logger.critical("No se encontró TELEGRAM_TOKEN")
        raise HTTPException(status_code=500, detail="Token de Telegram no configurado")
    
    # Crear aplicación de Telegram
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_manager = BotManager()
    
    # Añadir handlers
    telegram_app.add_handler(CommandHandler("start", bot_manager.start_command))
    telegram_app.add_handler(CommandHandler("stop", bot_manager.stop_command))
    telegram_app.add_handler(CommandHandler("status", bot_manager.status_command))
    telegram_app.add_handler(CommandHandler("addfact", bot_manager.addfact_command))
    telegram_app.add_error_handler(bot_manager.error_handler)
    
    # Configurar webhook si está configurado
    if WEBHOOK_URL:
        logger.info(f"Configurando webhook: {WEBHOOK_URL}/webhook")
        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/webhook",
            secret_token=WEBHOOK_SECRET
        )
        
        # Configurar jobs si ya hay un chat configurado
        if bot_manager.config.get('chat_id'):
            await bot_manager.setup_daily_jobs(telegram_app)
    else:
        logger.info("Modo polling activado (sin webhook")
        # Iniciar polling en segundo plano
        asyncio.create_task(run_polling())
    
    yield
    
    # Shutdown
    logger.info("Apagando aplicación...")
    if telegram_app:
        if WEBHOOK_URL:
            await telegram_app.bot.delete_webhook()
        await telegram_app.shutdown()

    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)

async def run_polling():
    """Ejecutar polling en segundo plano si no hay webhook"""
    if not WEBHOOK_URL and telegram_app:
        logger.info("Iniciando bot en modo polling...")
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()

# Crear aplicación FastAPI
app = FastAPI(
    title="Telegram Bot API",
    description="API para gestionar el bot de curiosidades de C",
    version="1.0.0",
    lifespan=lifespan
)

# Instancia del bot manager
bot_manager = BotManager()

# Rutas de la API
@app.get("/")
async def root():
    return {"message": "Bot de curiosidades de C funcionando correctamente"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/status")
async def get_status():
    config = bot_manager.load_json(CONFIG_FILE)
    facts = bot_manager.load_json(FACTS_FILE)
    
    return {
        "active": bool(config.get('chat_id')),
        "chat_id": config.get('chat_id'),
        "admin_id": config.get('admin_id'),
        "setup_date": config.get('setup_date'),
        "total_facts": len(facts.get('facts', [])),
        "webhook_configured": bool(WEBHOOK_URL)
    }

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Endpoint para recibir updates de Telegram"""
    if WEBHOOK_SECRET:
        secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret_token != WEBHOOK_SECRET:
            logger.warning("Intento de acceso no autorizado al webhook")
            raise HTTPException(status_code=403, detail="No autorizado")
    
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error procesando webhook: {e}")
        raise HTTPException(status_code=500, detail="Error interno")

@app.post("/send-test")
async def send_test_message():
    """Endpoint para enviar un mensaje de prueba"""
    config = bot_manager.load_json(CONFIG_FILE)
    chat_id = config.get('chat_id')
    
    if not chat_id:
        raise HTTPException(status_code=400, detail="No hay chat configurado")
    
    facts = bot_manager.load_json(FACTS_FILE).get('facts', [])
    if not facts:
        raise HTTPException(status_code=400, detail="No hay curiosidades disponibles")
    
    fact = random.choice(facts)
    
    try:
        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=f"🧪 **Mensaje de prueba**\n\n{fact}\n\n_✅ Bot funcionando correctamente_",
            parse_mode='Markdown'
        )
        return {"status": "success", "message": "Mensaje de prueba enviado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error enviando mensaje: {e}")

# Página HTML simple para ver el estado
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    config = bot_manager.load_json(CONFIG_FILE)
    facts = bot_manager.load_json(FACTS_FILE)
    
    status = "🟢 ACTIVO" if config.get('chat_id') else "🔴 INACTIVO"
    
    html_content = f"""
    <html>
        <head>
            <title>Dashboard - Bot de Curiosidades de C</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; }}
                .card {{ background: #f4f4f4; padding: 20px; margin: 10px 0; border-radius: 8px; }}
                .active {{ color: green; }}
                .inactive {{ color: red; }}
            </style>
        </head>
        <body>
            <h1>🤖 Dashboard - Bot de Curiosidades de C</h1>
            
            <div class="card">
                <h2>Estado del Bot: <span class="{ 'active' if config.get('chat_id') else 'inactive' }">{status}</span></h2>
                <p><strong>Chat ID:</strong> {config.get('chat_id', 'No configurado')}</p>
                <p><strong>Admin ID:</strong> {config.get('admin_id', 'No configurado')}</p>
                <p><strong>Fecha configuración:</strong> {config.get('setup_date', 'No configurado')}</p>
                <p><strong>Curiosidades disponibles:</strong> {len(facts.get('facts', []))}</p>
                <p><strong>Webhook:</strong> {'🟢 CONFIGURADO' if WEBHOOK_URL else '🔴 NO CONFIGURADO'}</p>
            </div>
            
            <div class="card">
                <h2>Acciones</h2>
                <p><a href="/health">✅ Health Check</a></p>
                <p><a href="/status">📊 Estado JSON</a></p>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)