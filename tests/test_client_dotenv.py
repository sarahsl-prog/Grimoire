"""``ClientConfig.from_env`` reading a ``.env`` file (B7).

Every test passes an explicit ``dotenv_path`` (or ``chdir``s into ``tmp_path``)
so none of them depends on the repository's own ``.env``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from loguru import logger

from grimoire.client import config as config_module
from grimoire.client.config import DEFAULT_BASE_URL, ClientConfig

KEY = "grim_dvl_from_dotenv_1234567890"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestPrecedence:
    def test_dotenv_beats_the_default(self, tmp_path: Path) -> None:
        f = _write(
            tmp_path / ".env",
            f"GRIMOIRE_API_URL=http://box:9000\nGRIMOIRE_API_KEY={KEY}\n",
        )
        cfg = ClientConfig.from_env({}, dotenv_path=f)
        assert (cfg.base_url, cfg.api_key) == ("http://box:9000", KEY)

    def test_real_environment_beats_dotenv(self, tmp_path: Path) -> None:
        f = _write(
            tmp_path / ".env",
            "GRIMOIRE_API_URL=http://file:1\nGRIMOIRE_API_KEY=from-file\n",
        )
        env = {"GRIMOIRE_API_URL": "http://env:2", "GRIMOIRE_API_KEY": "from-env"}
        cfg = ClientConfig.from_env(env, dotenv_path=f)
        assert (cfg.base_url, cfg.api_key) == ("http://env:2", "from-env")

    def test_each_key_is_resolved_independently(self, tmp_path: Path) -> None:
        f = _write(
            tmp_path / ".env",
            "GRIMOIRE_API_URL=http://file:1\nGRIMOIRE_API_KEY=from-file\n",
        )
        cfg = ClientConfig.from_env({"GRIMOIRE_API_KEY": "from-env"}, dotenv_path=f)
        assert (cfg.base_url, cfg.api_key) == ("http://file:1", "from-env")

    def test_blank_environment_value_counts_as_absent(self, tmp_path: Path) -> None:
        f = _write(tmp_path / ".env", "GRIMOIRE_API_KEY=from-file\n")
        cfg = ClientConfig.from_env({"GRIMOIRE_API_KEY": "   "}, dotenv_path=f)
        assert cfg.api_key == "from-file"

    def test_blank_dotenv_value_counts_as_absent(self, tmp_path: Path) -> None:
        f = _write(tmp_path / ".env", "GRIMOIRE_API_KEY=\nGRIMOIRE_API_URL=  \n")
        cfg = ClientConfig.from_env({}, dotenv_path=f)
        assert cfg.api_key is None
        assert cfg.base_url == DEFAULT_BASE_URL

    def test_quoted_whitespace_only_value_counts_as_absent(
        self, tmp_path: Path
    ) -> None:
        """Quotes protect the spaces from the parser, not from our own check."""
        f = _write(tmp_path / ".env", 'GRIMOIRE_API_KEY="   "\n')
        assert ClientConfig.from_env({}, dotenv_path=f).api_key is None

    def test_trailing_slash_is_stripped_from_a_dotenv_url(self, tmp_path: Path) -> None:
        f = _write(tmp_path / ".env", "GRIMOIRE_API_URL=http://box:9000/\n")
        assert ClientConfig.from_env({}, dotenv_path=f).base_url == "http://box:9000"


class TestWhichKeysAreRead:
    def test_other_keys_are_ignored_and_not_exported(self, tmp_path: Path) -> None:
        f = _write(
            tmp_path / ".env",
            "POSTGRES_PASSWORD=hunter2\nGRIMOIRE_API_KEY=k\nOTHER=1\n",
        )
        before = dict(os.environ)
        cfg = ClientConfig.from_env({}, dotenv_path=f)
        assert cfg.api_key == "k"
        assert dict(os.environ) == before  # nothing leaked into the process

    def test_the_reader_returns_only_the_two_client_settings(
        self, tmp_path: Path
    ) -> None:
        f = _write(
            tmp_path / ".env",
            "POSTGRES_PASSWORD=hunter2\nGRIMOIRE_API_KEY=k\nGRIMOIRE_API_URL=u\n",
        )
        assert config_module._read_dotenv(f) == {
            "GRIMOIRE_API_KEY": "k",
            "GRIMOIRE_API_URL": "u",
        }

    def test_shell_style_syntax_is_understood(self, tmp_path: Path) -> None:
        f = _write(
            tmp_path / ".env",
            "# a comment\nexport GRIMOIRE_API_KEY='quoted-key'\n"
            'GRIMOIRE_API_URL="http://box:9000"\n',
        )
        cfg = ClientConfig.from_env({}, dotenv_path=f)
        assert (cfg.base_url, cfg.api_key) == ("http://box:9000", "quoted-key")

    def test_variables_are_not_interpolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``${X}`` must stay literal: expanding it would read the environment."""
        monkeypatch.setenv("SECRET_ELSEWHERE", "leaked")
        f = _write(tmp_path / ".env", "GRIMOIRE_API_KEY=pre-${SECRET_ELSEWHERE}\n")
        assert ClientConfig.from_env({}, dotenv_path=f).api_key == (
            "pre-${SECRET_ELSEWHERE}"
        )


