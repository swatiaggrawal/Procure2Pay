"""Passwords and session tokens."""
import hashlib
import os
import secrets
import time

import bcrypt

ROUNDS = int(os.getenv("BCRYPT_ROUNDS", "10"))
SESSION_HOURS = float(os.getenv("SESSION_HOURS", "12"))   # absolute lifetime
IDLE_HOURS = float(os.getenv("IDLE_HOURS", "4"))          # logged out after this long without a request
MIN_PASSWORD = 8


def hash_pw(p: str) -> str:
    return bcrypt.hashpw(p.encode()[:72], bcrypt.gensalt(rounds=ROUNDS)).decode()


def check_pw(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode()[:72], h.encode())
    except ValueError:
        return False


_DUMMY = None


def burn_time(p: str):
    """Unknown usernames still cost one bcrypt check, so response time does not reveal which usernames exist."""
    global _DUMMY
    _DUMMY = _DUMMY or hash_pw("not-a-real-password")
    check_pw(p, _DUMMY)


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def new_token():
    t = secrets.token_urlsafe(32)
    return t, hash_token(t)
