"""
Authentication Module for WAVEFORM-3000
Handles login, signup, token validation
"""

import os
import jwt
import uuid
from functools import wraps
from flask import request, jsonify
import logging
import random
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

from services.email_service import send_otp_email
from email_validator import validate_email, EmailNotValidError

logger = logging.getLogger(__name__)

SECRET_KEY = os.environ["SECRET_KEY"]

## new fucntiosn
def generate_otp():
    return str(random.randint(100000, 999999))


def validate_user_email(email):
    try:
        valid = validate_email(email)
        return valid.email
    except EmailNotValidError:
        return None

##old fucntions
def generate_token(user_id, email, expires_in_days=7):
    """Generate JWT token for user"""
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.utcnow() + timedelta(days=expires_in_days)
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
    return token


def generate_password_setup_token(user_id, email, expires_in_minutes=15):
    payload = {
        "user_id": str(user_id),
        "email": email,
        "purpose": "set_password",
        "exp": datetime.utcnow() + timedelta(minutes=expires_in_minutes)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def verify_password_setup_token(token):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if payload.get("purpose") != "set_password":
            return None
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Password setup token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid password setup token: {e}")
        return None


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
        request.email = payload.get("email")
        
        return f(*args, **kwargs)
    
    return decorated_function


# ============ AUTH HANDLERS ============
# new login 

def handle_login(data, get_db):

    email = data.get("email", "").strip().lower()

    password = data.get("password", "").strip()

    try:
        with get_db() as conn:

            cur = conn.cursor()

            cur.execute("""
                SELECT
                    user_id,
                    password_hash,
                    email_verified
                FROM users
                WHERE email = %s
            """, (email,))

            user = cur.fetchone()

            if not user:
                return None, None, "Invalid credentials"

            user_id, password_hash, verified = user

            if not verified:
                return None, None, "Verify email first"
            
            if not password_hash:
                return None, None, "Password not set"

            if not check_password_hash(password_hash, password):
                return None, None, "Invalid credentials"

            token = generate_token(user_id, email)

            return token, email, None

    except Exception as e:
        logger.error(e, exc_info=True)
        return None, None, "Internal server error"
    
def handle_forgot_password(data, get_db):

    email = data.get("email", "").strip().lower()

    otp = generate_otp()

    otp_hash = generate_password_hash(otp)

    expiry = datetime.utcnow() + timedelta(minutes=10)

    try:
        with get_db() as conn:

            cur = conn.cursor()

            cur.execute("""
                UPDATE users
                SET
                    reset_otp_hash = %s,
                    reset_otp_expiry = %s
                WHERE email = %s
            """, (
                otp_hash,
                expiry,
                email
            ))

        send_otp_email(email, otp)

        return True, None

    except Exception as e:
        logger.error(e, exc_info=True)

        return False, "Internal server error"
    

##NEW SIGNUP



def handle_signup(data, get_db):

    email = validate_user_email(
        data.get("email", "").strip().lower()
    )

    if not email:
        return None, "Invalid email"

    otp = generate_otp()

    otp_hash = generate_password_hash(otp)

    otp_expiry = datetime.utcnow() + timedelta(minutes=10)

    try:
        with get_db() as conn:
            cur = conn.cursor()

            cur.execute("""
                SELECT
                    user_id,
                    email_verified,
                    password_hash
                FROM users
                WHERE email = %s
            """, (email,))

            existing_user = cur.fetchone()

            if existing_user:
                user_id, email_verified, password_hash = existing_user

                if password_hash:
                    return None, "Email already exists"

                cur.execute("""
                    UPDATE users
                    SET
                        email_verified = FALSE,
                        otp_hash = %s,
                        otp_expiry = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s
                """, (
                    otp_hash,
                    otp_expiry,
                    user_id
                ))
            else:
                user_id = str(uuid.uuid4())

                cur.execute("""
                    INSERT INTO users (
                        user_id,
                        email,
                        auth_method,
                        email_verified,
                        otp_hash,
                        otp_expiry
                    )
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (
                    user_id,
                    email,
                    "password",
                    False,
                    otp_hash,
                    otp_expiry
                ))

        send_otp_email(email, otp)

        return {
            "otp_required": True,
            "email": email
        }, None

    except Exception as e:
        logger.error(e, exc_info=True)
        return None, "Internal server error"

def handle_verify_email(data, get_db):

    email = data.get("email", "").strip().lower()

    otp = data.get("otp", "").strip()

    try:
        with get_db() as conn:

            cur = conn.cursor()

            cur.execute("""
                SELECT
                    user_id,
                    otp_hash,
                    otp_expiry,
                    email_verified
                FROM users
                WHERE email = %s
            """, (email,))

            user = cur.fetchone()

            if not user:
                return None, "User not found"

            (
                user_id,
                otp_hash,
                otp_expiry,
                email_verified
            ) = user

            if email_verified:
                return None, "Already verified"

            if not otp_hash or not otp_expiry:
                return None, "OTP not found"

            if datetime.utcnow() > otp_expiry:
                return None, "OTP expired"

            if not check_password_hash(otp_hash, otp):
                return None, "Invalid OTP"

            cur.execute("""
                UPDATE users
                SET
                    email_verified = TRUE,
                    otp_hash = NULL,
                    otp_expiry = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
            """, (user_id,))

            return {
                "verified": True,
                "email": email,
                "password_required": True,
                "setup_token": generate_password_setup_token(user_id, email)
            }, None

    except Exception as e:
        logger.error(e, exc_info=True)
        return None, "Internal server error"

def handle_set_password(data, get_db):

    setup_token = data.get("setup_token", "").strip()

    token_payload = verify_password_setup_token(setup_token)

    if not token_payload:
        return None, "Invalid or expired password setup token"

    email = token_payload.get("email", "").strip().lower()

    user_id = token_payload.get("user_id")

    password = data.get("password", "").strip()

    if len(password) < 6:
        return None, "Password too short"

    password_hash = generate_password_hash(password)

    try:
        with get_db() as conn:

            cur = conn.cursor()

            cur.execute("""
                SELECT
                    email_verified,
                    password_hash
                FROM users
                WHERE user_id = %s
                    AND email = %s
            """, (user_id, email,))

            user = cur.fetchone()

            if not user:
                return None, "User not found"

            email_verified, existing_password_hash = user

            if not email_verified:
                return None, "Verify email first"

            if existing_password_hash:
                return None, "Password already set"

            cur.execute("""
                UPDATE users
                SET
                    password_hash = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
            """, (
                password_hash,
                user_id
            ))

            token = generate_token(user_id, email)

            return {
                "token": token,
                "email": email
            }, None

    except Exception as e:
        logger.error(e, exc_info=True)
        return None, "Internal server error"

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
                token = generate_token(str(user_id), email)
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
            
            token = generate_token(user_id, email)
            logger.info(f"✓ Google signup: {email} -> {username}")
            return token, username, None
    
    except Exception as e:
        logger.error(f"Google login error: {e}", exc_info=True)
        return None, None, "Internal server error"


    
    
# def handle_signup(data, get_db):
#     """
#     Signup with username + password.
#     Returns (token, username, error_msg) tuple.
#     """
#     username = data.get("username", "").strip()
#     password = data.get("password", "").strip()
    
#     # Validation
#     if len(username) < 3:
#         return None, None, "Username must be at least 3 characters"
    
#     if len(password) < 6:
#         return None, None, "Password must be at least 6 characters"
    
#     try:
#         user_id = str(uuid.uuid4())
#         password_hash = generate_password_hash(password)
        
#         with get_db() as conn:
#             cur = conn.cursor()
            
#             # Check if username exists
#             cur.execute("SELECT user_id FROM users WHERE username = %s", (username,))
#             if cur.fetchone():
#                 return None, None, "Username already exists"
            
#             # Insert user
#             cur.execute("""
#                 INSERT INTO users 
#                 (user_id, username, password_hash, auth_method)
#                 VALUES (%s, %s, %s, %s)
#             """, (user_id, username, password_hash, "password"))
        
#         # Generate token
#         token = generate_token(user_id, username)
        
#         logger.info(f"✓ Signup successful: {username}")
#         return token, username, None
    
#     except Exception as e:
#         logger.error(f"Signup error: {e}", exc_info=True)
#         return None, None, "Internal server error"
# """
    

# old login
# def handle_login(data, get_db):
#     """
#     Login with username + password.
#     Returns (token, username, error_msg) tuple.
#     """
#     username = data.get("username", "").strip()
#     password = data.get("password", "").strip()
    
#     if not username or not password:
#         return None, None, "Missing username or password"
    
#     try:
#         with get_db() as conn:
#             cur = conn.cursor()
            
#             # Query user by username
#             cur.execute("""
#                 SELECT user_id, username, password_hash, auth_method
#                 FROM users
#                 WHERE username = %s
#             """, (username,))
            
#             user = cur.fetchone()
            
#             if not user:
#                 return None, None, "Invalid credentials"
            
#             user_id, db_username, password_hash, auth_method = user
            
#             # Check password
#             if not check_password_hash(password_hash, password):
#                 return None, None, "Invalid credentials"
            
#             # Generate token
#             token = generate_token(str(user_id), db_username)
            
#             logger.info(f"✓ Login successful: {username}")
#             return token, db_username, None
    
#     except Exception as e:
#         logger.error(f"Login error: {e}", exc_info=True)
#         return None, None, "Internal server erro