class TestBadFiles:
    def test_missing_file_is_fine(self, tmp_path: Path) -> None:
        cfg = ClientConfig.from_env({}, dotenv_path=tmp_path / "nope.env")
        assert (cfg.base_url, cfg.api_key) == (DEFAULT_BASE_URL, None)

    def test_a_directory_is_ignored(self, tmp_path: Path) -> None:
        cfg = ClientConfig.from_env({}, dotenv_path=tmp_path)
        assert cfg.api_key is None

    def test_malformed_lines_do_not_break_the_good_ones(self, tmp_path: Path) -> None:
        f = _write(
            tmp_path / ".env",
            "this is not an assignment\n=novalue\nGRIMOIRE_API_KEY=ok\n\x00junk\n",
        )
        assert ClientConfig.from_env({}, dotenv_path=f).api_key == "ok"

    def test_undecodable_file_is_ignored(self, tmp_path: Path) -> None:
        f = tmp_path / ".env"
        f.write_bytes(b"GRIMOIRE_API_KEY=\xff\xfe\xfa\n")
        cfg = ClientConfig.from_env({"GRIMOIRE_API_KEY": "env"}, dotenv_path=f)
        assert cfg.api_key == "env"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
    def test_unreadable_file_is_ignored(self, tmp_path: Path) -> None:
        f = _write(tmp_path / ".env", "GRIMOIRE_API_KEY=k\n")
        f.chmod(0)
        try:
            assert ClientConfig.from_env({}, dotenv_path=f).api_key is None
        finally:
            f.chmod(0o600)


class TestSecretsStayOutOfLogs:
    def test_no_value_is_ever_logged(self, tmp_path: Path) -> None:
        lines: list[str] = []
        hid = logger.add(lines.append, level="DEBUG", format="{message}")
        try:
            f = _write(
                tmp_path / ".env",
                f"GRIMOIRE_API_KEY={KEY}\nGRIMOIRE_API_URL=http://secret-host:1\n",
            )
            ClientConfig.from_env({}, dotenv_path=f)
            ClientConfig.from_env({}, dotenv_path=tmp_path / "missing")
            bad = tmp_path / "bad.env"
            bad.write_bytes(b"GRIMOIRE_API_KEY=\xff\xfe\n")
            ClientConfig.from_env({}, dotenv_path=bad)
        finally:
            logger.remove(hid)
        joined = "\n".join(lines)
        assert KEY not in joined
        assert "secret-host" not in joined

    def test_a_failure_message_is_not_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception's text can quote the file, so only its class is logged."""

        def boom(*_a: object, **_k: object) -> dict[str, str]:
            raise OSError("GRIMOIRE_API_KEY=leaked-in-the-message")

        monkeypatch.setattr(config_module, "dotenv_values", boom)
        lines: list[str] = []
        hid = logger.add(lines.append, level="DEBUG", format="{message}")
        try:
            assert config_module._read_dotenv(tmp_path / ".env") == {}
        finally:
            logger.remove(hid)
        assert lines  # it was reported...
        assert "leaked-in-the-message" not in "\n".join(lines)  # ...without the text


class TestDefaultLookup:
    """Where the file is looked for when nothing says otherwise."""

    def test_process_environment_reads_dotenv_in_the_current_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path / ".env", "GRIMOIRE_API_KEY=from-cwd\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GRIMOIRE_API_KEY", raising=False)
        monkeypatch.delenv("GRIMOIRE_API_URL", raising=False)
        assert ClientConfig.from_env().api_key == "from-cwd"

    def test_process_environment_still_wins_over_the_cwd_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path / ".env", "GRIMOIRE_API_KEY=from-cwd\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GRIMOIRE_API_KEY", "from-process")
        assert ClientConfig.from_env().api_key == "from-process"

    def test_an_explicit_mapping_never_reads_the_cwd_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So tests (and callers) passing ``env={}`` stay hermetic."""
        _write(tmp_path / ".env", "GRIMOIRE_API_KEY=from-cwd\n")
        monkeypatch.chdir(tmp_path)
        assert ClientConfig.from_env({}).api_key is None

    def test_no_dotenv_in_the_current_directory_is_fine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GRIMOIRE_API_KEY", raising=False)
        assert ClientConfig.from_env().api_key is None


class TestRepr:
    def test_repr_does_not_show_the_api_key(self, tmp_path: Path) -> None:
        """A logged or pasted config object must not carry the secret."""
        f = _write(tmp_path / ".env", f"GRIMOIRE_API_KEY={KEY}\n")
        cfg = ClientConfig.from_env({}, dotenv_path=f)
        assert cfg.api_key == KEY
        assert KEY not in repr(cfg)
        assert KEY not in str(cfg)
