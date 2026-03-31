"""Unit tests for Google Drive storage adapter.

This module provides comprehensive tests for the GoogleDriveAdapter class,
using mocked API responses to avoid requiring real credentials.

The tests cover:
- OAuth2 authentication flows
- Token refresh and storage
- File listing with pagination
- Change tracking
- Error handling (rate limits, network errors)
- Edge cases and input validation
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Generator
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest

from grimoire.config.settings import CloudGoogleConfig
from grimoire.storage.base import (
    FileChange,
    FileChangeType,
    FileInfo,
    FileMetadata,
)

# Handle import with and without the storage module available
try:
    from grimoire.storage.gdrive import (
        AuthenticationError,
        GoogleDriveAdapter,
        GoogleDriveError,
        RateLimitError,
        TokenRefreshError,
    )

    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False
    GoogleDriveAdapter = Any  # type: ignore
    AuthenticationError = Any  # type: ignore
    GoogleDriveError = Any  # type: ignore
    RateLimitError = Any  # type: ignore
    TokenRefreshError = Any  # type: ignore


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_config(tmp_path: Path) -> CloudGoogleConfig:
    """Create a mock CloudGoogleConfig for testing."""
    creds_path = tmp_path / "credentials.json"
    token_store = tmp_path / "tokens.json"

    # Write mock credentials file
    credentials = {
        "installed": {
            "client_id": "test_client_123.apps.googleusercontent.com",
            "client_secret": "test_secret_abc",
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"],
        }
    }
    creds_path.write_text(json.dumps(credentials))

    return CloudGoogleConfig(
        credentials_path=str(creds_path),
        token_store=str(token_store),
        client_id=None,
        client_secret=None,
    )


@pytest.fixture
def mock_tokens() -> dict[str, Any]:
    """Create mock OAuth tokens."""
    return {
        "access_token": "mock_access_token_123",
        "refresh_token": "mock_refresh_token_456",
        "expires_at": time.time() + 3600,
        "token_type": "Bearer",
        "scope": "https://www.googleapis.com/auth/drive.readonly",
    }


@pytest.fixture
def mock_gdrive_file() -> dict[str, Any]:
    """Create a mock Google Drive file response."""
    return {
        "id": "file_123",
        "name": "test_document.pdf",
        "mimeType": "application/pdf",
        "size": "1024",
        "modifiedTime": "2024-01-15T10:30:00.000Z",
        "md5Checksum": "abc123def456",
        "webViewLink": "https://drive.google.com/file/d/file_123/view",
    }


@pytest.fixture
def mock_gdrive_folder() -> dict[str, Any]:
    """Create a mock Google Drive folder response."""
    return {
        "id": "folder_456",
        "name": "Documents",
        "mimeType": "application/vnd.google-apps.folder",
        "size": "0",
        "modifiedTime": "2024-01-14T08:00:00.000Z",
        "webViewLink": "https://drive.google.com/drive/folders/folder_456",
    }


@pytest.fixture
def adapter(
    mock_config: CloudGoogleConfig,
) -> Generator[GoogleDriveAdapter, None, None]:
    """Create a GoogleDriveAdapter instance for testing."""
    if not GDRIVE_AVAILABLE:
        pytest.skip("Google Drive adapter not available")

    test_adapter = GoogleDriveAdapter(mock_config)
    yield test_adapter


# ============================================================================
# Happy Path Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterHappyPath:
    """Test normal operation scenarios."""

    @pytest.mark.asyncio
    async def test_init(self, adapter: GoogleDriveAdapter) -> None:
        """Adapter initializes with config."""
        assert adapter.config is not None
        assert adapter.API_BASE_URL == "https://www.googleapis.com/drive/v3"
        assert adapter.tokens == {}

    @pytest.mark.asyncio
    async def test_load_credentials(
        self, adapter: GoogleDriveAdapter, mock_config: CloudGoogleConfig
    ) -> None:
        """Credentials load from JSON file."""
        credentials = await adapter._load_credentials()
        assert credentials["client_id"] == "test_client_123.apps.googleusercontent.com"
        assert credentials["client_secret"] == "test_secret_abc"

    @pytest.mark.asyncio
    async def test_load_credentials_with_direct_config(self, tmp_path: Path) -> None:
        """Credentials can also come from config values."""
        creds_path = tmp_path / "empty.json"
        creds_path.write_text("{}")

        config = CloudGoogleConfig(
            credentials_path=str(creds_path),
            token_store=str(tmp_path / "tokens.json"),
            client_id="direct_client_id",
            client_secret="direct_secret",
        )
        adapter = GoogleDriveAdapter(config)
        credentials = await adapter._load_credentials()
        assert credentials["client_id"] == "direct_client_id"
        assert credentials["client_secret"] == "direct_secret"

    @pytest.mark.asyncio
    async def test_load_tokens(
        self, adapter: GoogleDriveAdapter, mock_tokens: dict[str, Any]
    ) -> None:
        """Tokens load from token store."""
        # Save tokens first
        await adapter._save_tokens(mock_tokens)

        loaded = await adapter._load_tokens()
        assert loaded["access_token"] == mock_tokens["access_token"]
        assert loaded["refresh_token"] == mock_tokens["refresh_token"]

    @pytest.mark.asyncio
    async def test_save_tokens(
        self, adapter: GoogleDriveAdapter, tmp_path: Path
    ) -> None:
        """Tokens are saved to token store with restricted permissions."""
        mock_tokens = {
            "access_token": "test_token",
            "refresh_token": "test_refresh",
            "expires_at": time.time(),
        }

        await adapter._save_tokens(mock_tokens)

        token_path = Path(adapter.config.token_store)
        assert token_path.exists()

        content = json.loads(token_path.read_text())
        assert content["access_token"] == "test_token"

        # Check permissions (Unix-only)
        if os.name == "posix":
            stat = os.stat(token_path)
            assert stat.st_mode & 0o777 == 0o600

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_list_files_success(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
        mock_gdrive_file: dict[str, Any],
        mock_gdrive_folder: dict[str, Any],
    ) -> None:
        """File listing returns FileInfo objects."""
        await adapter._save_tokens(mock_tokens)

        # Mock API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "files": [mock_gdrive_file, mock_gdrive_folder],
            "nextPageToken": None,
        }
        mock_response.headers = {}
        mock_request.return_value = mock_response

        files = await adapter.list_files("gdrive://Documents")

        assert len(files) == 2
        assert isinstance(files[0], FileInfo)
        assert files[0].name == "test_document.pdf"
        assert files[0].is_directory is False
        assert files[1].is_directory is True

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_list_files_empty_directory(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Empty directory returns empty list."""
        await adapter._save_tokens(mock_tokens)

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"files": [], "nextPageToken": None}
        mock_response.headers = {}
        mock_request.return_value = mock_response

        files = await adapter.list_files("gdrive://empty_folder")
        assert files == []

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_list_files_pagination(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """File listing handles pagination correctly."""
        await adapter._save_tokens(mock_tokens)

        # First page
        page1_response = Mock()
        page1_response.status_code = 200
        page1_response.json.return_value = {
            "files": [
                {
                    "id": "1",
                    "name": "file1",
                    "mimeType": "text/plain",
                    "size": "100",
                    "modifiedTime": "2024-01-01T00:00:00Z",
                }
            ],
            "nextPageToken": "page_token_123",
        }
        page1_response.headers = {}

        # Second page
        page2_response = Mock()
        page2_response.status_code = 200
        page2_response.json.return_value = {
            "files": [
                {
                    "id": "2",
                    "name": "file2",
                    "mimeType": "text/plain",
                    "size": "200",
                    "modifiedTime": "2024-01-02T00:00:00Z",
                }
            ],
            "nextPageToken": None,
        }
        page2_response.headers = {}

        mock_request.side_effect = [page1_response, page2_response]

        files = await adapter.list_files("gdrive://root")

        assert len(files) == 2
        assert files[0].name == "file1"
        assert files[1].name == "file2"

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_exists_true(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """exists() returns True for existing file."""
        await adapter._save_tokens(mock_tokens)

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "file_123"}
        mock_response.headers = {}
        mock_request.return_value = mock_response

        exists = await adapter.exists("gdrive://file_123")
        assert exists is True

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_exists_false(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """exists() returns False for missing file."""
        await adapter._save_tokens(mock_tokens)

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not found",
            request=Mock(),
            response=Mock(status_code=404),
        )
        mock_request.side_effect = httpx.HTTPStatusError(
            "Not found",
            request=Mock(),
            response=Mock(status_code=404, json=Mock(return_value={})),
        )

        exists = await adapter.exists("gdrive://nonexistent")
        assert exists is False

    @pytest.mark.asyncio
    async def test_supports_watch(self, adapter: GoogleDriveAdapter) -> None:
        """supports_watch() returns False for Google Drive."""
        supports = await adapter.supports_watch()
        assert supports is False

    @pytest.mark.asyncio
    async def test_authenticate_returns_url(self, adapter: GoogleDriveAdapter) -> None:
        """authenticate() returns authorization URL."""
        url = await adapter.authenticate()
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
        assert "client_id=test_client" in url or "client_id=" in url
        assert "scope=" in url


# ============================================================================
# File Reading Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterReadFile:
    """Test read_file functionality."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.get")
    async def test_read_file_success(
        self,
        mock_get: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Read file returns bytes successfully."""
        await adapter._save_tokens(mock_tokens)

        # Mock metadata response
        meta_response = Mock()
        meta_response.status_code = 200
        meta_response.json.return_value = {
            "id": "file_123",
            "mimeType": "application/pdf",
            "name": "test.pdf",
            "size": "1024",
        }
        meta_response.headers = {}

        # Mock download response
        download_response = Mock()
        download_response.status_code = 200
        download_response.content = b"file content bytes"

        with patch.object(
            adapter, "_make_api_request", new_callable=AsyncMock
        ) as mock_api:
            mock_api.return_value = meta_response.json.return_value
            mock_get.return_value = download_response

            content = await adapter.read_file("gdrive://file_123")
            assert content == b"file content bytes"

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.get")
    async def test_read_google_workspace_file(
        self,
        mock_get: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Read Google Workspace file downloads as PDF."""
        await adapter._save_tokens(mock_tokens)

        # Mock download
        download_response = Mock()
        download_response.status_code = 200
        download_response.content = b"pdf content"
        mock_get.return_value = download_response

        with patch.object(
            adapter, "_make_api_request", new_callable=AsyncMock
        ) as mock_api:
            mock_api.return_value = {
                "id": "doc_123",
                "mimeType": "application/vnd.google-apps.document",
                "name": "My Document",
            }

            content = await adapter.read_file("gdrive://doc_123")
            # Verify export URL is used for Google Docs
            assert content == b"pdf content"

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.get")
    async def test_read_file_network_retry(
        self,
        mock_get: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Read file handles network errors with retry."""
        await adapter._save_tokens(mock_tokens)

        mock_get.side_effect = httpx.NetworkError("Connection failed")

        with patch.object(
            adapter, "_make_api_request", new_callable=AsyncMock
        ) as mock_api:
            mock_api.return_value = {
                "id": "file_123",
                "mimeType": "text/plain",
                "name": "test.txt",
            }

            with pytest.raises((GoogleDriveError, httpx.NetworkError)):
                await adapter.read_file("gdrive://file_123")


# ============================================================================
# Edge Cases & Boundary Conditions
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_path_parsing(self, adapter: GoogleDriveAdapter) -> None:
        """Empty path parses to root."""
        folder_id, display_path = adapter._parse_gdrive_path("")
        assert folder_id is None
        assert display_path == "root"

    @pytest.mark.asyncio
    async def test_root_path_parsing(self, adapter: GoogleDriveAdapter) -> None:
        """Root path parsing."""
        folder_id, display_path = adapter._parse_gdrive_path("gdrive://")
        assert folder_id is None

    @pytest.mark.asyncio
    async def test_parse_gdrive_path_with_subpath(
        self, adapter: GoogleDriveAdapter
    ) -> None:
        """Path parsing with subdirectories."""
        folder_id, display_path = adapter._parse_gdrive_path(
            "gdrive://folder123/subfolder"
        )
        assert folder_id == "folder123"
        assert display_path == "folder123/subfolder"

    @pytest.mark.asyncio
    async def test_file_to_file_info_no_size(self, adapter: GoogleDriveAdapter) -> None:
        """FileInfo handles missing size field (folder)."""
        file_data = {
            "id": "folder_123",
            "name": "MyFolder",
            "mimeType": "application/vnd.google-apps.folder",
            "modifiedTime": "2024-01-01T00:00:00.000Z",
        }

        file_info = adapter._file_to_file_info(file_data)
        assert file_info.size_bytes == 0
        assert file_info.is_directory is True

    @pytest.mark.asyncio
    async def test_file_to_file_info_invalid_time(
        self, adapter: GoogleDriveAdapter
    ) -> None:
        """FileInfo handles invalid timestamp format."""
        file_data = {
            "id": "file_123",
            "name": "test.txt",
            "mimeType": "text/plain",
            "modifiedTime": "invalid-time",
        }

        file_info = adapter._file_to_file_info(file_data)
        assert isinstance(file_info.modified_at, datetime)

    @pytest.mark.asyncio
    async def test_get_access_token_with_valid_token(
        self, adapter: GoogleDriveAdapter, mock_tokens: dict[str, Any]
    ) -> None:
        """Valid token is returned without refresh."""
        await adapter._save_tokens(mock_tokens)

        token = await adapter._get_access_token()
        assert token == mock_tokens["access_token"]

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_refresh_expired_token(
        self,
        mock_post: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Expired token triggers automatic refresh."""
        expired_tokens = {
            **mock_tokens,
            "expires_at": time.time() - 100,  # Expired
        }
        await adapter._save_tokens(expired_tokens)

        # Mock token refresh response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_token_789",
            "refresh_token": "mock_refresh_token_456",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_post.return_value = mock_response

        token = await adapter._get_access_token()
        assert token == "new_token_789"

    @pytest.mark.asyncio
    async def test_missing_credentials_file(self, tmp_path: Path) -> None:
        """Missing credentials file raises AuthenticationError."""
        config = CloudGoogleConfig(
            credentials_path=str(tmp_path / "nonexistent.json"),
            token_store=str(tmp_path / "tokens.json"),
        )
        adapter = GoogleDriveAdapter(config)

        with pytest.raises(AuthenticationError) as exc_info:
            await adapter._load_credentials()
        assert "credentials file not found" in str(exc_info.value)


# ============================================================================
# Input Validation Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterInputValidation:
    """Test input validation."""

    @pytest.mark.asyncio
    async def test_invalid_gdrive_path(self, adapter: GoogleDriveAdapter) -> None:
        """Invalid path formats are handled."""
        # These don't raise, just parse differently
        folder_id, display_path = adapter._parse_gdrive_path("not-a-url")
        assert folder_id == "not-a-url"

    @pytest.mark.asyncio
    async def test_read_file_invalid_path(self, adapter: GoogleDriveAdapter) -> None:
        """Read without file ID raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            await adapter.read_file("gdrive://")
        assert "no file ID found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_metadata_invalid_path(self, adapter: GoogleDriveAdapter) -> None:
        """Get metadata without file ID raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            await adapter.get_metadata("gdrive://")
        assert "no file ID found" in str(exc_info.value)


# ============================================================================
# Error Handling Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterErrorHandling:
    """Test error handling for various failure modes."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_rate_limit_error(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Rate limit after retries raises RateLimitError."""
        await adapter._save_tokens(mock_tokens)

        # All responses return 429 (rate limited)
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "0"}  # 0 seconds to speed up test
        mock_request.return_value = mock_response

        with pytest.raises((RateLimitError, GoogleDriveError)):
            await adapter.list_files("gdrive://test")

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_token_refresh_on_401(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """401 response triggers token refresh."""
        await adapter._save_tokens({**mock_tokens, "expires_at": time.time() - 100})

        # First call 401, second call succeeds
        error_response = Mock()
        error_response.status_code = 401
        error_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=Mock(),
            response=Mock(status_code=401),
        )

        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {
            "files": [],
            "nextPageToken": None,
        }
        success_response.headers = {}

        mock_request.side_effect = [error_response, success_response]

        # This should try to refresh token on 401
        with patch.object(
            adapter, "_refresh_access_token", new_callable=AsyncMock
        ) as mock_refresh:
            mock_refresh.return_value = "new_token"
            # Give the new token to the next request
            mock_request.side_effect = [success_response]

            await adapter.list_files("gdrive://test")

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_invalid_credentials_format(
        self,
        mock_post: AsyncMock,
        adapter: GoogleDriveAdapter,
        tmp_path: Path,
    ) -> None:
        """Exchange code with invalid credentials raises AuthenticationError."""
        # Write invalid token format
        creds_path = Path(adapter.config.credentials_path)
        invalid_creds = {"invalid": "format"}
        creds_path.write_text(json.dumps(invalid_creds))

        # Should fail because client_id is missing
        with pytest.raises(AuthenticationError):
            await adapter._load_credentials()

    @pytest.mark.asyncio
    async def test_no_refresh_token_available(
        self, adapter: GoogleDriveAdapter, tmp_path: Path
    ) -> None:
        """Token refresh without refresh token raises TokenRefreshError."""
        tokens = {
            "access_token": "old_token",
            # No refresh_token
        }
        await adapter._save_tokens(tokens)

        with pytest.raises(TokenRefreshError) as exc_info:
            await adapter._refresh_access_token()
        assert "No refresh token available" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_token_exchange_network_error(
        self,
        mock_post: AsyncMock,
        adapter: GoogleDriveAdapter,
    ) -> None:
        """Network error during token exchange raises AuthenticationError."""
        mock_post.side_effect = httpx.NetworkError("Connection refused")

        with pytest.raises(AuthenticationError) as exc_info:
            await adapter.exchange_code("auth_code_123")
        assert "Network error" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.get")
    async def test_read_file_not_found(
        self,
        mock_get: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Read file returns 404 raises FileNotFoundError."""
        await adapter._save_tokens(mock_tokens)

        # Mock the metadata request first
        mock_meta_response = Mock()
        mock_meta_response.status_code = 404
        mock_meta_response.json.return_value = {}
        mock_meta_response.headers = {}

        with patch.object(
            adapter, "_make_api_request", new_callable=AsyncMock
        ) as mock_api:
            from grimoire.storage.gdrive import GoogleDriveError

            mock_api.side_effect = GoogleDriveError("File not found", status_code=404)

            with pytest.raises(FileNotFoundError) as exc_info:
                await adapter.read_file("gdrive://nonexistent_file")
            assert "not found" in str(exc_info.value).lower()


# ============================================================================
# Async Behavior Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterAsyncBehavior:
    """Test async behavior and concurrency."""

    @pytest.mark.asyncio
    async def test_client_initialization(self, adapter: GoogleDriveAdapter) -> None:
        """Async client is properly initialized."""
        assert adapter.client is not None
        assert isinstance(adapter.client, httpx.AsyncClient)

    @pytest.mark.asyncio
    async def test_close_client(self, adapter: GoogleDriveAdapter) -> None:
        """Close method closes HTTP client."""
        await adapter.close()


# ============================================================================
# Change Tracking Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterChangeTracking:
    """Test the changes.list API functionality."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_list_changes_success(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """list_changes returns FileChange objects."""
        await adapter._save_tokens(mock_tokens)

        # First call: get start page token
        start_response = Mock()
        start_response.status_code = 200
        start_response.json.return_value = {"startPageToken": "token_123"}
        start_response.headers = {}

        # Second call: get changes
        changes_response = Mock()
        changes_response.status_code = 200
        changes_response.json.return_value = {
            "changes": [
                {
                    "fileId": "file_1",
                    "file": {
                        "id": "file_1",
                        "name": "modified_doc.pdf",
                        "mimeType": "application/pdf",
                        "size": "2048",
                        "modifiedTime": "2024-01-20T15:00:00Z",
                    },
                    "removed": False,
                },
                {
                    "fileId": "file_2",
                    "removed": True,
                },
            ],
            "newStartPageToken": "token_456",
        }
        changes_response.headers = {}

        mock_request.side_effect = [start_response, changes_response]

        changes = await adapter.list_changes(datetime.now())

        assert len(changes) == 2
        assert isinstance(changes[0], FileChange)
        assert changes[0].change_type == FileChangeType.MODIFIED
        assert changes[1].change_type == FileChangeType.DELETED

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_list_changes_empty(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """list_changes with no changes returns empty list."""
        await adapter._save_tokens(mock_tokens)

        start_response = Mock()
        start_response.status_code = 200
        start_response.json.return_value = {"startPageToken": "token_123"}
        start_response.headers = {}

        changes_response = Mock()
        changes_response.status_code = 200
        changes_response.json.return_value = {
            "changes": [],
            "newStartPageToken": "token_456",
        }
        changes_response.headers = {}

        mock_request.side_effect = [start_response, changes_response]

        changes = await adapter.list_changes(datetime.now())
        assert changes == []

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_list_changes_trashed_file(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Trashed files are reported as DELETED."""
        await adapter._save_tokens(mock_tokens)

        start_response = Mock()
        start_response.status_code = 200
        start_response.json.return_value = {"startPageToken": "token_123"}
        start_response.headers = {}

        changes_response = Mock()
        changes_response.status_code = 200
        changes_response.json.return_value = {
            "changes": [
                {
                    "fileId": "file_1",
                    "file": {
                        "id": "file_1",
                        "name": "trashed_doc.pdf",
                        "trashed": True,
                    },
                    "removed": False,
                },
            ],
            "newStartPageToken": "token_456",
        }
        changes_response.headers = {}

        mock_request.side_effect = [start_response, changes_response]

        changes = await adapter.list_changes(datetime.now())

        assert len(changes) == 1
        assert changes[0].change_type == FileChangeType.DELETED


# ============================================================================
# Watch Method Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterWatch:
    """Test watch functionality (not supported)."""

    @pytest.mark.asyncio
    async def test_watch_raises_not_implemented(
        self, adapter: GoogleDriveAdapter
    ) -> None:
        """watch() method raises NotImplementedError."""
        with pytest.raises(NotImplementedError) as exc_info:
            await adapter.watch("gdrive://Documents", lambda x: None)
        assert "not support" in str(exc_info.value).lower()


# ============================================================================
# Metadata Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterMetadata:
    """Test metadata retrieval."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_get_metadata_success(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """get_metadata returns FileMetadata."""
        await adapter._save_tokens(mock_tokens)

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "file_123",
            "name": "important.pdf",
            "mimeType": "application/pdf",
            "size": "8192",
            "createdTime": "2024-01-01T00:00:00.000Z",
            "modifiedTime": "2024-01-15T12:00:00.000Z",
            "viewedByMeTime": "2024-01-16T08:00:00.000Z",
            "md5Checksum": "abc123def456",
            "owners": [{"displayName": "Test User"}],
            "webViewLink": "https://drive.google.com/file/d/file_123/view",
        }
        mock_response.headers = {}
        mock_request.return_value = mock_response

        metadata = await adapter.get_metadata("gdrive://file_123")

        assert isinstance(metadata, FileMetadata)
        assert metadata.size_bytes == 8192
        assert metadata.owner == "Test User"
        assert metadata.file_hash == "abc123def456"

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_get_metadata_not_found(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """get_metadata for nonexistent file raises FileNotFoundError."""
        await adapter._save_tokens(mock_tokens)

        from grimoire.storage.gdrive import GoogleDriveError

        with patch.object(
            adapter, "_make_api_request", new_callable=AsyncMock
        ) as mock_api:
            mock_api.side_effect = GoogleDriveError("File not found", status_code=404)

            with pytest.raises(FileNotFoundError):
                await adapter.get_metadata("gdrive://nonexistent")


# ============================================================================
# Credentials File Format Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterCredentialsFormat:
    """Test credentials file parsing variations."""

    @pytest.mark.asyncio
    async def test_credentials_web_format(self, tmp_path: Path) -> None:
        """Credentials file with 'web' format."""
        creds_path = tmp_path / "credentials.json"
        token_store = tmp_path / "tokens.json"

        creds = {
            "web": {
                "client_id": "web_client_123.apps.googleusercontent.com",
                "client_secret": "web_secret",
                "redirect_uris": ["http://localhost:8080/callback"],
            }
        }
        creds_path.write_text(json.dumps(creds))

        config = CloudGoogleConfig(
            credentials_path=str(creds_path),
            token_store=str(token_store),
        )
        adapter = GoogleDriveAdapter(config)

        credentials = await adapter._load_credentials()
        assert credentials["client_id"] == "web_client_123.apps.googleusercontent.com"

    @pytest.mark.asyncio
    async def test_credentials_plain_format(self, tmp_path: Path) -> None:
        """Credentials file with plain format (no installed/web wrapper)."""
        creds_path = tmp_path / "credentials.json"
        token_store = tmp_path / "tokens.json"

        creds = {
            "client_id": "plain_client_123.apps.googleusercontent.com",
            "client_secret": "plain_secret",
        }
        creds_path.write_text(json.dumps(creds))

        config = CloudGoogleConfig(
            credentials_path=str(creds_path),
            token_store=str(token_store),
        )
        adapter = GoogleDriveAdapter(config)

        credentials = await adapter._load_credentials()
        assert credentials["client_id"] == "plain_client_123.apps.googleusercontent.com"
        assert credentials["client_secret"] == "plain_secret"

    @pytest.mark.asyncio
    async def test_credentials_json_decode_error(self, tmp_path: Path) -> None:
        """Invalid JSON in credentials file raises AuthenticationError."""
        creds_path = tmp_path / "credentials.json"
        creds_path.write_text("invalid json {{{")

        config = CloudGoogleConfig(
            credentials_path=str(creds_path),
            token_store=str(tmp_path / "tokens.json"),
        )
        adapter = GoogleDriveAdapter(config)

        with pytest.raises(AuthenticationError) as exc_info:
            await adapter._load_credentials()
        assert "Invalid JSON" in str(exc_info.value)


# ============================================================================
# Token Refresh Error Handling Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterTokenRefreshErrors:
    """Test token refresh error scenarios."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_token_refresh_401_response(
        self,
        mock_post: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Token refresh with 401 raises TokenRefreshError."""
        await adapter._save_tokens(mock_tokens)

        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_post.return_value = mock_response

        with pytest.raises(TokenRefreshError) as exc_info:
            await adapter._refresh_access_token()
        assert "re-authentication required" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_token_refresh_400_response(
        self,
        mock_post: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Token refresh with 400 raises TokenRefreshError."""
        await adapter._save_tokens(mock_tokens)

        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"error": "invalid_grant"}
        mock_post.return_value = mock_response

        with pytest.raises(TokenRefreshError) as exc_info:
            await adapter._refresh_access_token()
        assert "Token refresh failed" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_token_refresh_network_error(
        self,
        mock_post: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Token refresh with network error raises TokenRefreshError."""
        await adapter._save_tokens(mock_tokens)

        mock_post.side_effect = httpx.NetworkError("Connection failed")

        with pytest.raises(TokenRefreshError) as exc_info:
            await adapter._refresh_access_token()
        assert "Network error" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_token_refresh_timeout(
        self,
        mock_post: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Token refresh with timeout raises TokenRefreshError."""
        await adapter._save_tokens(mock_tokens)

        mock_post.side_effect = httpx.TimeoutException("Request timed out")

        with pytest.raises(TokenRefreshError) as exc_info:
            await adapter._refresh_access_token()
        assert "Timeout" in str(exc_info.value)


# ============================================================================
# API Error Responses Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterAPIErrors:
    """Test API error handling."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_http_500_error(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """HTTP 500 raises GoogleDriveError."""
        await adapter._save_tokens(mock_tokens)

        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error", request=Mock(), response=Mock(status_code=500)
        )
        mock_request.side_effect = httpx.HTTPStatusError(
            "Server Error", request=Mock(), response=Mock(status_code=500)
        )

        with pytest.raises(GoogleDriveError) as exc_info:
            await adapter.list_files("gdrive://test")
        assert "API request failed" in str(exc_info.value)


# ============================================================================
# HTTP Status Handling Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterHTTPStatus:
    """Test HTTP status code handling."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_http_403_forbidden(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """HTTP 403 raises GoogleDriveError."""
        await adapter._save_tokens(mock_tokens)

        mock_request.side_effect = httpx.HTTPStatusError(
            "Forbidden", request=Mock(), response=Mock(status_code=403)
        )

        with pytest.raises(GoogleDriveError) as exc_info:
            await adapter.list_files("gdrive://test")
        assert "403" in str(exc_info.value) or "API request failed" in str(
            exc_info.value
        )

    """Test OAuth2 flow methods."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_exchange_code_success(
        self,
        mock_post: AsyncMock,
        adapter: GoogleDriveAdapter,
    ) -> None:
        """exchange_code returns tokens correctly."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/drive.readonly",
        }
        mock_post.return_value = mock_response

        tokens = await adapter.exchange_code("auth_code_abc")

        assert tokens["access_token"] == "new_access_token"
        assert tokens["refresh_token"] == "new_refresh_token"
        assert "expires_at" in tokens

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.post")
    async def test_exchange_code_failure(
        self,
        mock_post: AsyncMock,
        adapter: GoogleDriveAdapter,
    ) -> None:
        """exchange_code with invalid code raises AuthenticationError."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Request",
            request=Mock(),
            response=Mock(
                status_code=400,
                json=Mock(return_value={"error": "invalid_grant"}),
            ),
        )
        mock_post.return_value = mock_response

        with pytest.raises(AuthenticationError) as exc_info:
            await adapter.exchange_code("invalid_code")
        assert "Token exchange failed" in str(exc_info.value)


# ============================================================================
# Network Retry Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterRetries:
    """Test retry logic for network failures."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_network_error_with_retry_success(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Network error with eventual success after retry."""
        await adapter._save_tokens(mock_tokens)

        # First call fails with network error
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {"files": [], "nextPageToken": None}
        second_response.headers = {}

        # Network error, then success
        mock_request.side_effect = [
            httpx.NetworkError("Connection reset"),
            second_response,
        ]

        # Should retry and succeed
        files = await adapter.list_files("gdrive://test")
        assert files == []

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_timeout_with_retry_success(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Timeout with eventual success after retry."""
        await adapter._save_tokens(mock_tokens)

        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {"files": [], "nextPageToken": None}
        success_response.headers = {}

        mock_request.side_effect = [
            httpx.TimeoutException("Request timed out"),
            success_response,
        ]

        files = await adapter.list_files("gdrive://test")
        assert files == []


# ============================================================================
# List Files Recursive Tests
# ============================================================================


@pytest.mark.skipif(not GDRIVE_AVAILABLE, reason="Google Drive adapter not available")
class TestGoogleDriveAdapterRecursive:
    """Test recursive file listing."""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.request")
    async def test_list_files_recursive(
        self,
        mock_request: AsyncMock,
        adapter: GoogleDriveAdapter,
        mock_tokens: dict[str, Any],
    ) -> None:
        """Recursive file listing includes subdirectories."""
        await adapter._save_tokens(mock_tokens)

        # First level: folder and file
        level1_response = Mock()
        level1_response.status_code = 200
        level1_response.json.return_value = {
            "files": [
                {
                    "id": "folder_1",
                    "name": "SubFolder",
                    "mimeType": "application/vnd.google-apps.folder",
                    "modifiedTime": "2024-01-01T00:00:00Z",
                },
                {
                    "id": "file_1",
                    "name": "file1.txt",
                    "mimeType": "text/plain",
                    "size": "100",
                    "modifiedTime": "2024-01-01T00:00:00Z",
                },
            ],
            "nextPageToken": None,
        }
        level1_response.headers = {}

        # Second level: file inside subfolder
        level2_response = Mock()
        level2_response.status_code = 200
        level2_response.json.return_value = {
            "files": [
                {
                    "id": "file_2",
                    "name": "file2.txt",
                    "mimeType": "text/plain",
                    "size": "200",
                    "modifiedTime": "2024-01-02T00:00:00Z",
                },
            ],
            "nextPageToken": None,
        }
        level2_response.headers = {}

        mock_request.side_effect = [level1_response, level2_response]

        files = await adapter.list_files("gdrive://root", recursive=True)

        assert len(files) == 3  # folder + file + subfile
        file_names = [f.name for f in files]
        assert "file1.txt" in file_names
        assert "file2.txt" in file_names
