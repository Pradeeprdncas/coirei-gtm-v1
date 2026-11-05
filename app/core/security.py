import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.db import get_db
from app.models.entities import User

bearer = HTTPBearer(auto_error=False)


def password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f'scrypt$16384$8$1${salt.hex()}${digest.hex()}'


def password_check(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split('$')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


def make_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({'sub': str(user_id), 'iat': now, 'exp': now + timedelta(minutes=settings().jwt_expire_minutes)},
                      settings().jwt_secret, algorithm='HS256')


async def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
                       db: AsyncSession = Depends(get_db)) -> User:
    if credentials is None:
        raise HTTPException(401, detail={'code': 'UNAUTHORIZED', 'message': 'Sign in required'})
    try:
        decoded = jwt.decode(credentials.credentials, settings().jwt_secret, algorithms=['HS256'])
        user_id = int(decoded['sub'])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, detail={'code': 'UNAUTHORIZED', 'message': 'Invalid or expired access token'})
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(401, detail={'code': 'UNAUTHORIZED', 'message': 'User not found'})
    return user
