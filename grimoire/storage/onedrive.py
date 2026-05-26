"""OneDrive storage adapter using Microsoft Graph API.

This module provides an implementation of the StorageAdapter interface
for Microsoft OneDrive using the Microsoft Graph API.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, List, Optional, cast
from urllib.parse import urljoin

import httpx
from loguru import logger

from grimoire.config.settings import CloudOnedriveConfig
from grimoire.storage.base import (
    FileChange,
    FileChangeType,
    FileInfo,
    FileMetadata,
    StorageAdapter,
    WatchHandle,
)


@dataclass
class OneDriveTokenData:
    """OAuth2 token data for OneDrive."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str = "openid offline_access Files.Read"
    token_type: str = "Bearer"

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        """Check if the token is expired or will expire soon."""
        return datetime.now() + timedelta(seconds=buffer_seconds) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
            "scope": self.scope,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OneDriveTokenData:
        """Create from dictionary."""
        expires_at_str = data.get("expires_at", "")
        expires_at = (
            datetime.fromisoformat(expires_at_str)
            if expires_at_str
            else datetime.now()
        )
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_at=expires_at,
            scope=data.get("scope", "openid offline_access Files.Read"),
            token_type=data.get("token_type", "Bearer"),
        )


class OneDriveError(Exception):
    """Base exception for OneDrive errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class OneDriveAuthError(OneDriveError):
    """Authentication error for OneDrive."""

    pass


class OneDriveRateLimitError(OneDriveError):
    """Rate limit exceeded error."""

    def __init__(
        self, message: str, status_code: int = 429, retry_after: int = 60
    ) -> None:
        super().__init__(message, status_code)
        self.retry_after = retry_after


class OneDriveAdapter(StorageAdapter):
    """OneDrive storage adapter using Microsoft Graph API."""

    GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
    AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    DEFAULT_SCOPES = ["openid", "offline_access", "Files.Read", "User.Read"]

    def __init__(self, config: CloudOnedriveConfig) -> None:
        """Initialize the OneDrive adapter."""
        self.config = config
        self.token_data: OneDriveTokenData | None = None
        self.http_client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Accept": "application/json"},
        )
        self._delta_tokens: dict[str, str] = {}
        self._load_tokens()

    def _get_token_path(self) -> Path:
        """Get the path to the token store file."""
        return Path(os.path.expanduser(self.config.token_store))

    def _load_tokens(self) -> None:
        """Load tokens from the token store file, decrypting if necessary."""
        token_path = self._get_token_path()
        if token_path.exists():
            try:
                raw = token_path.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    from grimoire.utils.token_crypto import decrypt_tokens
                    data = decrypt_tokens(raw)
                self.token_data = OneDriveTokenData.from_dict(data)
                logger.debug(f"Loaded OneDrive tokens from {token_path}")
            except Exception as e:
                logger.warning(f"Failed to load OneDrive tokens: {e}")
                self.token_data = None
        else:
            logger.debug(f"OneDrive token store not found at {token_path}")
            self.token_data = None

    def _save_tokens(self) -> None:
        """Save tokens to the token store file, encrypting when possible."""
        if self.token_data is None:
            return

        token_path = self._get_token_path()
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            encrypted = False
            try:
                from grimoire.utils.token_crypto import encrypt_tokens, TokenCryptoError
                payload = encrypt_tokens(self.token_data.to_dict())
                encrypted = True
            except TokenCryptoError:
                logger.warning(
                    "Token encryption unavailable (cryptography not installed?). "
                    "Saving tokens as plain JSON — install cryptography to secure tokens at rest."
                )
                payload = json.dumps(self.token_data.to_dict(), indent=2)
            token_path.write_text(payload, encoding="utf-8")
            os.chmod(token_path, 0o600)
            logger.debug(f"Saved OneDrive tokens to {token_path} (encrypted={encrypted})")
        except OSError as e:
            logger.error(f"Failed to save OneDrive tokens: {e}")

    async def _ensure_token_valid(self) -> str:
        """Ensure the access token is valid, refreshing if necessary."""
        if self.token_data is None:
            raise OneDriveAuthError(
                "Not authenticated. Please authenticate first using authenticate()."
            )

        if self.token_data.is_expired():
            logger.info("OneDrive access token expired, refreshing...")
            await self._refresh_token()

        return self.token_data.access_token

    async def _refresh_token(self) -> None:
        """Refresh the OAuth2 access token."""
        if self.token_data is None or not self.token_data.refresh_token:
            raise OneDriveAuthError("No refresh token available.")

        if not self.config.client_id or not self.config.client_secret:
            raise OneDriveAuthError("Client ID and secret required.")

        data = {
            "grant_type": "refresh_token",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "refresh_token": self.token_data.refresh_token,
            "scope": " ".join(self.DEFAULT_SCOPES),
        }

        try:
            response = await self.http_client.post(self.TOKEN_URL, data=data)
            response.raise_for_status()
            token_response = response.json()

            self.token_data = OneDriveTokenData(
                access_token=token_response["access_token"],
                refresh_token=token_response.get(
                    "refresh_token", self.token_data.refresh_token
                ),
                expires_at=datetime.now()
                + timedelta(seconds=token_response.get("expires_in", 3600)),
                scope=token_response.get("scope", " ".join(self.DEFAULT_SCOPES)),
                token_type=token_response.get("token_type", "Bearer"),
            )
            self._save_tokens()
            logger.info("Successfully refreshed OneDrive access token")

        except httpx.HTTPStatusError as e:
            error_msg = f"Token refresh failed: {e.response.status_code}"
            raise OneDriveAuthError(error_msg, e.response.status_code)
        except httpx.NetworkError as e:
            raise OneDriveAuthError(f"Network error during token refresh: {e}")

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an authenticated request to the Microsoft Graph API."""
        access_token = await self._ensure_token_valid()

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {access_token}"

        url = urljoin(self.GRAPH_BASE_URL + "/", endpoint.lstrip("/"))

        try:
            response = await self.http_client.request(
                method, url, headers=headers, **kwargs
            )

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                raise OneDriveRateLimitError(
                    "Rate limit exceeded", retry_after=retry_after
                )

            if response.status_code == 401:
                if self.token_data and not self.token_data.is_expired(
                    buffer_seconds=0
                ):
                    await self._refresh_token()
                    access_token = self.token_data.access_token
                    headers["Authorization"] = f"Bearer {access_token}"
                    response = await self.http_client.request(
                        method, url, headers=headers, **kwargs
                    )

            response.raise_for_status()
            return cast(dict[str, Any], response.json())

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            error_msg = f"API request failed: {status_code}"
            if status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", 60))
                raise OneDriveRateLimitError(error_msg, status_code, retry_after)
            elif status_code == 401:
                raise OneDriveAuthError(error_msg, status_code)
            elif status_code == 403:
                raise PermissionError(f"Access denied: {error_msg}")
            else:
                raise OneDriveError(error_msg, status_code)
        except httpx.NetworkError as e:
            raise OneDriveError(f"Network error: {e}")
        except httpx.TimeoutException as e:
            raise OneDriveError(f"Request timeout: {e}")

    def _parse_odt_datetime(self, odt_string: str | None) -> datetime:
        """Parse OData datetime string to datetime object."""
        if not odt_string:
            return datetime.now()
        try:
            return datetime.fromisoformat(odt_string.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now()

    def _drive_item_to_file_info(self, item: dict[str, Any]) -> FileInfo:
        """Convert Microsoft Graph drive item to FileInfo."""
        name = item.get("name", "")
        path = item.get("parentReference", {}).get("path", "")
        full_path = f"{path}/{name}" if path else name

        if full_path.startswith("/drive/root:"):
            full_path = full_path[12:]
        if full_path.startswith(":"):
            full_path = full_path[1:]

        size = item.get("size", 0)
        if isinstance(size, str):
            size = int(size)

        modified = self._parse_odt_datetime(item.get("lastModifiedDateTime"))
        is_folder = "folder" in item
        mime_type = item.get("file", {}).get("mimeType")

        return FileInfo(
            path=full_path,
            name=name,
            size_bytes=size,
            modified_at=modified,
            is_directory=is_folder,
            mime_type=mime_type,
            metadata={
                "id": item.get("id"),
                "e_tag": item.get("eTag"),
                "c_tag": item.get("cTag"),
                "web_url": item.get("webUrl"),
            },
        )

    async def authenticate(self, auth_code: str, redirect_uri: str) -> None:
        """Authenticate with OneDrive using OAuth2 authorization code."""
        if not self.config.client_id or not self.config.client_secret:
            raise OneDriveAuthError("Client ID and secret required.")

        data = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": auth_code,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.DEFAULT_SCOPES),
        }

        try:
            response = await self.http_client.post(self.TOKEN_URL, data=data)
            response.raise_for_status()
            token_response = response.json()

            self.token_data = OneDriveTokenData(
                access_token=token_response["access_token"],
                refresh_token=token_response["refresh_token"],
                expires_at=datetime.now()
                + timedelta(seconds=token_response.get("expires_in", 3600)),
                scope=token_response.get("scope", " ".join(self.DEFAULT_SCOPES)),
                token_type=token_response.get("token_type", "Bearer"),
            )
            self._save_tokens()
            logger.info("Successfully authenticated with OneDrive")

        except httpx.HTTPStatusError as e:
            error_msg = f"Authentication failed: {e.response.status_code}"
            raise OneDriveAuthError(error_msg, e.response.status_code)
        except httpx.NetworkError as e:
            raise OneDriveAuthError(f"Network error during authentication: {e}")

    def get_auth_url(self, redirect_uri: str, state: str | None = None) -> str:
        """Get the OAuth2 authorization URL."""
        if not self.config.client_id:
            raise OneDriveAuthError("Client ID required.")

        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.DEFAULT_SCOPES),
            "response_mode": "query",
        }
        if state:
            params["state"] = state

        query = httpx.QueryParams(params)
        return f"{self.AUTH_URL}?{query}"

    async def list_files(
        self, path: str, recursive: bool = False
    ) -> List[FileInfo]:
        """List files in a OneDrive directory."""
        await self._ensure_token_valid()

        onedrive_path = path if path.startswith("/") else f"/{path}"
        if onedrive_path == "/":
            onedrive_path = ""

        files: list[FileInfo] = []

        if recursive:
            endpoint = (
                f"/me/drive/root:{onedrive_path}:/delta"
                if onedrive_path
                else "/me/drive/root/delta"
            )

            while endpoint:
                data = await self._make_request("GET", endpoint)
                items = data.get("value", [])

                for item in items:
                    if "file" in item:
                        files.append(self._drive_item_to_file_info(item))

                endpoint = data.get("@odata.nextLink", "")
                if endpoint:
                    endpoint = endpoint.replace(self.GRAPH_BASE_URL, "")
        else:
            endpoint = (
                f"/me/drive/root:{onedrive_path}:/children"
                if onedrive_path
                else "/me/drive/root/children"
            )

            while endpoint:
                data = await self._make_request("GET", endpoint)
                items = data.get("value", [])

                for item in items:
                    files.append(self._drive_item_to_file_info(item))

                endpoint = data.get("@odata.nextLink", "")
                if endpoint:
                    endpoint = endpoint.replace(self.GRAPH_BASE_URL, "")

        logger.debug(f"Listed {len(files)} files from OneDrive path: {path}")
        return files

    async def read_file(self, path: str) -> bytes:
        """Read file contents from OneDrive."""
        await self._ensure_token_valid()

        onedrive_path = path if path.startswith("/") else f"/{path}"
        endpoint = f"/me/drive/root:{onedrive_path}"

        try:
            file_data = await self._make_request("GET", endpoint)
        except OneDriveError as e:
            if e.status_code == 404:
                raise FileNotFoundError(f"File not found: {path}")
            raise
        except PermissionError:
            raise

        download_url = file_data.get("@microsoft.graph.downloadUrl")
        if not download_url:
            raise OneDriveError(f"No download URL available for: {path}")

        try:
            access_token = await self._ensure_token_valid()
            headers = {"Authorization": f"Bearer {access_token}"}
            response = await self.http_client.get(download_url, headers=headers)
            response.raise_for_status()
            return response.content
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FileNotFoundError(f"File not found during download: {path}")
            raise OneDriveError(f"Download failed: {e.response.status_code}")
        except httpx.NetworkError as e:
            raise OneDriveError(f"Network error during download: {e}")

    async def get_metadata(self, path: str) -> FileMetadata:
        """Get detailed file metadata from OneDrive."""
        await self._ensure_token_valid()

        onedrive_path = path if path.startswith("/") else f"/{path}"
        endpoint = f"/me/drive/root:{onedrive_path}"

        try:
            data = await self._make_request("GET", endpoint)
        except OneDriveError as e:
            if e.status_code == 404:
                raise FileNotFoundError(f"Path not found: {path}")
            raise
        except PermissionError:
            raise

        size = data.get("size", 0)
        if isinstance(size, str):
            size = int(size)

        return FileMetadata(
            path=path,
            size_bytes=size,
            modified_at=self._parse_odt_datetime(data.get("lastModifiedDateTime")),
            created_at=self._parse_odt_datetime(data.get("createdDateTime")),
            file_hash=data.get("file", {}).get("hashes", {}).get("sha256Hash"),
            mime_type=data.get("file", {}).get("mimeType"),
            additional={
                "id": data.get("id"),
                "e_tag": data.get("eTag"),
                "c_tag": data.get("cTag"),
                "web_url": data.get("webUrl"),
            },
        )

    async def exists(self, path: str) -> bool:
        """Check if path exists in OneDrive."""
        try:
            await self.get_metadata(path)
            return True
        except FileNotFoundError:
            return False

    async def list_changes(
        self, since: datetime, path: Optional[str] = None
    ) -> List[FileChange]:
        """List changes in OneDrive since a given timestamp."""
        await self._ensure_token_valid()

        delta_token = self._delta_tokens.get(path or "root")

        if delta_token:
            endpoint = f"/me/drive/root:/delta(token='{delta_token}')"
        else:
            onedrive_path = path if path else ""
            if onedrive_path and not onedrive_path.startswith("/"):
                onedrive_path = f"/{onedrive_path}"

            endpoint = (
                f"/me/drive/root:{onedrive_path}:/delta"
                if onedrive_path
                else "/me/drive/root/delta"
            )

        changes: list[FileChange] = []
        last_data: dict[str, Any] = {}

        while endpoint:
            last_data = await self._make_request("GET", endpoint)
            items = last_data.get("value", [])

            for item in items:
                if item.get("name") == "root" and "folder" in item:
                    continue

                if item.get("deleted"):
                    change_type = FileChangeType.DELETED
                else:
                    change_type = FileChangeType.MODIFIED

                change = FileChange(
                    change_type=change_type,
                    path=item.get("name", ""),
                    timestamp=self._parse_odt_datetime(
                        item.get("lastModifiedDateTime")
                    ),
                    file_info=self._drive_item_to_file_info(item)
                    if "file" in item
                    else None,
                )
                changes.append(change)

            next_link: str | None = last_data.get("@odata.nextLink")
            if next_link:
                endpoint = next_link.replace(self.GRAPH_BASE_URL, "")
            else:
                endpoint = ""

        delta_url: str | None = last_data.get("@odata.deltaLink")
        if delta_url:
            token_start = delta_url.find("token=")
            if token_start > 0:
                extracted = delta_url[token_start + 6 :].strip("'\"\"")
                if extracted:
                    self._delta_tokens[path or "root"] = extracted

        logger.debug(f"Found {len(changes)} changes in OneDrive")
        return changes

    async def supports_watch(self) -> bool:
        """Check if native watching is supported."""
        return False

    async def watch(
        self, path: str, callback: Callable[[FileChange], None]
    ) -> WatchHandle:
        """Watch for changes at a path."""
        raise NotImplementedError(
            "OneDrive adapter does not support native watching. "
            "Use list_changes() for polling-based change detection."
        )

    async def close(self) -> None:
        """Close the adapter and release resources."""
        await self.http_client.aclose()

    async def __aenter__(self) -> OneDriveAdapter:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()
