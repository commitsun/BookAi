"""
FastAPI dependency functions shared across all routes.
"""

import socketio
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.instance import Instance, Property
from app.repositories import instance_repo
from app.services.email_channel_client import EmailChannelClient
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_client import LLMProvider
from app.services.mcp_manager import MCPManager
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


async def resolve_property(
    odoo_property_id: int,
    instance: Instance,
    db: AsyncSession,
) -> Property | None:
    """Resolve an external (Odoo) property ID to the internal Property.

    The external API always receives Odoo property IDs from clients.
    This function translates them to the internal Property object.

    Returns None for odoo_property_id=0 (unrouted inbox).
    Raises 404 if the odoo_property_id is not found in the instance.
    """
    if odoo_property_id == 0:
        return None
    prop = await instance_repo.find_property_by_odoo_property_id(
        db, odoo_property_id, instance.id,
    )
    if prop is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property with property_id={odoo_property_id} not found for this instance",
        )
    return prop


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


def get_mcp_manager(request: Request) -> MCPManager:
    return request.app.state.mcp_manager
