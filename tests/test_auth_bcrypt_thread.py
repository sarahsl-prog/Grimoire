"""bcrypt verification must not run on the event loop thread.

bcrypt is deliberately slow; on the loop it stalled every other request
(SSE streams included) for each authenticated call.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from grimoire.api.auth import authenticate_api_key
from grimoire.db.models import ApiKey, ApiKeyTier


async def test_checkpw_runs_in_a_worker_thread() -> None:
    key = ApiKey(
        id="k",
        name="t",
        tier=ApiKeyTier.READ,
        key_prefix="grim_rdl_abc",
        key_hash="$2b$12$hash",
        created_at=datetime.now(UTC),
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = key
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def _checkpw(raw: bytes, hashed: bytes) -> bool:
        seen.append(threading.current_thread())
        return True

    with patch("grimoire.api.auth.bcrypt.checkpw", _checkpw):
        assert await authenticate_api_key("grim_rdl_abc_secret", db) is key

    assert seen and seen[0] is not loop_thread
