"""API Key authentication dependency for FastAPI."""

import os

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(auto_error=False)


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> None:
    """Verify the Bearer token matches ABAX_API_KEY.

    If ABAX_API_KEY is not set, all requests are allowed (dev mode).
    """
    api_key = os.getenv("ABAX_API_KEY")
    if not api_key:
        return  # Dev mode: no auth required
    if credentials is None or credentials.credentials != api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
