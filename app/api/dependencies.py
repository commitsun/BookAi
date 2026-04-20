"""
FastAPI dependency functions shared across all routes.
"""

import socketio
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.instance import Instance
from app.repositories import instance_repo
from app.services.email_channel_client import EmailChannelClient
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_client import LLMProvider
from app.services.whatsapp_client import WhatsAppClient

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_instance(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Instance:
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    instance = await instance_repo.find_by_bearer_token(db, credentials.credentials)
    if instance is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not instance.bookai_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="BookAI is disabled for this instance",
        )
    return instance


def get_wa_client(request: Request) -> WhatsAppClient:
    return request.app.state.wa_client


def get_email_client(request: Request) -> EmailChannelClient:
    return request.app.state.email_client


def get_sio(request: Request) -> socketio.AsyncServer:
    return request.app.state.sio


def get_sdk_registry(request: Request) -> InstanceSDKRegistry:
    return request.app.state.sdk_registry


def get_llm_client(request: Request) -> LLMProvider:
    return request.app.state.llm_client
