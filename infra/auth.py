"""Authentication dependency for FastAPI — supports JWT and API key."""

import os
import time

import jwt
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(auto_error=False)

JWT_SECRET = os.getenv("ABAX_JWT_SECRET", "")
JWT_ALGORITHM = "HS256"


def create_jwt(user_id: str, expires_hours: int = 24) -> str:
    """Create a signed JWT token for the given user_id."""
    if not JWT_SECRET:
        raise ValueError("ABAX_JWT_SECRET not set — cannot create JWT tokens")
    now = int(time.time())
    payload = {
        "sub": user_id,
        "exp": now + expires_hours * 3600,
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict | None:
    """Decode and verify a JWT token. Returns payload dict or None on failure."""
    if not JWT_SECRET:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> str | None:
    """Verify the Bearer token as JWT or API key.

    Returns user_id if JWT is valid, None otherwise (API key or dev mode).
    If ABAX_API_KEY is not set and ABAX_JWT_SECRET is not set, all requests
    are allowed (dev mode).
    """
    api_key = os.getenv("ABAX_API_KEY")
    token = credentials.credentials if credentials else None

    # Try JWT first
    if token and JWT_SECRET:
        payload = decode_jwt(token)
        if payload:
            return payload.get("sub")

    # Fall back to API key
    if not api_key:
        return None  # Dev mode: no auth required

    if token == api_key:
        return None  # API key valid but no user_id extracted

    raise HTTPException(status_code=401, detail="Invalid or missing credentials")
