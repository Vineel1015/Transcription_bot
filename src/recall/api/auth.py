"""REST authentication: Entra ID JWT bearer tokens; SERVICE_TOKEN for automation (smoke tests); DEV_AUTH=true
allows a static token locally (optionally acting as the user in X-Dev-User)."""

from __future__ import annotations

import asyncio
import secrets
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select

from recall.config import get_settings
from recall.db.models import Person
from recall.db.session import session_scope
from recall.query.auth import Principal, load_principal

_jwks: dict[str, jwt.PyJWKClient] = {}


def _jwks_client(tenant: str) -> jwt.PyJWKClient:
    if tenant not in _jwks:
        _jwks[tenant] = jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys")
    return _jwks[tenant]


async def _principal_for(person: Person | None) -> Principal:
    if person is None:
        raise HTTPException(403, "your account is not known to Recall yet")
    async with session_scope() as session:
        return await load_principal(session, person)


async def current_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_dev_user: Annotated[str | None, Header()] = None,
) -> Principal:
    s = get_settings()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    token = authorization.split(" ", 1)[1].strip()
    if s.service_token and secrets.compare_digest(token, s.service_token):
        return Principal.service()
    if s.dev_auth and secrets.compare_digest(token, s.dev_token):
        async with session_scope() as session:
            if x_dev_user:
                person = await session.scalar(select(Person).where(Person.email == x_dev_user.lower()))
                if person is None:
                    raise HTTPException(403, f"no person with email {x_dev_user}")
                return await load_principal(session, person)
        return Principal.service()
    if not (s.graph_tenant_id and s.entra_api_audience):
        raise HTTPException(401, "Entra authentication is not configured")
    try:
        key = await asyncio.to_thread(_jwks_client(s.graph_tenant_id).get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=s.entra_api_audience,
            issuer=[
                f"https://login.microsoftonline.com/{s.graph_tenant_id}/v2.0",
                f"https://sts.windows.net/{s.graph_tenant_id}/",
            ],
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"invalid token: {exc}") from exc
    async with session_scope() as session:
        person = await session.scalar(select(Person).where(Person.entra_id == claims.get("oid")))
    return await _principal_for(person)


async def require_admin(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if not principal.is_admin:
        raise HTTPException(403, "admin only")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
AdminPrincipal = Annotated[Principal, Depends(require_admin)]
