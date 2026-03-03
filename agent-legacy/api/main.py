"""Agent API server — FastAPI app on :8001."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent.api import chat, sessions
from agent.store import store


@asynccontextmanager
async def lifespan(app: FastAPI):
    # store._init_db() already called at import time
    app.state.store = store
    yield


app = FastAPI(title="Abax Agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(sessions.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
