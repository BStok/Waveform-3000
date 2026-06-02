import logging
import os
import random
import uuid
from datetime import datetime, timedelta
from functools import wraps

import jwt
import resend
from email_validator import EmailNotValidError, validate_email
from flask import jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from app.config import Config


logger = logging.getLogger(__name__)
SECRET_KEY = Config.SECRET_KEY


def send_otp_email(email, otp):
    resend.api_key = os.environ["RESEND_API_KEY"]
    resend.Emails.send({
        "from": os.environ.get("EMAIL_FROM", "onboarding@resend.dev"),
        "to": email,
        "subject": "Your Verification Code for Waveform SignUp",
        "html": f"""
        <h2>Your OTP Code</h2>
        <p>{otp}</p>
        <p>Expires in 10 minutes.</p>
        """,
    })


def generate_otp():
    return str(random.randint(100000, 999999))


def validate_user_email(email):
    try:
        return validate_email(email).email
    except EmailNotValidError:
        return None


def generate_token(user_id, email, expires_in_days=7):
    payload = {
        "user_id": str(user_id),
        "email": email,
        "exp": datetime.utcnow() + timedelta(days=expires_in_days),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def generate_password_setup_token(user_id, email, expires_in_minutes=15):
    payload = {
        "user_id": str(user_id),
        "email": email,
        "purpose": "set_password",
        "exp": datetime.utcnow() + timedelta(minutes=expires_in_minutes),
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
        logger.warning("Invalid password setup token: %s", e)
        return None


def verify_token(token):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        logger.warning("Token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("Invalid token: %s", e)
        return None


def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid authorization"}), 401

        payload = verify_token(auth_header[7:])
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401

        request.user_id = payload.get("user_id")
        request.email = payload.get("email")
        return f(*args, **kwargs)

    return decorated_function


def handle_login(data, get_db):
    email = data.get("email", "").strip().lower()
    password = data.get("password", "").strip()

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT user_id, password_hash, email_verified
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

            return generate_token(user_id, email), email, None
    except Exception as e:
        logger.error("Login error: %s", e, exc_info=True)
        return None, None, "Internal server error"


def handle_signup(data, get_db):
    email = validate_user_email(data.get("email", "").strip().lower())
    if not email:
        return None, "Invalid email"

    otp = generate_otp()
    otp_hash = generate_password_hash(otp)
    otp_expiry = datetime.utcnow() + timedelta(minutes=10)

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT user_id, email_verified, password_hash
                FROM users
                WHERE email = %s
            """, (email,))
            existing_user = cur.fetchone()

            if existing_user:
                user_id, _email_verified, password_hash = existing_user
                if password_hash:
                    return None, "Email already exists"

                cur.execute("""
                    UPDATE users
                    SET email_verified = FALSE,
                        otp_hash = %s,
                        otp_expiry = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s
                """, (otp_hash, otp_expiry, user_id))
            else:
                user_id = str(uuid.uuid4())
                cur.execute("""
                    INSERT INTO users (
                        user_id, email, auth_method, email_verified, otp_hash, otp_expiry
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, email, "password", False, otp_hash, otp_expiry))

        send_otp_email(email, otp)
        return {"otp_required": True, "email": email}, None
    except Exception as e:
        logger.error("Signup error: %s", e, exc_info=True)
        return None, "Internal server error"


def handle_verify_email(data, get_db):
    email = data.get("email", "").strip().lower()
    otp = data.get("otp", "").strip()

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT user_id, otp_hash, otp_expiry, email_verified
                FROM users
                WHERE email = %s
            """, (email,))
            user = cur.fetchone()

            if not user:
                return None, "User not found"

            user_id, otp_hash, otp_expiry, email_verified = user
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
                SET email_verified = TRUE,
                    otp_hash = NULL,
                    otp_expiry = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
            """, (user_id,))

            return {
                "verified": True,
                "email": email,
                "password_required": True,
                "setup_token": generate_password_setup_token(user_id, email),
            }, None
    except Exception as e:
        logger.error("Verify email error: %s", e, exc_info=True)
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

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT email_verified, password_hash
                FROM users
                WHERE user_id = %s AND email = %s
            """, (user_id, email))
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
                SET password_hash = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
            """, (generate_password_hash(password), user_id))

            return {"token": generate_token(user_id, email), "email": email}, None
    except Exception as e:
        logger.error("Set password error: %s", e, exc_info=True)
        return None, "Internal server error"
