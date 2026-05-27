"""Google Drive storage adapter for Grimoire.

This module provides a Google Drive implementation of the StorageAdapter ABC,
supporting OAuth2 authentication, file listing with pagination, and change
polling via the Google Drive API.

Example:
    >>> from grimoire.config import settings
    >>> from grimoire.storage.gdrive import GoogleDriveAdapter
    >>> adapter = GoogleDriveAdapter(settings.cloud.google)
    >>> files = await adapter.list_files("gdrive://Documents")
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from loguru import logger

from grimoire.config.settings import CloudGoogleConfig
from grimoire.storage.base import (
    FileChange,
    FileChangeType,
    FileInfo,
    FileMetadata,
    StorageAdapter,
    WatchHandle,
)


class GoogleDriveError(Exception):
    """Base exception for Google Drive adapter errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AuthenticationError(GoogleDriveError):
    """Raised when authentication fails or tokens are invalid."""

    pass


class RateLimitError(GoogleDriveError):
    """Raised when Google Drive API rate limit is exceeded."""

    pass


class TokenRefreshError(GoogleDriveError):
    """Raised when token refresh fails."""

    pass


class GoogleDriveAdapter(StorageAdapter):
    """Google Drive storage adapter using the Google Drive API v3.

    This adapter implements the StorageAdapter ABC for Google Drive, using
    OAuth2 authentication and the REST API. It supports:
    - OAuth2 flow with token persistence
    - File listing with pagination
    - Change tracking via the changes.list API
    - Token refresh on expiration
    - Rate limit handling with exponential backoff

    Attributes:
        config: CloudGoogleConfig with OAuth credentials and token store path.
        client: Async httpx client for API requests.
        tokens: Current OAuth tokens including access_token and refresh_token.
        page_token: Last known change page token for incremental sync.

    Example:
        >>> config = settings.cloud.google
        >>> adapter = GoogleDriveAdapter(config)
        >>> await adapter.authenticate()  # Run OAuth flow if needed
        >>> files = await adapter.list_files("gdrive://Documents")
    """

    # Google Drive API v3 endpoints
    API_BASE_URL = "https://www.googleapis.com/drive/v3"
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    # OAuth scopes needed for read-only access
    SCOPES = [
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    def __init__(self, config: CloudGoogleConfig) -> None:
        """Initialize the Google Drive adapter.

        Args:
            config: CloudGoogleConfig with credentials_path, token_store,
                client_id, and client_secret.
        """
        self.config = config
        self.client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.API_BASE_URL,
            timeout=30.0,
            follow_redirects=True,
        )
        self.tokens: dict[str, Any] = {}
        self._page_token: str | None = None
        self._credentials: dict[str, Any] = {}

    async def _load_credentials(self) -> dict[str, Any]:
        """Load OAuth credentials from the credentials file.

        Returns:
            Dictionary with client_id and client_secret.

        Raises:
            AuthenticationError: If credentials file cannot be loaded.
        """
        if self._credentials:
            return self._credentials

        creds_path = Path(self.config.credentials_path).expanduser()
        if not creds_path.exists():
            raise AuthenticationError(
                f"Google credentials file not found: {creds_path}."
                "Please create OAuth2 credentials at "
                "https://console.cloud.google.com/apis/credentials"
            )

        try:
            with open(creds_path, encoding="utf-8") as f:
                data = json.load(f)
                # Handle different credential file formats
                if "installed" in data:
                    # Desktop app credentials format
                    client_info = data["installed"]
                elif "web" in data:
                    # Web app credentials format
                    client_info = data["web"]
                else:
                    client_info = data

                self._credentials = {
                    "client_id": client_info.get("client_id") or self.config.client_id,
                    "client_secret": client_info.get("client_secret")
                    or self.config.client_secret,
                }

                if not self._credentials["client_id"]:
                    raise AuthenticationError(
                        "client_id not found in credentials file or config"
                    )

                return self._credentials
        except json.JSONDecodeError as e:
            raise AuthenticationError(f"Invalid JSON in credentials file: {e}") from e
        except OSError as e:
            raise AuthenticationError(f"Failed to read credentials file: {e}") from e

    async def _load_tokens(self) -> dict[str, Any]:
        """Load tokens from the token store, decrypting if necessary.

        Plain JSON is tried first for backward compatibility; if that fails,
        the file is treated as an encrypted payload and decrypted.
        """
        token_path = Path(self.config.token_store).expanduser()
        if token_path.exists():
            try:
                raw = token_path.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    from grimoire.utils.token_crypto import decrypt_tokens
                    data = decrypt_tokens(raw)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                logger.warning(f"Failed to load tokens from {token_path}: {e}")
        return {}

    async def _save_tokens(self, tokens: dict[str, Any]) -> None:
        """Save tokens to the token store, encrypting when possible.

        Falls back to plain JSON if the ``cryptography`` library is missing,
        emitting a loud warning so operators know tokens are not at-rest
        encrypted.
        """
        token_path = Path(self.config.token_store).expanduser()
        token_path.parent.mkdir(parents=True, exist_ok=True)

        encrypted = False
        try:
            from grimoire.utils.token_crypto import encrypt_tokens, TokenCryptoError
            payload = encrypt_tokens(tokens)
            encrypted = True
        except TokenCryptoError:
            logger.warning(
                "Token encryption unavailable (cryptography not installed?). "
                "Saving tokens as plain JSON — install cryptography to secure tokens at rest."
            )
            payload = json.dumps(tokens, indent=2)

        try:
            token_path.write_text(payload, encoding="utf-8")
            os.chmod(token_path, 0o600)
        except OSError as e:
            logger.error(f"Failed to save tokens to {token_path}: {e}")
            raise AuthenticationError(f"Failed to save tokens: {e}") from e

    async def _refresh_access_token(self) -> str:
        """Refresh the access token using the refresh token.

        Returns:
            New access token.

        Raises:
            TokenRefreshError: If token refresh fails.
        """
        tokens = await self._load_tokens()
        refresh_token = tokens.get("refresh_token")

        if not refresh_token:
            raise TokenRefreshError("No refresh token available")

        credentials = await self._load_credentials()

        data = {
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.TOKEN_URL, data=data)

            if response.status_code == 401:
                raise TokenRefreshError(
                    "Invalid refresh token, re-authentication required"
                )
            elif response.status_code == 400:
                error_data = response.json()
                error_msg = error_data.get(
                    "error_description", error_data.get("error", "Unknown error")
                )
                raise TokenRefreshError(f"Token refresh failed: {error_msg}")
            elif response.status_code != 200:
                raise TokenRefreshError(f"HTTP {response.status_code}: {response.text}")

            token_data: dict[str, Any] = response.json()
            access_token = token_data.get("access_token")
            if not isinstance(access_token, str):
                raise TokenRefreshError("Invalid token response: access_token missing")
            new_tokens = {
                "access_token": access_token,
                "refresh_token": refresh_token,  # Keep the existing refresh token
                "expires_at": time.time() + token_data.get("expires_in", 3600),
                "token_type": token_data.get("token_type", "Bearer"),
            }
            await self._save_tokens(new_tokens)
            self.tokens = new_tokens
            return access_token

        except httpx.NetworkError as e:
            raise TokenRefreshError(f"Network error during token refresh: {e}") from e
        except httpx.TimeoutException as e:
            raise TokenRefreshError(f"Timeout during token refresh: {e}") from e

    async def _get_access_token(self) -> str:
        """Get a valid access token, refreshing if necessary.

        Returns:
            Valid access token.

        Raises:
            AuthenticationError: If no valid token is available.
        """
        tokens = await self._load_tokens()

        access_token_val = tokens.get("access_token")
        access_token = str(access_token_val) if access_token_val else None
        expires_at = tokens.get("expires_at", 0)

        # Check if token is expired or about to expire (5 min buffer)
        if access_token and expires_at > time.time() + 300:
            self.tokens = tokens
            return access_token

        # Token expired or doesn't exist, try refresh
        if tokens.get("refresh_token"):
            try:
                return await self._refresh_access_token()
            except TokenRefreshError:
                logger.warning(
                    "Token refresh failed, re-authentication may be required"
                )
                raise AuthenticationError("Token expired and refresh failed")

        raise AuthenticationError("No valid access token available")

    async def _make_api_request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        """Make an authenticated API request with retry logic.

        Args:
            method: HTTP method (GET, POST, etc.).
            endpoint: API endpoint path (e.g., "/files").
            params: Query parameters.
            json_data: JSON body data.
            retries: Number of retries for rate limits.

        Returns:
            JSON response as dictionary.

        Raises:
            GoogleDriveError: On API errors.
            RateLimitError: On rate limiting after retries.
        """
        access_token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        last_exception: Exception | None = None

        for attempt in range(retries):
            try:
                response = await self.client.request(
                    method=method,
                    url=endpoint,
                    headers=headers,
                    params=params,
                    json=json_data,
                )

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 2**attempt))
                    logger.warning(f"Rate limited. Retrying after {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    continue

                # Handle token expiration during request
                if response.status_code == 401:
                    if attempt < retries - 1:
                        logger.debug("Access token expired, refreshing...")
                        access_token = await self._refresh_access_token()
                        headers["Authorization"] = f"Bearer {access_token}"
                        continue

                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    if attempt == retries - 1:
                        raise RateLimitError(
                            "Rate limit exceeded after retries",
                            status_code=429,
                        ) from e
                    continue
                raise GoogleDriveError(
                    f"API request failed: {e}",
                    status_code=e.response.status_code,
                ) from e

            except httpx.NetworkError as e:
                last_exception = e
                if attempt == retries - 1:
                    raise GoogleDriveError(f"Network error: {e}") from e
                wait_time = 2**attempt
                logger.warning(f"Network error, retrying in {wait_time}s: {e}")
                await asyncio.sleep(wait_time)

            except httpx.TimeoutException as e:
                last_exception = e
                if attempt == retries - 1:
                    raise GoogleDriveError(f"Request timeout: {e}") from e
                wait_time = 2**attempt
                logger.warning(f"Timeout, retrying in {wait_time}s: {e}")
                await asyncio.sleep(wait_time)

        raise GoogleDriveError(f"Max retries exceeded: {last_exception}")

    def _parse_gdrive_path(self, path: str) -> tuple[str | None, str]:
        """Parse a gdrive:// path into folder ID and relative path.

        Args:
            path: Path like "gdrive://Documents" or "gdrive://folder_id/Subfolder".

        Returns:
            Tuple of (folder_id, display_path). folder_id may be None for root.
        """
        if path.startswith("gdrive://"):
            path = path[9:]  # Remove scheme

        if not path or path == "/":
            return (None, "root")

        # Path could be a folder ID or a display name
        # For now, treat first component as potential folder ID or just use root
        parts = path.strip("/").split("/")
        folder_id = parts[0] if parts else None

        return (folder_id, "/".join(parts))

    def _file_to_file_info(self, file_data: dict[str, Any]) -> FileInfo:
        """Convert Google Drive file data to FileInfo.

        Args:
            file_data: Google Drive API file response.

        Returns:
            FileInfo object.
        """
        # Parse modified time
        modified_time_str = file_data.get("modifiedTime", "")
        try:
            modified_at = datetime.fromisoformat(
                modified_time_str.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            modified_at = datetime.now()

        size_bytes = 0
        if "size" in file_data:
            try:
                size_bytes = int(file_data["size"])
            except (ValueError, TypeError):
                pass

        return FileInfo(
            path=f"gdrive://{file_data.get('id', '')}",
            name=file_data.get("name", "Unknown"),
            size_bytes=size_bytes,
            modified_at=modified_at,
            is_directory=file_data.get("mimeType")
            == "application/vnd.google-apps.folder",
            mime_type=file_data.get("mimeType"),
            metadata={
                "id": file_data.get("id"),
                "mimeType": file_data.get("mimeType"),
                "webViewLink": file_data.get("webViewLink"),
                "md5Checksum": file_data.get("md5Checksum"),
            },
        )

    async def authenticate(self) -> str:
        """Run OAuth2 authentication flow.

        This method initiates the OAuth2 flow and returns a URL for the user
        to authorize the application. After authorization, the user should
        provide the authorization code to complete the flow.

        Returns:
            Authorization URL for the user to visit.

        Note:
            This is a synchronous method for the URL generation. The actual
            token exchange happens when the user provides the auth code via
            exchange_code().
        """
        credentials = await self._load_credentials()

        # Build authorization URL
        params = {
            "client_id": credentials["client_id"],
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",  # Out-of-band for CLI
            "scope": " ".join(self.SCOPES),
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",  # Force to get refresh token
        }

        from urllib.parse import urlencode

        auth_url = f"{self.AUTH_URL}?{urlencode(params)}"

        return auth_url

    async def exchange_code(self, auth_code: str) -> dict[str, Any]:
        """Exchange authorization code for access and refresh tokens.

        Args:
            auth_code: Authorization code from OAuth flow.

        Returns:
            Token dictionary with access_token, refresh_token, etc.
        """
        credentials = await self._load_credentials()

        data = {
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "code": auth_code,
            "grant_type": "authorization_code",
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.TOKEN_URL, data=data)

            response.raise_for_status()
            token_data = response.json()

            tokens = {
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token"),
                "expires_at": time.time() + token_data.get("expires_in", 3600),
                "token_type": token_data.get("token_type", "Bearer"),
                "scope": token_data.get("scope", ""),
            }

            await self._save_tokens(tokens)
            self.tokens = tokens

            logger.info("Successfully authenticated with Google Drive")
            return tokens

        except httpx.HTTPStatusError as e:
            error_text = e.response.text
            try:
                error_json = e.response.json()
                error_text = error_json.get(
                    "error_description", error_json.get("error", error_text)
                )
            except Exception:
                pass
            raise AuthenticationError(f"Token exchange failed: {error_text}") from e
        except httpx.NetworkError as e:
            raise AuthenticationError(
                f"Network error during authentication: {e}"
            ) from e

    async def list_files(
        self, path: str = "gdrive://", recursive: bool = False
    ) -> list[FileInfo]:
        """List files in a Google Drive folder.

        Uses the Google Drive files.list API with pagination.

        Args:
            path: Google Drive path (e.g., "gdrive://Documents" or folder ID).
            recursive: If True, include files from subfolders.

        Returns:
            List of FileInfo objects.

        Raises:
            GoogleDriveError: On API errors.
            AuthenticationError: If not authenticated.
        """
        folder_id, _ = self._parse_gdrive_path(path)

        # Build query
        query_parts = ["trashed = false"]
        if folder_id:
            # Check if it's a folder ID (usually longer alphanumeric) or name
            if len(folder_id) > 20 or " " not in folder_id:
                query_parts.append(f"'{folder_id}' in parents")
            else:
                # Try to find by name
                query_parts.append(
                    f"name = '{folder_id}' and mimeType = 'application/vnd.google-apps.folder'"
                )

        query = " and ".join(query_parts)

        params = {
            "q": query,
            "spaces": "drive",
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink,md5Checksum)",
            "pageSize": 100,
        }

        files: list[FileInfo] = []
        page_token: str | None = None

        while True:
            if page_token:
                params["pageToken"] = page_token

            result = await self._make_api_request("GET", "/files", params=params)

            for file_data in result.get("files", []):
                file_info = self._file_to_file_info(file_data)
                files.append(file_info)

                # Recursively load subfolders if requested
                if recursive and file_info.is_directory:
                    try:
                        subfiles = await self.list_files(
                            f"gdrive://{file_data['id']}", recursive=True
                        )
                        files.extend(subfiles)
                    except GoogleDriveError as e:
                        logger.warning(
                            f"Failed to list subdirectory {file_info.name}: {e}"
                        )

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        logger.debug(f"Listed {len(files)} files from Google Drive: {path}")
        return files

    async def read_file(self, path: str) -> bytes:
        """Read file contents from Google Drive.

        Supports downloading regular files. Google Workspace files
        (Docs, Sheets, etc.) are exported as PDF.

        Args:
            path: File path (e.g., "gdrive://file_id").

        Returns:
            File contents as bytes.

        Raises:
            FileNotFoundError: If file doesn't exist.
            GoogleDriveError: On API errors.
        """
        file_id, _ = self._parse_gdrive_path(path)
        if not file_id:
            raise ValueError("Invalid Google Drive path: no file ID found")

        # First get file metadata to determine mime type
        meta_params = {
            "fields": "id,mimeType,name,size",
        }

        try:
            file_meta = await self._make_api_request(
                "GET", f"/files/{file_id}", params=meta_params
            )
            mime_type = file_meta.get("mimeType", "")
        except GoogleDriveError as e:
            if e.status_code == 404:
                raise FileNotFoundError(f"File not found: {path}") from e
            raise

        access_token = await self._get_access_token()

        # Determine download URL based on file type
        if mime_type.startswith("application/vnd.google-apps."):
            # Google Workspace file - export to PDF
            export_mime = "application/pdf"
            download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType={export_mime}"
        else:
            # Regular file
            download_url = (
                f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
            )

        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(download_url, headers=headers)
                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise FileNotFoundError(f"File not found: {path}") from e
            raise GoogleDriveError(f"Failed to download file: {e}") from e
        except httpx.NetworkError as e:
            raise GoogleDriveError(f"Network error downloading file: {e}") from e

    async def get_metadata(self, path: str) -> FileMetadata:
        """Get detailed file metadata from Google Drive.

        Args:
            path: File or folder path.

        Returns:
            FileMetadata with detailed information.

        Raises:
            FileNotFoundError: If path doesn't exist.
            GoogleDriveError: On API errors.
        """
        file_id, _ = self._parse_gdrive_path(path)
        if not file_id:
            raise ValueError("Invalid Google Drive path: no file ID found")

        params = {
            "fields": "id,name,mimeType,size,createdTime,modifiedTime,viewedByMeTime,owners,permissions,webViewLink,md5Checksum",
        }

        try:
            result = await self._make_api_request(
                "GET", f"/files/{file_id}", params=params
            )
        except GoogleDriveError as e:
            if e.status_code == 404:
                raise FileNotFoundError(f"File not found: {path}") from e
            raise

        created_time_str = result.get("createdTime", "")
        modified_time_str = result.get("modifiedTime", "")
        viewed_time_str = result.get("viewedByMeTime", "")

        try:
            created_at = (
                datetime.fromisoformat(created_time_str.replace("Z", "+00:00"))
                if created_time_str
                else None
            )
        except ValueError:
            created_at = None

        try:
            modified_at = (
                datetime.fromisoformat(modified_time_str.replace("Z", "+00:00"))
                if modified_time_str
                else datetime.now()
            )
        except ValueError:
            modified_at = datetime.now()

        try:
            accessed_at = (
                datetime.fromisoformat(viewed_time_str.replace("Z", "+00:00"))
                if viewed_time_str
                else None
            )
        except ValueError:
            accessed_at = None

        size_bytes = 0
        if "size" in result:
            try:
                size_bytes = int(result["size"])
            except (ValueError, TypeError):
                pass

        owners = result.get("owners", [])
        owner = owners[0].get("displayName") if owners else None

        return FileMetadata(
            path=path,
            size_bytes=size_bytes,
            created_at=created_at,
            modified_at=modified_at,
            accessed_at=accessed_at,
            file_hash=result.get("md5Checksum"),
            owner=owner,
            mime_type=result.get("mimeType"),
            additional={
                "id": result.get("id"),
                "webViewLink": result.get("webViewLink"),
                "mimeType": result.get("mimeType"),
            },
        )

    async def exists(self, path: str) -> bool:
        """Check if a file or folder exists in Google Drive.

        Args:
            path: Path to check.

        Returns:
            True if path exists, False otherwise.
        """
        file_id, _ = self._parse_gdrive_path(path)
        if not file_id:
            return True  # Root always exists

        try:
            await self._make_api_request(
                "GET",
                f"/files/{file_id}",
                params={"fields": "id", "supportsAllDrives": True},
            )
            return True
        except GoogleDriveError as e:
            if e.status_code == 404:
                return False
            raise

    async def list_changes(
        self, since: datetime, path: str | None = None
    ) -> list[FileChange]:
        """List changes since a given timestamp using the changes.list API.

        Uses Google Drive's change tracking with page tokens for efficient
        incremental sync. The first call starts from the current state.

        Args:
            since: Timestamp to check changes from.
            path: Optional path to limit scope (not fully supported by API).

        Returns:
            List of FileChange objects.

        Raises:
            GoogleDriveError: On API errors.
        """
        # Get a starting page token if we don't have one
        if self._page_token is None:
            token_result = await self._make_api_request(
                "GET",
                "/changes/startPageToken",
                params={"supportsAllDrives": True},
            )
            self._page_token = token_result.get("startPageToken")

        changes: list[FileChange] = []
        page_token = self._page_token

        while page_token:
            result = await self._make_api_request(
                "GET",
                "/changes",
                params={
                    "pageToken": page_token,
                    "fields": "nextPageToken,newStartPageToken,changes(fileId,file(name,mimeType,size,modifiedTime,trashed),removed)",
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                },
            )

            for change_data in result.get("changes", []):
                change_type = FileChangeType.MODIFIED

                if change_data.get("removed") or change_data.get("file", {}).get(
                    "trashed"
                ):
                    change_type = FileChangeType.DELETED
                elif not change_data.get("file"):
                    change_type = FileChangeType.DELETED

                file_id = change_data.get("fileId", "")
                file_data = change_data.get("file", {})

                file_info = None
                if file_data and not change_data.get("removed"):
                    file_info = self._file_to_file_info(file_data)

                change = FileChange(
                    change_type=change_type,
                    path=f"gdrive://{file_id}",
                    timestamp=datetime.now(),  # API doesn't provide change timestamp directly
                    file_info=file_info,
                )
                changes.append(change)

            page_token = result.get("nextPageToken")
            if not page_token:
                # Save the new start token for next time
                new_start_token = result.get("newStartPageToken")
                if new_start_token:
                    self._page_token = new_start_token
                break

        logger.debug(f"Found {len(changes)} changes in Google Drive")
        return changes

    async def supports_watch(self) -> bool:
        """Return False as Google Drive adapter uses polling.

        Google Drive does support push notifications via webhooks,
        but they require a publicly accessible callback URL. For local
        installations, polling via list_changes() is used instead.

        Returns:
            False - no native watching support.
        """
        return False

    async def watch(
        self, path: str, callback: Callable[[FileChange], None]
    ) -> WatchHandle:
        """Raise NotImplementedError as watching is not supported.

        Google Drive uses polling via list_changes() instead of native
        filesystem watching. The polling logic should be implemented
        in the WatchManager using list_changes() method.

        Args:
            path: Directory path to watch.
            callback: Function called on each file change event.

        Raises:
            NotImplementedError: Always raised since watching is not supported.
        """
        raise NotImplementedError(
            "Google Drive adapter does not support native watching. "
            "Use list_changes() with polling instead."
        )

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        await self.client.aclose()

    def __del__(self) -> None:
        """Cleanup when the adapter is garbage collected."""
        try:
            asyncio.get_event_loop().create_task(self.close())
        except Exception:
            pass  # Don't raise during garbage collection
