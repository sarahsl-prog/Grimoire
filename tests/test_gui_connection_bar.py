"""Tests for the ConnectionBar widget."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from grimoire.gui.widgets.connection_bar import ConnectionBar  # noqa: E402


class TestKeyFieldStaysVisible:
    """A rejected/stale API key must be replaceable without restarting.

    Before this fix, set_state(configured=True) hid both the key field and
    the Use-key button - but `configured` means only bool(api_key); nothing
    has verified the key works. That left no way to paste a working key
    once the one loaded from GRIMOIRE_API_KEY started failing.
    """

    def test_key_field_visible_when_configured(self, qtbot) -> None:
        bar = ConnectionBar()
        qtbot.addWidget(bar)

        bar.set_state(configured=True, base_url="http://api:8001")

        # isVisible() also reflects whether the top-level window is shown,
        # which this test never does; isHidden() reports only whether
        # something explicitly hid THIS widget (the old `setVisible(False)`
        # call this fix removes), independent of that.
        assert not bar._key_field.isHidden()
        assert not bar._apply.isHidden()

    def test_key_field_visible_when_unconfigured(self, qtbot) -> None:
        bar = ConnectionBar()
        qtbot.addWidget(bar)

        bar.set_state(configured=False, base_url="http://api:8001")

        assert not bar._key_field.isHidden()
        assert not bar._apply.isHidden()


class TestStatusTextDoesNotOverclaim:
    def test_configured_alone_does_not_say_connected(self, qtbot) -> None:
        """ "Connected" must be reserved for a confirmed health check."""
        bar = ConnectionBar()
        qtbot.addWidget(bar)

        bar.set_state(configured=True, base_url="http://api:8001")

        assert "Connected" not in bar.status_text()
        assert "http://api:8001" in bar.status_text()

    def test_health_check_success_says_connected(self, qtbot) -> None:
        bar = ConnectionBar()
        qtbot.addWidget(bar)
        bar.set_state(configured=True, base_url="http://api:8001")

        bar.set_health(True)

        assert "Connected to http://api:8001" in bar.status_text()

    def test_health_check_failure_says_cannot_reach(self, qtbot) -> None:
        bar = ConnectionBar()
        qtbot.addWidget(bar)
        bar.set_state(configured=True, base_url="http://api:8001")

        bar.set_health(False)

        assert "Cannot reach http://api:8001" in bar.status_text()

    def test_unconfigured_state_names_the_env_var(self, qtbot) -> None:
        bar = ConnectionBar()
        qtbot.addWidget(bar)

        bar.set_state(configured=False, base_url="http://api:8001")

        assert "GRIMOIRE_API_KEY" in bar.status_text()

    def test_new_set_state_call_clears_a_stale_health_result(self, qtbot) -> None:
        """A key/URL change must not keep showing a prior check's verdict."""
        bar = ConnectionBar()
        qtbot.addWidget(bar)
        bar.set_state(configured=True, base_url="http://api:8001")
        bar.set_health(True)
        assert "Connected" in bar.status_text()

        bar.set_state(configured=True, base_url="http://other:9000")

        assert "Connected" not in bar.status_text()
