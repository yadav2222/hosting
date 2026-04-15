#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import time
import shutil
import zipfile
import tarfile
import sqlite3
import signal
import ast
import importlib
import importlib.util
import html as html_lib
import logging
from datetime import datetime

# Auto install required packages
def install_requirements():
    requirements = [
        "pyTelegramBotAPI",
        "requests", 
        "psutil"
    ]
    
    for package in requirements:
        try:
            if package == "pyTelegramBotAPI":
                import telebot
            elif package == "psutil":
                import psutil
            elif package == "requests":
                import requests
            print(f"✅ {package} already installed")
        except ImportError:
            print(f"📦 Installing {package}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
                print(f"✅ {package} installed successfully")
            except Exception as e:
                print(f"❌ Failed to install {package}: {e}")

# Install requirements before importing
install_requirements()

# Now import the packages
import psutil
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8213136029:AAGHxp_VFaxoOBaPpc-osiEDuCm2sijQNkc"

# Load thresholds
CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "90.0"))
MEMORY_THRESHOLD = float(os.environ.get("MEMORY_THRESHOLD", "90.0"))
MAX_RUNNING_PROCESSES = int(os.environ.get("MAX_RUNNING_PROCESSES", "10"))
MAX_FILES_PER_USER = int(os.environ.get("MAX_FILES_PER_USER", "3"))

# Application directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "metadata.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")

# Create directories
for directory in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    os.makedirs(directory, exist_ok=True)

# Start time for uptime
START_TIME = datetime.utcnow()

# Database setup
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

