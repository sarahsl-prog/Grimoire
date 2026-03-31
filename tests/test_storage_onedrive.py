"""Unit tests for OneDrive storage adapter.

This module provides comprehensive test coverage for the OneDriveAdapter
using mocked Microsoft Graph API responses.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from grimoire.config.settings import CloudOnedriveConfig
from grimoire.storage.base import FileChangeType
from grimoire.storage.onedrive import (
    OneDriveAdapter,
    OneDriveAuthError,
    OneDriveError,
    OneDriveRateLimitError,
    OneDriveTokenData,
)


@pytest.fixture
def onedrive_config(temp_directory: Path) -> CloudOnedriveConfig:
    """Create a test OneDrive configuration."""
    token_store = temp_directory / "onedrive_tokens.json"
    return CloudOnedriveConfig(
        client_id="test_client_id",
        client_secret="test_client_secret",
        token_store=str(token_store),
    )


@pytest.fixture
def mock_token_data() -> OneDriveTokenData:
    """Create mock token data."""
    return OneDriveTokenData(
        access_token="test_access_token",
        refresh_token="test_refresh_token",
        expires_at=datetime.now() + timedelta(hours=1),
    )


@pytest.fixture
def expired_token_data() -> OneDriveTokenData:
    """Create expired token data."""
    return OneDriveTokenData(
        access_token="expired_access_token",
        refresh_token="test_refresh_token",
        expires_at=datetime.now() - timedelta(hours=1),
    )


@pytest.fixture
def sample_drive_item() -> dict[str, Any]:
    """Create a sample drive item from Graph API."""
    return {
        "id": "12345",
        "name": "document.pdf",
        "size": 1024,
        "lastModifiedDateTime": "2024-01-15T10:30:00Z",
        "createdDateTime": "2024-01-01T08:00:00Z",
        "eTag": '"{etag}"',
        "cTag": '"{ctag}"',
        "webUrl": "https://1drv.ms/b/s!ABC123",
        "parentReference": {"path": "/drive/root:/Documents"},
        "file": {
            "mimeType": "application/pdf",
            "hashes": {"sha256Hash": "abc123hash"},
        },
    }


@pytest.fixture
def sample_folder_item() -> dict[str, Any]:
    """Create a sample folder drive item."""
    return {
        "id": "folder123",
        "name": "Documents",
        "size": 0,
        "lastModifiedDateTime": "2024-01-10T09:00:00Z",
        "createdDateTime": "2024-01-01T08:00:00Z",
        "parentReference": {"path": "/drive/root:"},
        "folder": {"childCount": 5},
    }


class TestOneDriveTokenData:
    """Tests for OneDriveTokenData class."""

    def test_create_valid_token(self) -> None:
        """Token data can be created with valid values."""
        token = OneDriveTokenData(
            access_token="access",
            refresh_token="refresh",
            expires_at=datetime.now() + timedelta(hours=1),
        )
        assert token.access_token == "access"
        assert token.refresh_token == "refresh"
        assert not token.is_expired()

    def test_expired_token_detection(self) -> None:
        """Expired tokens are correctly detected."""
        past = datetime.now() - timedelta(hours=1)
        token = OneDriveTokenData(
            access_token="access",
            refresh_token="refresh",
            expires_at=past,
        )
        assert token.is_expired()

    def test_token_to_dict(self) -> None:
        """Token can be serialized to dictionary."""
        expires = datetime(2024, 1, 1, 12, 0, 0)
        token = OneDriveTokenData(
            access_token="access",
            refresh_token="refresh",
            expires_at=expires,
            scope="Files.Read",
            token_type="Bearer",
        )
        data = token.to_dict()
        assert data["access_token"] == "access"
        assert data["refresh_token"] == "refresh"

    def test_token_from_dict(self) -> None:
        """Token can be deserialized from dictionary."""
        data = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": "2024-01-01T12:00:00",
            "scope": "Files.Read",
            "token_type": "Bearer",
        }
        token = OneDriveTokenData.from_dict(data)
        assert token.access_token == "access"
        assert token.refresh_token == "refresh"


class TestOneDriveAdapterInit:
    """Tests for OneDriveAdapter initialization."""

    def test_create_adapter(self, onedrive_config: CloudOnedriveConfig) -> None:
        """Adapter can be created with valid config."""
        adapter = OneDriveAdapter(onedrive_config)
        assert adapter.config == onedrive_config
        assert adapter.token_data is None

    def test_adapter_loads_existing_tokens(
        self, onedrive_config: CloudOnedriveConfig, mock_token_data: OneDriveTokenData
    ) -> None:
        """Adapter loads tokens from file if available."""
        token_path = Path(onedrive_config.token_store)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            json.dump(mock_token_data.to_dict(), f)

        adapter = OneDriveAdapter(onedrive_config)
        assert adapter.token_data is not None
        assert adapter.token_data.access_token == mock_token_data.access_token

    def test_adapter_handles_missing_token_file(
        self, onedrive_config: CloudOnedriveConfig
    ) -> None:
        """Adapter handles missing token file gracefully."""
        adapter = OneDriveAdapter(onedrive_config)
        assert adapter.token_data is None

    async def test_adapter_context_manager(self, onedrive_config: CloudOnedriveConfig) -> None:
        """Adapter works as async context manager."""
        async with OneDriveAdapter(onedrive_config) as adapter:
            assert isinstance(adapter, OneDriveAdapter)


class TestOneDriveAuth:
    """Tests for OneDrive authentication."""

    def test_get_auth_url_success(self, onedrive_config: CloudOnedriveConfig) -> None:
        """Auth URL is generated correctly."""
        adapter = OneDriveAdapter(onedrive_config)
        url = adapter.get_auth_url("http://localhost:8080/callback", state="test_state")

        assert "login.microsoftonline.com" in url
        assert "test_client_id" in url
        assert "Files.Read" in url

    async def test_authenticate_success(
        self, onedrive_config: CloudOnedriveConfig, httpx_mock: HTTPXMock
    ) -> None:
        """Successful authentication stores tokens."""
        httpx_mock.add_response(
            url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            json={
                "access_token": "new_access_token",
                "refresh_token": "new_refresh_token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

        adapter = OneDriveAdapter(onedrive_config)
        await adapter.authenticate("auth_code", "http://localhost:8080/callback")

        assert adapter.token_data is not None
        assert adapter.token_data.access_token == "new_access_token"


class TestTokenRefresh:
    """Tests for token refresh functionality."""

    async def test_token_refresh_when_expired(
        self, onedrive_config: CloudOnedriveConfig,
        expired_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """Token is refreshed when expired."""
        httpx_mock.add_response(
            url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            json={
                "access_token": "refreshed_access_token",
                "refresh_token": "refreshed_refresh_token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = expired_token_data

        await adapter._refresh_token()

        assert adapter.token_data.access_token == "refreshed_access_token"

    async def test_token_refresh_failure(
        self, onedrive_config: CloudOnedriveConfig,
        expired_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """Token refresh failure raises auth error."""
        httpx_mock.add_response(
            url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            status_code=401,
            json={"error": "invalid_grant"},
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = expired_token_data

        with pytest.raises(OneDriveAuthError, match="Token refresh failed"):
            await adapter._ensure_token_valid()


class TestListFiles:
    """Tests for list_files method."""

    async def test_list_files_non_recursive(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        sample_drive_item: dict[str, Any],
        sample_folder_item: dict[str, Any],
        httpx_mock: HTTPXMock
    ) -> None:
        """Files can be listed non-recursively."""
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root/children",
            json={"value": [sample_folder_item, sample_drive_item]},
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        files = await adapter.list_files("/", recursive=False)

        assert len(files) == 2
        file_info = [f for f in files if f.name == "document.pdf"][0]
        assert file_info.size_bytes == 1024
        assert file_info.mime_type == "application/pdf"
        assert not file_info.is_directory

    async def test_list_files_recursive(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        sample_drive_item: dict[str, Any],
        httpx_mock: HTTPXMock
    ) -> None:
        """Files can be listed recursively using delta endpoint."""
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root:/Documents:/delta",
            json={"value": [sample_drive_item]},
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        files = await adapter.list_files("/Documents", recursive=True)

        assert len(files) == 1
        assert files[0].name == "document.pdf"


class TestReadFile:
    """Tests for read_file method."""

    async def test_read_file_success(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """File can be read successfully."""
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root:/Documents/document.pdf",
            json={
                "id": "123",
                "name": "document.pdf",
                "@microsoft.graph.downloadUrl": "https://download.url/file",
            },
        )
        httpx_mock.add_response(
            url="https://download.url/file",
            content=b"PDF file content",
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        content = await adapter.read_file("/Documents/document.pdf")

        assert content == b"PDF file content"

    async def test_read_file_not_found(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """FileNotFoundError raised when file doesn't exist."""
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root:/nonexistent.txt",
            status_code=404,
            json={"error": {"message": "Item not found"}},
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        with pytest.raises(FileNotFoundError):
            await adapter.read_file("/nonexistent.txt")

    async def test_read_file_permission_denied(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """PermissionError raised when access denied."""
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root:/restricted.pdf",
            status_code=403,
            json={"error": {"message": "Access denied"}},
        )

        valid_token = OneDriveTokenData(
            access_token=mock_token_data.access_token,
            refresh_token=mock_token_data.refresh_token,
            expires_at=datetime.now() + timedelta(hours=1),
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = valid_token

        with pytest.raises(PermissionError):
            await adapter.read_file("/restricted.pdf")


class TestListChanges:
    """Tests for list_changes method."""

    async def test_list_changes_success(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        sample_drive_item: dict[str, Any],
        httpx_mock: HTTPXMock
    ) -> None:
        """Changes can be listed successfully."""
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root:/Documents:/delta",
            json={
                "value": [sample_drive_item],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/drive/root:/Documents:/delta?token='abc123'",
            },
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        since = datetime.now() - timedelta(days=1)
        changes = await adapter.list_changes(since, path="/Documents")

        assert len(changes) == 1
        assert changes[0].path == "document.pdf"

    async def test_list_changes_deleted_item(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """Deleted items are correctly identified."""
        deleted_item = {
            "id": "deleted123",
            "name": "deleted.pdf",
            "deleted": {"state": "deleted"},
        }
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root/delta",
            json={
                "value": [deleted_item],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token='abc'",
            },
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        since = datetime.now() - timedelta(days=1)
        changes = await adapter.list_changes(since)

        assert len(changes) == 1
        assert changes[0].change_type == FileChangeType.DELETED


class TestWatch:
    """Tests for watch method."""

    async def test_supports_watch_returns_false(
        self, onedrive_config: CloudOnedriveConfig
    ) -> None:
        """supports_watch returns False for OneDrive."""
        adapter = OneDriveAdapter(onedrive_config)
        result = await adapter.supports_watch()
        assert result is False

    async def test_watch_raises_not_implemented(
        self, onedrive_config: CloudOnedriveConfig
    ) -> None:
        """watch raises NotImplementedError."""
        adapter = OneDriveAdapter(onedrive_config)

        with pytest.raises(NotImplementedError, match="does not support native watching"):
            await adapter.watch("/", lambda x: None)


class TestRateLimiting:
    """Tests for rate limit handling."""

    async def test_rate_limit_error(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """Rate limit error includes retry-after information."""
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root/children",
            status_code=429,
            headers={"Retry-After": "120"},
            json={"error": {"message": "Rate limit exceeded"}},
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        with pytest.raises(OneDriveRateLimitError) as exc_info:
            await adapter.list_files("/")

        assert exc_info.value.retry_after == 120


class TestTokenBuffer:
    """Tests for token buffer handling."""

    def test_token_buffer_detection(self) -> None:
        """Tokens within buffer are detected as expired."""
        near_future = datetime.now() + timedelta(minutes=2)
        token = OneDriveTokenData(
            access_token="access",
            refresh_token="refresh",
            expires_at=near_future,
        )
        assert token.is_expired(buffer_seconds=300)


class TestExists:
    """Tests for exists method."""

    async def test_exists_true(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        sample_drive_item: dict[str, Any],
        httpx_mock: HTTPXMock
    ) -> None:
        """Returns True for existing file."""
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root:/existing.txt",
            json=sample_drive_item,
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        result = await adapter.exists("/existing.txt")
        assert result is True

    async def test_exists_false(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """Returns False for non-existent file."""
        httpx_mock.add_response(
            url="https://graph.microsoft.com/v1.0/me/drive/root:/nonexistent.txt",
            status_code=404,
            json={"error": {"message": "Item not found"}},
        )

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        result = await adapter.exists("/nonexistent.txt")
        assert result is False


class TestNetworkErrors:
    """Tests for network error handling."""

    async def test_network_error_during_list_files(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """Network errors are handled gracefully."""
        httpx_mock.add_exception(httpx.ConnectError("Network unreachable"))

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        with pytest.raises(OneDriveError, match="Network error"):
            await adapter.list_files("/")

    async def test_timeout_during_list_files(
        self,
        onedrive_config: CloudOnedriveConfig,
        mock_token_data: OneDriveTokenData,
        httpx_mock: HTTPXMock
    ) -> None:
        """Timeout errors are handled gracefully."""
        httpx_mock.add_exception(httpx.TimeoutException("Request timed out"))

        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = mock_token_data

        with pytest.raises(OneDriveError, match="timeout"):
            await adapter.list_files("/")


class TestTokenPersistence:
    """Tests for token persistence."""

    def test_load_corrupt_token_file(
        self, onedrive_config: CloudOnedriveConfig
    ) -> None:
        """Corrupt token file is handled gracefully."""
        token_path = Path(onedrive_config.token_store)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, "w") as f:
            f.write("not valid json {{ ! }}")

        adapter = OneDriveAdapter(onedrive_config)
        assert adapter.token_data is None

    async def test_tokens_saved_after_authentication(
        self, onedrive_config: CloudOnedriveConfig, httpx_mock: HTTPXMock
    ) -> None:
        """Tokens are saved after successful authentication."""
        httpx_mock.add_response(
            url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
            },
        )

        adapter = OneDriveAdapter(onedrive_config)
        await adapter.authenticate("code", "http://localhost/callback")

        token_path = Path(onedrive_config.token_store)
        assert token_path.exists()

        with open(token_path) as f:
            saved_data = json.load(f)

        assert saved_data["access_token"] == "access"

    async def test_no_save_if_token_is_none(
        self, onedrive_config: CloudOnedriveConfig
    ) -> None:
        """No save if token is None."""
        adapter = OneDriveAdapter(onedrive_config)
        adapter.token_data = None
        adapter._save_tokens()  # type: ignore[misc]

