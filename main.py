import os
import logging
import json
import random
import asyncio
import sqlite3

from datetime import time, datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from contextlib import asynccontextmanager
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackContext, CallbackQueryHandler
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
FACTS_JSON_FILE = 'facts.json' # Renombrado para claridad
DATABASE_FILE = 'facts.db'
LOCK_FILE = 'bot.lock'
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = None # Forzamos el modo polling para depuración
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-secret-token")
PORT = int(os.getenv("PORT", 8001))

# Variable global para la aplicación de Telegram
telegram_app = None

class BotManager:
    def __init__(self):
        self.config = self._load_config()
        self._init_db()
        
    def _load_config(self):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
                # Asegurar que configured_chat_ids sea una lista
                if 'configured_chat_ids' not in config_data or not isinstance(config_data['configured_chat_ids'], list):
                    config_data['configured_chat_ids'] = []
                return config_data
        except (FileNotFoundError, json.JSONDecodeError):
            return {'configured_chat_ids': []}
    
    def _save_config(self):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)

    def _get_db_connection(self):
        conn = sqlite3.connect(DATABASE_FILE)
        conn.row_factory = sqlite3.Row # Permite acceder a las columnas por nombre
        return conn

    def _init_db(self):
        conn = self._get_db_connection()
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, fact_text TEXT NOT NULL UNIQUE)")
        conn.commit()

        # Migrar datos de facts.json si existen y la DB está vacía
        cursor.execute("SELECT COUNT(*) FROM facts")
        db_fact_count = cursor.fetchone()[0]

        if db_fact_count == 0 and os.path.exists(FACTS_JSON_FILE):
            logger.info(f"Migrando curiosidades de {FACTS_JSON_FILE} a la base de datos.")
            try:
                with open(FACTS_JSON_FILE, 'r', encoding='utf-8') as f:
                    json_data = json.load(f)
                facts_to_migrate = json_data.get('facts', [])
                
                for fact in facts_to_migrate:
                    try:
                        cursor.execute("INSERT INTO facts (fact_text) VALUES (?)", (fact,))
                    except sqlite3.IntegrityError: # Manejar duplicados si los hubiera
                        logger.warning(f"Curiosidad duplicada no insertada: {fact[:50]}...")
                conn.commit()
                logger.info(f"Migración completada. {len(facts_to_migrate)} curiosidades migradas.")
                os.remove(FACTS_JSON_FILE) # Eliminar el archivo JSON después de la migración
                logger.info(f"Archivo {FACTS_JSON_FILE} eliminado.")
            except (FileNotFoundError, json.JSONDecodeError) as e:
                logger.error(f"Error al migrar {FACTS_JSON_FILE}: {e}")
        conn.close()

    async def is_user_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        owner_id = self.config.get('owner_id')
        if owner_id is None:
            # If owner_id is not set, no one is admin yet. The first user to call /start will become the admin.
            return False
        
        return update.effective_user.id == owner_id

    async def send_fact(self, context: CallbackContext):
        configured_chat_ids = self.config.get('configured_chat_ids', [])
        if not configured_chat_ids:
            logger.warning("Job ejecutado pero no hay chat_id configurado en configured_chat_ids.")
            return

        conn = self._get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT fact_text FROM facts ORDER BY RANDOM() LIMIT 1")
        fact_row = cursor.fetchone()
        conn.close()

        if not fact_row:
            logger.warning("No se encontraron curiosidades en la base de datos.")
            # Consider sending a message to active_chat_id if no facts are available
            return

        fact = fact_row['fact_text']
        
        for chat_id in configured_chat_ids:
            try:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=f"📚 **Curiosidad sobre C**\n\n{fact}\n\n_🕐 {datetime.now().strftime('%H:%M')}_",
                    parse_mode='Markdown'
                )
                logger.info(f"Curiosidad enviada al chat {chat_id}")
            except Forbidden:
                logger.error(f"Error: Bot bloqueado en el chat {chat_id}. Eliminando de la lista.")
                self.config['configured_chat_ids'].remove(chat_id)
                self._save_config()
            except Exception as e:
                logger.error(f"Error al enviar mensaje al chat {chat_id}: {e}")

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
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # If owner_id is not set, the current user becomes the owner
        if self.config.get('owner_id') is None:
            self.config['owner_id'] = user_id
            self._save_config() # Save immediately so other checks work
            logger.info(f"Owner ID set to {user_id}")
        
        # Now, check if the current user is the owner
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo el propietario del bot puede configurar esto.")
            return

        # Add current chat to configured_chat_ids if not already there
        if chat_id not in self.config['configured_chat_ids']:
            self.config['configured_chat_ids'].append(chat_id)
            await update.message.reply_text(f"✅ Este chat ({chat_id}) ha sido añadido a la lista de publicación.")
        
        self.config['active_chat_id'] = chat_id # Set current chat as active
        self.config['setup_date'] = datetime.now().isoformat()
        self._save_config()

        await self.setup_daily_jobs(context.application)

        await update.message.reply_text(
            "🚀 **Bot activado exitosamente!**\n\n" 
            "📚 A partir de ahora enviaré 8 curiosidades diarias sobre el lenguaje C a los chats configurados.\n" 
            "💡 _Solo el propietario del bot puede modificar esta configuración._\n" 
            "❌ Usa /stop para detener el bot.",
            parse_mode='Markdown'
        )
        logger.info(f"Bot configurado por propietario {user_id} en chat {chat_id}")

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo el propietario del bot puede detener este bot.")
            return

        chat_id = update.effective_chat.id
        
        if chat_id in self.config['configured_chat_ids']:
            self.config['configured_chat_ids'].remove(chat_id)
            await update.message.reply_text(f"🛑 Este chat ({chat_id}) ha sido eliminado de la lista de publicación.")
        
        if self.config.get('active_chat_id') == chat_id:
            self.config.pop('active_chat_id', None)

        self._save_config()

        # Si no quedan chats configurados, detener los jobs
        if not self.config['configured_chat_ids']:
            await self.remove_all_jobs(context.application)
            await update.message.reply_text(
                "🛑 **Bot detenido completamente!**\n\n" 
                "📚 Ya no enviaré curiosidades diarias sobre C a ningún chat.\n" 
                "🚀 Usa /start en un chat para reactivar cuando quieras.",
                parse_mode='Markdown'
            )
            logger.info(f"Bot detenido completamente por propietario {update.effective_user.id}")
        else:
            await update.message.reply_text(
                "🛑 **Bot detenido en este chat!**\n\n" 
                "📚 Seguiré enviando curiosidades a los otros chats configurados.\n" 
                "🚀 Usa /start en este chat para reactivar cuando quieras.",
                parse_mode='Markdown'
            )
            logger.info(f"Bot detenido en chat {chat_id} por propietario {update.effective_user.id}")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo el propietario del bot puede ver el estado.")
            return

        chat_id = update.effective_chat.id
        is_active_in_this_chat = chat_id in self.config.get('configured_chat_ids', [])
        
        status_text = "✅ **ACTIVO**" if is_active_in_this_chat else "❌ **INACTIVO**"
        setup_date = self.config.get('setup_date', 'No configurado')
        owner_id = self.config.get('owner_id', 'No configurado')

        conn = self._get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM facts")
        total_facts = cursor.fetchone()[0]
        conn.close()
        
        configured_chats_str = ", ".join(map(str, self.config.get('configured_chat_ids', []))) if self.config.get('configured_chat_ids') else "Ninguno"

        await update.message.reply_text(
            f"""
📊 **Estado del Bot**

• Propietario: {owner_id}
• Estado en este chat: {status_text}
• Chat ID actual: {chat_id}
• Chats configurados: {configured_chats_str}
• Fecha configuración: {setup_date}
• Curiosidades disponibles: {total_facts}

🛠️ _Comandos disponibles:_
/start - Activar bot en este chat
/stop - Detener bot en este chat
/status - Ver estado
/addfact [curiosidad] - Añadir curiosidad
/listchats - Listar chats configurados
/config - Menú de configuración""",
            parse_mode='Markdown'
        )

    async def addfact_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo el propietario del bot puede añadir curiosidades.")
            return

        try:
            if not context.args:
                raise IndexError # No arguments provided

            # Join all arguments into a single string, then split by delimiter
            input_text = " ".join(context.args)
            facts_to_add = [f.strip() for f in input_text.split('---') if f.strip()] # Split by '---' and clean up

            if not facts_to_add:
                raise IndexError # No valid facts found after splitting

            conn = self._get_db_connection()
            cursor = conn.cursor()
            added_count = 0
            skipped_count = 0

            for fact_text in facts_to_add:
                try:
                    cursor.execute("INSERT INTO facts (fact_text) VALUES (?) ", (fact_text,))
                    conn.commit()
                    added_count += 1
                except sqlite3.IntegrityError:
                    skipped_count += 1
                    logger.warning(f"Curiosidad duplicada no insertada: {fact_text[:50]}...")
            
            conn.close()

            response_message = f"✅ ¡Operación completada!\n"
            if added_count > 0:
                response_message += f"Se añadieron {added_count} curiosidades nuevas.\n"
            if skipped_count > 0:
                response_message += f"Se omitieron {skipped_count} curiosidades (ya existían).\n"
            
            await update.message.reply_text(response_message)
            logger.info(f"Curiosidades añadidas por {update.effective_user.id}: {added_count} nuevas, {skipped_count} omitidas.")

        except IndexError:
            await update.message.reply_text("⚠️ Por favor, proporciona una o varias curiosidades después del comando.\nSepara cada curiosidad con `---`.\nEjemplo: `/addfact Curiosidad 1 --- Curiosidad 2 --- Curiosidad 3`", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error en addfact_command: {e}")
            await update.message.reply_text("❌ Ocurrió un error al añadir la curiosidad.")

    async def list_chats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo el propietario del bot puede listar los chats.")
            return
        
        configured_chat_ids = self.config.get('configured_chat_ids', [])
        if not configured_chat_ids:
            await update.message.reply_text("No hay chats configurados para la publicación.")
            return
        
        chat_list_str = "Chats configurados para publicación:\n"
        for chat_id in configured_chat_ids:
            chat_list_str += f"- `{chat_id}`\n"
        
        await update.message.reply_text(chat_list_str, parse_mode='Markdown')

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Error: {context.error}", exc_info=context.error)
        
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Ocurrió un error inesperado. Por favor, intenta nuevamente."
            )

    async def config_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo el propietario del bot puede acceder al menú de configuración.")
            return

        keyboard = [
            [InlineKeyboardButton("Ver Estado", callback_data='config_status')],
            [InlineKeyboardButton("Añadir Curiosidad", callback_data='config_addfact')],
            [InlineKeyboardButton("Gestionar Chats", callback_data='config_manage_chats')],
            [InlineKeyboardButton("Detener Bot en este Chat", callback_data='config_stop')],
            [InlineKeyboardButton("Activar Bot en este Chat", callback_data='config_start')],
            [InlineKeyboardButton("Cerrar Menú", callback_data='config_close')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text('Menú de Configuración:', reply_markup=reply_markup)

    async def manage_chats_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.is_user_admin(update, context):
            await update.message.reply_text("❌ Solo el propietario del bot puede gestionar chats.")
            return

        keyboard = [
            [InlineKeyboardButton("Listar Chats", callback_data='manage_chats_list')],
            [InlineKeyboardButton("Añadir Chat Actual", callback_data='manage_chats_add_current')],
            [InlineKeyboardButton("Eliminar Chat Actual", callback_data='manage_chats_remove_current')],
            [InlineKeyboardButton("Volver al Menú Principal", callback_data='config_menu_main')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text('Menú de Gestión de Chats:', reply_markup=reply_markup)

    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer() # Acknowledge the query

        if not await self.is_user_admin(update, context):
            await query.edit_message_text("❌ No tienes permiso para realizar esta acción.")
            return

        action = query.data
        chat_id = update.effective_chat.id

        if action == 'config_status':
            await self.status_command(update, context)
        elif action == 'config_addfact':
            await query.edit_message_text("Para añadir una curiosidad, usa el comando: `/addfact [tu curiosidad aquí]`\nSepara múltiples curiosidades con `---`.", parse_mode='Markdown')
        elif action == 'config_manage_chats':
            await self.manage_chats_menu(update, context)
        elif action == 'config_stop':
            await self.stop_command(update, context)
        elif action == 'config_start':
            await self.start_command(update, context)
        elif action == 'config_close':
            await query.edit_message_text("Menú cerrado.")
        elif action == 'manage_chats_list':
            await self.list_chats_command(update, context)
        elif action == 'manage_chats_add_current':
            if chat_id not in self.config['configured_chat_ids']:
                self.config['configured_chat_ids'].append(chat_id)
                self._save_config()
                await query.edit_message_text(f"✅ Este chat ({chat_id}) ha sido añadido a la lista de publicación.")
            else:
                await query.edit_message_text(f"ℹ️ Este chat ({chat_id}) ya está en la lista de publicación.")
        elif action == 'manage_chats_remove_current':
            if chat_id in self.config['configured_chat_ids']:
                self.config['configured_chat_ids'].remove(chat_id)
                self._save_config()
                await query.edit_message_text(f"🛑 Este chat ({chat_id}) ha sido eliminado de la lista de publicación.")
                # Si el chat eliminado era el activo, desconfigurarlo
                if self.config.get('active_chat_id') == chat_id:
                    self.config.pop('active_chat_id', None)
                    self._save_config()
            else:
                await query.edit_message_text(f"ℹ️ Este chat ({chat_id}) no está en la lista de publicación.")
        elif action == 'config_menu_main':
            await self.config_menu(update, context)

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
    telegram_app.add_handler(CommandHandler("listchats", bot_manager.list_chats_command)) # Nuevo handler
    telegram_app.add_handler(CommandHandler("config", bot_manager.config_menu)) 
    telegram_app.add_handler(CallbackQueryHandler(bot_manager.callback_handler)) 
    telegram_app.add_error_handler(bot_manager.error_handler)
    
    # Configurar webhook si está configurado
    if WEBHOOK_URL:
        logger.info(f"Configurando webhook: {WEBHOOK_URL}/webhook")
        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/webhook",
            secret_token=WEBHOOK_SECRET
        )
        
        # Configurar jobs si ya hay un chat configurado
        if bot_manager.config.get('chat_id'): # This is now active_chat_id, but still works for initial setup
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
    config = bot_manager._load_config()
    
    conn = bot_manager._get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM facts")
    total_facts = cursor.fetchone()[0]
    conn.close()

    return {
        "active": bool(config.get('active_chat_id')),
        "owner_id": config.get('owner_id'),
        "active_chat_id": config.get('active_chat_id'),
        "configured_chat_ids": config.get('configured_chat_ids', []),
        "setup_date": config.get('setup_date'),
        "total_facts": total_facts,
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
        logger.exception("Error procesando webhook") # Changed to logger.exception
        raise HTTPException(status_code=500, detail="Error interno")

@app.post("/send-test")
async def send_test_message():
    """Endpoint para enviar un mensaje de prueba"""
    config = bot_manager._load_config()
    configured_chat_ids = config.get('configured_chat_ids', [])
    
    if not configured_chat_ids:
        raise HTTPException(status_code=400, detail="No hay chats configurados para enviar mensajes.")
    
    conn = bot_manager._get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT fact_text FROM facts ORDER BY RANDOM() LIMIT 1")
    fact_row = cursor.fetchone()
    conn.close()

    if not fact_row:
        raise HTTPException(status_code=400, detail="No hay curiosidades disponibles")
    
    fact = fact_row['fact_text']
    
    for chat_id in configured_chat_ids:
        try:
            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=f"🧪 **Mensaje de prueba**\n\n{fact}\n\n_✅ Bot funcionando correctamente_",
                parse_mode='Markdown'
            )
            logger.info(f"Mensaje de prueba enviado al chat {chat_id}")
        except Exception as e:
            logger.error(f"Error enviando mensaje de prueba al chat {chat_id}: {e}")
            
    return {"status": "success", "message": "Mensaje de prueba enviado a los chats configurados"}

# Página HTML simple para ver el estado
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    config = bot_manager._load_config()
    
    conn = bot_manager._get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM facts")
    total_facts = cursor.fetchone()[0]
    conn.close()

    status = "🟢 ACTIVO" if config.get('active_chat_id') else "🔴 INACTIVO"
    configured_chats_str = ", ".join(map(str, config.get('configured_chat_ids', []))) if config.get('configured_chat_ids') else "Ninguno"

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
                <h2>Estado del Bot: <span class="{ 'active' if config.get('active_chat_id') else 'inactive' }" >{status}</span></h2>
                <p><strong>Propietario ID:</strong> {config.get('owner_id', 'No configurado')}</p>
                <p><strong>Chat ID Activo:</strong> {config.get('active_chat_id', 'No configurado')}</p>
                <p><strong>Chats Configurados:</strong> {configured_chats_str}</p>
                <p><strong>Fecha configuración:</strong> {config.get('setup_date', 'No configurado')}</p>
                <p><strong>Curiosidades disponibles:</strong> {total_facts}</p>
                <p><strong>Webhook:</strong> {'🟢 CONFIGURADO' if WEBHOOK_URL else '🔴 NO CONFIGURADO'}</p>
            </div>
            
            <div class="card">
                <h2>Acciones</h2>
                <p><a href="/health">✅ Health Check</a></p>
                <p><a href="/status">📊 Estado JSON</a></p>
                <p><a href="/send-test">🧪 Enviar Mensaje de Prueba</a></p>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
