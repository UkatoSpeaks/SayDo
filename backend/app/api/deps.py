from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.db import get_session
from app.services.embeddings import EmbeddingProvider
from app.services.ingestion import ProcessingTracker
from app.services.llm import LLMProvider
from app.services.storage import PdfStorage


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_storage(request: Request) -> PdfStorage:
    storage: PdfStorage = request.app.state.storage
    return storage


def get_embedder(request: Request) -> EmbeddingProvider:
    embedder: EmbeddingProvider = request.app.state.embedder
    return embedder


def get_llm(request: Request) -> LLMProvider:
    llm: LLMProvider = request.app.state.llm
    return llm


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return sessionmaker


def get_tracker(request: Request) -> ProcessingTracker:
    tracker: ProcessingTracker = request.app.state.processing
    return tracker


def get_verify_tracker(request: Request) -> ProcessingTracker:
    tracker: ProcessingTracker = request.app.state.verifying
    return tracker


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
StorageDep = Annotated[PdfStorage, Depends(get_storage)]
EmbedderDep = Annotated[EmbeddingProvider, Depends(get_embedder)]
SessionmakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
TrackerDep = Annotated[ProcessingTracker, Depends(get_tracker)]
VerifyTrackerDep = Annotated[ProcessingTracker, Depends(get_verify_tracker)]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