def init_db():
    with db_lock:
        cur = conn.cursor()
        # Files table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                filename TEXT,
                orig_name TEXT,
                path TEXT,
                uploaded_at TEXT,
                file_type TEXT,
                pid INTEGER,
                status TEXT DEFAULT 'Stopped'
            )
        ''')
        # Runs table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                started_at TEXT,
                finished_at TEXT,
                pid INTEGER,
                log_path TEXT,
                exit_code INTEGER
            )
        ''')
        # Users table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TEXT,
                last_seen TEXT
            )
        ''')
        conn.commit()

init_db()

# Database helpers
def add_file_record(user_id, username, filename, orig_name, path, file_type):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (user_id, username, filename, orig_name, path, uploaded_at, file_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, filename, orig_name, path, datetime.utcnow().isoformat(), file_type)
        )
        conn.commit()
        return cur.lastrowid

def list_user_files(user_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, filename, orig_name, uploaded_at, file_type, status, pid FROM files WHERE user_id=? ORDER BY id DESC",
        (user_id,)
    )
    return cur.fetchall()

def get_file_record(file_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM files WHERE id=?", (file_id,))
    return cur.fetchone()

def remove_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()

def record_run_start(file_id, pid, log_path):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO runs (file_id, started_at, pid, log_path) VALUES (?, ?, ?, ?)",
            (file_id, datetime.utcnow().isoformat(), pid, log_path)
        )
        conn.commit()
        return cur.lastrowid

def record_run_finish(run_id, exit_code):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE runs SET finished_at=?, exit_code=? WHERE id=?",
            (datetime.utcnow().isoformat(), exit_code, run_id)
        )
        conn.commit()

def update_file_status(file_id, pid, status):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET pid=?, status=? WHERE id=?",
            (pid, status, file_id)
        )
        conn.commit()

# Process management
processes = {}
proc_lock = threading.Lock()

# System monitoring
def get_system_load():
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        proc_count = len(processes)
        return float(cpu), float(mem), int(proc_count)
    except Exception as e:
        logger.error(f"Error getting system load: {e}")
        return 0.0, 0.0, 0

def should_stop_due_to_load():
    load, memory, process_count = get_system_load()
    if process_count >= MAX_RUNNING_PROCESSES:
        return True, f"Too many running processes ({process_count}/{MAX_RUNNING_PROCESSES})"
    if load >= CPU_THRESHOLD:
        return True, f"High CPU load ({load}%)"
    if memory >= MEMORY_THRESHOLD:
        return True, f"High memory usage ({memory}%)"
    return False, None

# File utilities
def get_file_type(filename):
    if not filename:
        return "unknown"
    name = filename.lower()
    if name.endswith(".py"):
        return "python"
    if name.endswith(".js"):
        return "javascript"
    if name.endswith(".zip"):
        return "zip"
    if any(name.endswith(ext) for ext in [".tar", ".tar.gz", ".tgz"]):
        return "archive"
    return "unknown"

def extract_archive(file_path, extract_dir):
    try:
        if file_path.lower().endswith(".zip"):
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
        elif file_path.lower().endswith(".tar.gz") or file_path.lower().endswith(".tgz"):
            with tarfile.open(file_path, 'r:gz') as tar_ref:
                tar_ref.extractall(extract_dir)
        elif file_path.lower().endswith(".tar"):
            with tarfile.open(file_path, 'r') as tar_ref:
                tar_ref.extractall(extract_dir)
        else:
            return False, "Unsupported archive format"
        return True, None
    except Exception as e:
        return False, str(e)

def find_main_file(directory):
    """Find the main executable file in a directory"""
    priority_files = [
        "main.py", "bot.py", "app.py", "server.py", "index.py", "script.py",
        "main.js", "bot.js", "app.js", "server.js", "index.js", "script.js"
    ]
    
    # First check root directory for priority files
    for file_name in priority_files:
        file_path = os.path.join(directory, file_name)
        if os.path.isfile(file_path):
            logger.info(f"Found priority file: {file_path}")
            return file_path
    
    # Search recursively for priority files
    for root, dirs, files in os.walk(directory):
        for file_name in priority_files:
            if file_name in files:
                file_path = os.path.join(root, file_name)
                logger.info(f"Found priority file recursively: {file_path}")
                return file_path
    
    # If no priority files found, look for any .py or .js file
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".py") or file.endswith(".js"):
                file_path = os.path.join(root, file)
                logger.info(f"Found fallback file: {file_path}")
                return file_path
    
    return None

def install_requirements_from_file(requirements_path, chat_id, file_name):
    try:
        if not os.path.exists(requirements_path):
            return False, "requirements.txt not found"
        
        logger.info(f"Installing requirements from {requirements_path}")
        
        # Read requirements
        with open(requirements_path, 'r') as f:
            requirements = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
        if not requirements:
            return True, "No requirements found in requirements.txt"
        
        success_count = 0
        failed_count = 0
        failed_packages = []
        
        for package in requirements:
            try:
                logger.info(f"Installing {package}")
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", package],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0:
                    success_count += 1
                    logger.info(f"Successfully installed {package}")
                else:
                    failed_count += 1
                    failed_packages.append(package)
                    logger.error(f"Failed to install {package}: {result.stderr}")
            except subprocess.TimeoutExpired:
                failed_count += 1
                failed_packages.append(f"{package} (timeout)")
                logger.error(f"Timeout installing {package}")
            except Exception as e:
                failed_count += 1
                failed_packages.append(f"{package} ({str(e)})")
                logger.error(f"Error installing {package}: {e}")
        
        message = f"Installed {success_count} packages"
        if failed_count > 0:
            message += f", failed {failed_count}: {', '.join(failed_packages[:5])}"
        
        return failed_count == 0, message
        
    except Exception as e:
        logger.error(f"Error in install_requirements_from_file: {e}")
        return False, f"Error installing requirements: {str(e)}"

# Import detection
def extract_imports(file_path):
    imports = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imports.add(node.module.split('.')[0])
    except Exception as e:
        logger.error(f"Error parsing imports: {e}")
    return imports

def install_missing_imports(imports, chat_id, file_name):
    missing = []
    for module in imports:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    
    if not missing:
        return True, "All imports available"
    
    success_count = 0
    failed_count = 0
    failed_modules = []
    
    pip_name_map = {
        'telebot': 'pyTelegramBotAPI',
        'PIL': 'Pillow',
        'cv2': 'opencv-python',
        'Crypto': 'pycryptodome',
        'bs4': 'beautifulsoup4'
    }
    
    for module in missing:
        pip_name = pip_name_map.get(module, module)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", pip_name],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                success_count += 1
                logger.info(f"Successfully installed {module}")
            else:
                failed_count += 1
                failed_modules.append(module)
                logger.error(f"Failed to install {module}: {result.stderr}")
        except Exception as e:
            failed_count += 1
            failed_modules.append(module)
            logger.error(f"Error installing {module}: {e}")
    
    message = f"Installed {success_count} modules"
    if failed_count > 0:
        message += f", failed {failed_count}: {', '.join(failed_modules)}"
    
    return failed_count == 0, message

# Telegram bot
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Keyboards
def main_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📢 Updates Channel"))
    kb.add(KeyboardButton("📤 Upload File"), KeyboardButton("📁 My Files"))
    kb.add(KeyboardButton("⚡ Bot Speed"), KeyboardButton("📊 Statistics"))
    kb.add(KeyboardButton("📞 Contact Owner"))
    return kb

def file_actions_kb(file_id, is_running=False):
    kb = InlineKeyboardMarkup()
    if is_running:
        kb.row(
            InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}")
        )
    else:
        kb.row(
            InlineKeyboardButton("▶️ Start", callback_data=f"start:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}")
        )
    kb.row(
        InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{file_id}"),
        InlineKeyboardButton("📄 Logs", callback_data=f"logs:{file_id}")
    )
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_to_files"))
    return kb

# Bot handlers
@bot.message_handler(commands=['start', 'help'])
def start_handler(message):
    user = message.from_user
    user_id = user.id
    
    # Update user in database
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO users (user_id, username, joined_at, last_seen) VALUES (?, ?, ?, ?)",
            (user_id, user.username or "", datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
        )
        conn.commit()
    
    files = list_user_files(user_id)
    file_count = len(files)
    
    welcome_text = f"""
🔥 <b>24x7 TEAM X HOSTING BOT</b>

👋 Welcome <b>{html_lib.escape(user.first_name or 'User')}</b>
🆔 Your ID: <code>{user_id}</code>
📂 Files: {file_count}/{MAX_FILES_PER_USER}

🤖 <b>Features:</b>
• Host Python & JS scripts
• Auto-install dependencies
• 24/7 operation
• Real-time logs

👇 Use buttons below to get started!
    """
    
    bot.send_message(message.chat.id, welcome_text, reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "📢 Updates Channel")
def updates_handler(message):
    bot.send_message(message.chat.id, "📢 Join our channel: https://t.me/TEAM_X_LEGACY")

@bot.message_handler(func=lambda m: m.text == "📞 Contact Owner")
def contact_handler(message):
    bot.send_message(message.chat.id, "📞 Contact: @XAHAF_LEGACY")

@bot.message_handler(func=lambda m: m.text == "⚡ Bot Speed")
def speed_handler(message):
    cpu, memory, processes_count = get_system_load()
    uptime_td = datetime.utcnow() - START_TIME
    days = uptime_td.days
    hours, remainder = divmod(uptime_td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"
    
    bot.send_message(
        message.chat.id,
        f"⚡ <b>System Status</b>\n\n"
        f"• CPU Usage: {cpu:.1f}%\n"
        f"• Memory Usage: {memory:.1f}%\n"
        f"• Running Processes: {processes_count}\n"
        f"• Max Processes: {MAX_RUNNING_PROCESSES}\n"
        f"• Uptime: {uptime_str}"
    )

@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def stats_handler(message):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM files")
    user_count = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files")
    file_count = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'")
    running_count = cur.fetchone()[0] or 0
    
    cpu, memory, _ = get_system_load()
    
    stats_text = f"""
📊 <b>Bot Statistics</b>

👥 Total Users: {user_count}
📁 Total Files: {file_count}
🚀 Running Files: {running_count}
⚡ CPU Usage: {cpu:.1f}%
💾 Memory Usage: {memory:.1f}%
    """
    bot.send_message(message.chat.id, stats_text)

@bot.message_handler(func=lambda m: m.text == "📁 My Files")
def my_files_handler(message):
    send_files_list(message.chat.id, message.from_user.id)

def send_files_list(chat_id, user_id):
    files = list_user_files(user_id)
    if not files:
        bot.send_message(chat_id, "📁 <b>Your Files</b>\n\nNo files uploaded yet.")
        return
    
    text = "📁 <b>Your Files</b>\n\nClick on a file to manage it:"
    kb = InlineKeyboardMarkup()
    
    for file in files:
        file_id, filename, orig_name, uploaded, file_type, status, pid = file
        emoji = "🟢" if status == "Running" else "🔴"
        button_text = f"{emoji} {orig_name} ({file_type})"
        kb.add(InlineKeyboardButton(button_text, callback_data=f"manage:{file_id}"))
    
    bot.send_message(chat_id, text, reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📤 Upload File")
def upload_handler(message):
    bot.send_message(message.chat.id, 
        "📤 <b>Upload a File</b>\n\n"
        "Send me a Python (.py), JavaScript (.js) file, or a ZIP archive.\n\n"
        "For ZIP files, I'll automatically:\n"
        "• Extract the archive\n"
        "• Find the main file\n"
        "• Install requirements.txt if present\n"
        "• Install missing imports\n"
        "• Start the script automatically"
    )

# File upload handler
@bot.message_handler(content_types=['document'])
def document_handler(message):
    user = message.from_user
    user_id = user.id
    
    # Check file limit
    user_files = list_user_files(user_id)
    if len(user_files) >= MAX_FILES_PER_USER:
        bot.reply_to(message, f"❌ You've reached the file limit ({MAX_FILES_PER_USER}). Delete some files to upload new ones.")
        return
    
    try:
        file_info = bot.get_file(message.document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to download file: {str(e)}")
        return
    
    original_filename = message.document.file_name or "unknown"
    file_type = get_file_type(original_filename)
    
    # Create user directory
    user_dir = os.path.join(UPLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    # Save file
    safe_filename = f"{int(time.time())}_{original_filename}"
    file_path = os.path.join(user_dir, safe_filename)
    
    try:
        with open(file_path, 'wb') as f:
            f.write(file_bytes)
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to save file: {str(e)}")
        return
    
    final_path = file_path
    extracted_dir = None
    
    # Handle archives
    if file_type in ["zip", "archive"]:
        bot.reply_to(message, "📦 Extracting archive...")
        extracted_dir = os.path.join(TEMP_DIR, f"extracted_{user_id}_{int(time.time())}")
        os.makedirs(extracted_dir, exist_ok=True)
        
        success, error = extract_archive(file_path, extracted_dir)
        if not success:
            bot.reply_to(message, f"❌ Failed to extract archive: {error}")
            try:
                os.remove(file_path)
            except:
                pass
            return
        
        # Find main file in the extracted directory
        main_file = find_main_file(extracted_dir)
        if not main_file:
            bot.reply_to(message, "❌ No main Python or JS file found in archive.")
            try:
                shutil.rmtree(extracted_dir, ignore_errors=True)
                os.remove(file_path)
            except:
                pass
            return
        
        final_path = extracted_dir
        file_type = get_file_type(main_file)
        bot.reply_to(message, f"✅ Found main file: {os.path.basename(main_file)}")
    
    # Add to database
    file_id = add_file_record(user_id, user.username, safe_filename, original_filename, final_path, file_type)
    
    # For single files, start automatically
    if file_type in ["python", "javascript"] and extracted_dir is None:
        bot.reply_to(message, f"✅ File uploaded! Starting automatically...")
        start_file_process(file_id, message.chat.id)
    else:
        bot.reply_to(message, f"✅ File uploaded! Use 'My Files' to manage it.")

# Process management functions
def start_file_process(file_id, chat_id):
    # Check system load
    should_stop, reason = should_stop_due_to_load()
    if should_stop:
        bot.send_message(chat_id, f"⚠️ Cannot start: {reason}")
        return
    
    file_record = get_file_record(file_id)
    if not file_record:
        bot.send_message(chat_id, "❌ File not found")
        return
    
    file_path = file_record["path"]
    original_name = file_record["orig_name"]
    file_type = file_record["file_type"]
    
    logger.info(f"Starting file process: {file_path}")
    
    # Determine what to run
    target_file = None
    working_dir = None
    
    if os.path.isdir(file_path):
        # Find main file in directory
        main_file = find_main_file(file_path)
        if not main_file:
            bot.send_message(chat_id, "❌ No main file found in directory")
            return
        target_file = main_file
        working_dir = os.path.dirname(main_file)
        logger.info(f"Running from directory. Main file: {target_file}, Working dir: {working_dir}")
    else:
        # Single file
        if not os.path.exists(file_path):
            bot.send_message(chat_id, f"❌ File not found: {file_path}")
            return
        target_file = file_path
        working_dir = os.path.dirname(file_path)
        logger.info(f"Running single file: {target_file}, Working dir: {working_dir}")
    
    # Check file extension
    ext = os.path.splitext(target_file)[1].lower()
    
    # Install requirements if present
    if ext == ".py":
        # Look for requirements.txt in working directory
        requirements_path = os.path.join(working_dir, "requirements.txt")
        if os.path.exists(requirements_path):
            bot.send_message(chat_id, "📦 Installing requirements.txt...")
            success, message = install_requirements_from_file(requirements_path, chat_id, original_name)
            if not success:
                bot.send_message(chat_id, f"⚠️ Requirements installation had issues: {message}")
            else:
                bot.send_message(chat_id, f"✅ Requirements installed: {message}")
        
        # Install missing imports
        bot.send_message(chat_id, "🔍 Checking for missing imports...")
        imports = extract_imports(target_file)
        if imports:
            success, message = install_missing_imports(imports, chat_id, original_name)
            bot.send_message(chat_id, f"📦 Import check: {message}")
    
    # Prepare command
    if ext == ".py":
        cmd = [sys.executable, target_file]
    elif ext == ".js":
        cmd = ["node", target_file]
    else:
        bot.send_message(chat_id, f"❌ Unsupported file type: {ext}")
        return
    
    # Create log file
    log_filename = f"file_{file_id}_{int(time.time())}.log"
    log_path = os.path.join(LOGS_DIR, log_filename)
    
    try:
        # Start process
        logger.info(f"Starting process: {cmd} in directory: {working_dir}")
        
        with open(log_path, 'w') as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=working_dir,
                text=True
            )
        
        # Record in database
        run_id = record_run_start(file_id, process.pid, log_path)
        update_file_status(file_id, process.pid, "Running")
        
        # Store in memory
        with proc_lock:
            processes[file_id] = {
                'process': process,
                'run_id': run_id,
                'log_path': log_path,
                'started_at': datetime.utcnow().isoformat()
            }
        
        bot.send_message(chat_id, 
            f"✅ <b>{html_lib.escape(original_name)}</b> started!\n"
            f"📝 PID: <code>{process.pid}</code>\n"
            f"📁 Logs: <code>{log_filename}</code>"
        )
        
        # Monitor process
        def monitor_process():
            try:
                exit_code = process.wait()
                logger.info(f"Process {process.pid} finished with exit code {exit_code}")
            except Exception as e:
                logger.error(f"Process monitoring error: {e}")
                exit_code = -1
            finally:
                # Update status
                update_file_status(file_id, None, "Stopped")
                record_run_finish(run_id, exit_code)
                with proc_lock:
                    processes.pop(file_id, None)
                
                if exit_code != 0:
                    try:
                        bot.send_message(chat_id, 
                            f"⚠️ <b>{html_lib.escape(original_name)}</b> stopped\n"
                            f"Exit code: {exit_code}"
                        )
                    except:
                        pass
        
        threading.Thread(target=monitor_process, daemon=True).start()
        
    except Exception as e:
        error_msg = f"❌ Failed to start process: {str(e)}"
        logger.error(error_msg)
        bot.send_message(chat_id, error_msg)

def stop_file_process(file_id):
    stopped = False
    with proc_lock:
        if file_id in processes:
            process_info = processes[file_id]
            process = process_info['process']
            
            try:
                if process.poll() is None:  # Process is still running
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    stopped = True
                    logger.info(f"Stopped process {file_id}")
            except Exception as e:
                logger.error(f"Error stopping process {file_id}: {e}")
            
            processes.pop(file_id, None)
    
    update_file_status(file_id, None, "Stopped")
    return stopped

def get_file_logs(file_id, lines=50):
    try:
        # Check running processes first
        with proc_lock:
            if file_id in processes:
                log_path = processes[file_id]['log_path']
                if os.path.exists(log_path):
                    with open(log_path, 'r') as f:
                        content = f.readlines()
                    return ''.join(content[-lines:]) if content else "No logs yet"
        
        # Check database for recent logs
        cur = conn.cursor()
        cur.execute(
            "SELECT log_path FROM runs WHERE file_id=? ORDER BY started_at DESC LIMIT 1",
            (file_id,)
        )
        row = cur.fetchone()
        if row and row[0] and os.path.exists(row[0]):
            with open(row[0], 'r') as f:
                content = f.readlines()
            return ''.join(content[-lines:]) if content else "No logs found"
        
        return "No log file found"
    except Exception as e:
        return f"Error reading logs: {str(e)}"

# Callback handlers
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    data = call.data
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    
    if data == "back_to_files":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        send_files_list(chat_id, user_id)
        return
    
    try:
        if data.startswith("manage:"):
            file_id = int(data.split(":")[1])
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("start:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Starting...")
            start_file_process(file_id, chat_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("stop:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Stopping...")
            stop_file_process(file_id)
            bot.send_message(chat_id, "⏹ Process stopped")
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("restart:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Restarting...")
            stop_file_process(file_id)
            time.sleep(2)
            start_file_process(file_id, chat_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("delete:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Deleting...")
            file_record = get_file_record(file_id)
            if file_record:
                # Stop if running
                stop_file_process(file_id)
                
                # Remove files from disk
                file_path = file_record["path"]
                try:
                    if os.path.isdir(file_path):
                        shutil.rmtree(file_path, ignore_errors=True)
                    elif os.path.exists(file_path):
                        os.remove(file_path)
                except Exception as e:
                    logger.error(f"Error deleting file {file_path}: {e}")
                
                # Remove from database
                remove_file_record(file_id)
            
            bot.send_message(chat_id, "🗑 File deleted")
            send_files_list(chat_id, user_id)
        
        elif data.startswith("logs:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Getting logs...")
            logs = get_file_logs(file_id)
            file_record = get_file_record(file_id)
            file_name = file_record["orig_name"] if file_record else "Unknown"
            
            if len(logs) > 4000:
                logs = logs[-4000:]
                logs = "... (truncated) ...\n" + logs
            
            log_text = f"📄 <b>Logs for {html_lib.escape(file_name)}</b>\n\n<pre>{html_lib.escape(logs)}</pre>"
            bot.send_message(chat_id, log_text)
    
    except Exception as e:
        bot.answer_callback_query(call.id, "Error processing request")
        logger.error(f"Callback error: {e}")

def show_file_management(chat_id, file_id, user_id, message_id=None):
    file_record = get_file_record(file_id)
    if not file_record:
        bot.send_message(chat_id, "❌ File not found")
        return
    
    if file_record["user_id"] != user_id:
        bot.send_message(chat_id, "❌ Access denied")
        return
    
    # Check if running
    is_running = False
    with proc_lock:
        is_running = file_id in processes
    
    status_text = "🟢 Running" if is_running else "🔴 Stopped"
    pid_text = f"\nPID: {file_record['pid']}" if file_record['pid'] else ""
    
    text = f"""
⚙️ <b>File Management</b>

📁 File: {html_lib.escape(file_record['orig_name'])}
📊 Type: {file_record['file_type']}
📈 Status: {status_text}{pid_text}
⏰ Uploaded: {file_record['uploaded_at'][:16]}
    """
    
    kb = file_actions_kb(file_id, is_running)
    
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        except:
            bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

# Start bot polling
def start_bot():
    logger.info("Starting Telegram bot...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=50)
        except Exception as e:
            logger.error(f"Bot polling error: {e}")
            time.sleep(5)

# Main execution
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Running 24/7 ✅"

def run_web():
    app.run(host="0.0.0.0", port=10000)

# Run both bot + web together
if __name__ == "__main__":
    t1 = threading.Thread(target=start_bot)
    t1.start()
    
    run_web()
