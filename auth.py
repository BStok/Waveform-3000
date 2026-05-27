"""
Authentication Module for WAVEFORM-3000
Handles login, signup, token validation
"""

import os
import jwt
import uuid
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from flask import request, jsonify
import logging

logger = logging.getLogger(__name__)

SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-dev-key-change-this")


def generate_token(user_id, username, expires_in_days=7):
    """Generate JWT token for user"""
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.utcnow() + timedelta(days=expires_in_days)
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    return token


def verify_token(token):
    """
    Verify JWT token and return payload.
    Returns None if invalid or expired.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning(f"Token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid token: {e}")
        return None


def require_auth(f):
    """Decorator to require valid JWT in Authorization header"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid authorization"}), 401
        
        token = auth_header[7:]  # Remove "Bearer " prefix
        payload = verify_token(token)
        
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401
        
        # Attach user_id to request context
        request.user_id = payload.get("user_id")
        request.username = payload.get("username")
        
        return f(*args, **kwargs)
    
    return decorated_function


# ============ AUTH HANDLERS ============

def handle_login(data, get_db):
    """
    Login with username + password.
    Returns (token, username, error_msg) tuple.
    """
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    
    if not username or not password:
        return None, None, "Missing username or password"
    
    try:
        with get_db() as conn:
            cur = conn.cursor()
            
            # Query user by username
            cur.execute("""
                SELECT user_id, username, password_hash, auth_method
                FROM users
                WHERE username = %s
            """, (username,))
            
            user = cur.fetchone()
            
            if not user:
                return None, None, "Invalid credentials"
            
            user_id, db_username, password_hash, auth_method = user
            
            # Check password
            if not check_password_hash(password_hash, password):
                return None, None, "Invalid credentials"
            
            # Generate token
            token = generate_token(str(user_id), db_username)
            
            logger.info(f"✓ Login successful: {username}")
            return token, db_username, None
    
    except Exception as e:
        logger.error(f"Login error: {e}", exc_info=True)
        return None, None, "Internal server error"


def handle_signup(data, get_db):
    """
    Signup with username + password.
    Returns (token, username, error_msg) tuple.
    """
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    
    # Validation
    if len(username) < 3:
        return None, None, "Username must be at least 3 characters"
    
    if len(password) < 6:
        return None, None, "Password must be at least 6 characters"
    
    try:
        user_id = str(uuid.uuid4())
        password_hash = generate_password_hash(password)
        
        with get_db() as conn:
            cur = conn.cursor()
            
            # Check if username exists
            cur.execute("SELECT user_id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                return None, None, "Username already exists"
            
            # Insert user
            cur.execute("""
                INSERT INTO users 
                (user_id, username, password_hash, auth_method)
                VALUES (%s, %s, %s, %s)
            """, (user_id, username, password_hash, "password"))
        
        # Generate token
        token = generate_token(user_id, username)
        
        logger.info(f"✓ Signup successful: {username}")
        return token, username, None
    
    except Exception as e:
        logger.error(f"Signup error: {e}", exc_info=True)
        return None, None, "Internal server error"


def handle_google_login(google_token_payload, get_db):
    """
    Handle Google OAuth login.
    Creates user if doesn't exist, or logs in existing.
    Returns (token, username, error_msg) tuple.
    """
    google_id = google_token_payload.get("sub")
    email = google_token_payload.get("email")
    name = google_token_payload.get("name", "User")
    
    if not google_id or not email:
        return None, None, "Invalid Google token"
    
    try:
        with get_db() as conn:
            cur = conn.cursor()
            
            # Check if user exists
            cur.execute("""
                SELECT user_id, username
                FROM users
                WHERE google_id = %s
            """, (google_id,))
            
            user = cur.fetchone()
            
            if user:
                # Existing Google user - login
                user_id, username = user
                token = generate_token(str(user_id), username)
                logger.info(f"✓ Google login: {email}")
                return token, username, None
            
            # New Google user - create account
            user_id = str(uuid.uuid4())
            # Use email prefix as username, ensure uniqueness
            username = email.split("@")[0]
            
            # Make username unique if needed
            counter = 1
            base_username = username
            while True:
                cur.execute("SELECT user_id FROM users WHERE username = %s", (username,))
                if not cur.fetchone():
                    break
                username = f"{base_username}{counter}"
                counter += 1
            
            # Create user with Google auth
            cur.execute("""
                INSERT INTO users
                (user_id, username, email, google_id, auth_method, password_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (user_id, username, email, google_id, "google", ""))
            
            token = generate_token(user_id, username)
            logger.info(f"✓ Google signup: {email} -> {username}")
            return token, username, None
    
    except Exception as e:
        logger.error(f"Google login error: {e}", exc_info=True)
        return None, None, "Internal server error"