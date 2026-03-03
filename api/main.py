"""
PharmaData API — FastAPI multi-utilisateur
==========================================

Lancer avec :
    uvicorn api.main:app --reload

Swagger UI disponible sur : http://localhost:8000/docs
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from api.config import settings
from api.database import Base, engine
from api.models import (  # noqa: F401 — nécessaire pour que SQLAlchemy découvre les tables
    Conversation,
    ExcelFile,
    Message,
    RefreshToken,
    User,
)
from api.routers import auth, conversations, files


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Créer les tables SQLAlchemy ─────────────────────────────────────────
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # ── Initialiser le checkpointer LangGraph (PostgreSQL) ──────────────────
    pool = AsyncConnectionPool(
        conninfo=settings.DATABASE_URL_SYNC,
        max_size=10,
        open=False,
        kwargs={"autocommit": True},
    )
    await pool.open()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()

    # ── Compiler le graph avec le checkpointer PostgreSQL ───────────────────
    from src.agent import create_graph

    app.state.api_graph = create_graph(checkpointer)

    yield

    # ── Nettoyage ────────────────────────────────────────────────────────────
    await pool.close()
    await engine.dispose()


app = FastAPI(
    title="PharmaData API",
    description="API REST multi-utilisateur pour l'assistant PharmaData",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(files.router)
app.include_router(conversations.router)


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}
