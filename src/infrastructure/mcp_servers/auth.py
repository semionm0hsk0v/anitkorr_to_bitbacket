import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta

import httpx

from src.common.config.config import settings


class KeycloakAuth(httpx.Auth):
    def __init__(self, server_url: str, realm: str, client_id: str, client_secret: str):
        self.server_url = server_url
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: str | None = None
        self.token_expires_at: datetime | None = None
        self._client = httpx.AsyncClient(verify=False)
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        access_token = await self._get_valid_token()
        request.headers["Authorization"] = f"Bearer {access_token}"
        response = yield request

        if response.status_code == 401:
            async with self._lock:
                access_token = await self._refresh_token()
                request.headers["Authorization"] = f"Bearer {access_token}"
                yield request

    async def _get_valid_token(self) -> str:
        async with self._lock:
            if (
                self.access_token is None
                or self.token_expires_at is None
                or datetime.now() + timedelta(seconds=30) >= self.token_expires_at
            ):
                return await self._refresh_token()
            return self.access_token

    async def _refresh_token(self) -> str:
        token_url = f"{self.server_url}/realms/{self.realm}/protocol/openid-connect/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        response = await self._client.post(
            token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data,
        )
        response.raise_for_status()
        token_data = response.json()

        access_token_raw = token_data.get("access_token")
        if not isinstance(access_token_raw, str) or not access_token_raw:
            raise ValueError("Keycloak token response does not contain a valid access_token")
        self.access_token = access_token_raw
        expires_in = token_data.get("expires_in", 300)
        self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
        return access_token_raw

    async def aclose(self):
        await self._client.aclose()


keycloak_auth: KeycloakAuth = KeycloakAuth(
    server_url=settings.get("KEYCLOAK_URL", ""),
    client_id=settings.get("KEYCLOAK_CLIENT_ID", "mcp-client"),
    realm=settings.get("KEYCLOAK_REALM", ""),
    client_secret=settings.get("KEYCLOAK_CLIENT_SECRET", ""),
)
