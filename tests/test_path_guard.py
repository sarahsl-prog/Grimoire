"""The shared server-path guard and the ``api.allowed_roots`` setting."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from grimoire.config.settings import APIConfig
from grimoire.utils.path_guard import MAX_PATH_LEN, PathNotAllowedError, resolve_allowed


class TestResolveAllowed:
    def test_a_path_inside_a_root_is_returned_resolved(self, tmp_path: Path) -> None:
        inside = tmp_path / "docs" / "a.txt"
        inside.parent.mkdir()
        inside.write_text("x")

        assert resolve_allowed(str(inside), [tmp_path]) == inside.resolve()

    def test_the_root_itself_is_allowed(self, tmp_path: Path) -> None:
        assert resolve_allowed(str(tmp_path), [tmp_path]) == tmp_path.resolve()

    def test_a_path_outside_every_root_is_forbidden(self, tmp_path: Path) -> None:
        with pytest.raises(PathNotAllowedError) as exc:
            resolve_allowed("/etc/passwd", [tmp_path])
        assert exc.value.forbidden is True

    def test_any_one_of_several_roots_is_enough(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        target = b / "x.txt"
        target.write_text("x")

        assert resolve_allowed(str(target), [a, b]) == target.resolve()

    def test_no_roots_means_nothing_is_allowed(self, tmp_path: Path) -> None:
        with pytest.raises(PathNotAllowedError):
            resolve_allowed(str(tmp_path), [])

    def test_dot_dot_cannot_climb_out(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()

        with pytest.raises(PathNotAllowedError):
            resolve_allowed(f"{root}/../../etc/passwd", [root])

    def test_a_symlink_out_of_the_root_is_followed_and_refused(
        self, tmp_path: Path
    ) -> None:
        root, outside = tmp_path / "root", tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("s")
        (root / "link").symlink_to(outside)

        with pytest.raises(PathNotAllowedError):
            resolve_allowed(str(root / "link" / "secret.txt"), [root])

    def test_a_sibling_sharing_a_name_prefix_is_not_inside(
        self, tmp_path: Path
    ) -> None:
        # "/x/data-evil" must not pass for a root of "/x/data".
        root, sibling = tmp_path / "data", tmp_path / "data-evil"
        root.mkdir()
        sibling.mkdir()

        with pytest.raises(PathNotAllowedError):
            resolve_allowed(str(sibling), [root])

    def test_a_root_that_is_itself_a_symlink_is_judged_by_where_it_points(
        self, tmp_path: Path
    ) -> None:
        real, alias = tmp_path / "real", tmp_path / "alias"
        real.mkdir()
        (real / "f.txt").write_text("x")
        os.symlink(real, alias)

        assert (
            resolve_allowed(str(real / "f.txt"), [alias]) == (real / "f.txt").resolve()
        )

    def test_a_null_byte_is_a_bad_request_not_forbidden(self, tmp_path: Path) -> None:
        with pytest.raises(PathNotAllowedError) as exc:
            resolve_allowed(f"{tmp_path}/a\x00b", [tmp_path])
        assert exc.value.forbidden is False

    def test_an_overlong_path_is_a_bad_request_not_forbidden(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PathNotAllowedError) as exc:
            resolve_allowed("/" + "a" * MAX_PATH_LEN, [tmp_path])
        assert exc.value.forbidden is False

    def test_the_message_never_echoes_the_rejected_path(self, tmp_path: Path) -> None:
        with pytest.raises(PathNotAllowedError) as exc:
            resolve_allowed("/etc/shadow", [tmp_path])
        assert "shadow" not in exc.value.message

    def test_the_message_names_the_allowed_roots(self, tmp_path: Path) -> None:
        with pytest.raises(PathNotAllowedError) as exc:
            resolve_allowed("/etc/passwd", [tmp_path])
        assert str(tmp_path.resolve()) in exc.value.message


class TestSetting:
    def test_the_default_is_tmp_only(self) -> None:
        assert APIConfig().allowed_roots == [Path("/tmp")]

    def test_roots_can_be_configured(self) -> None:
        config = APIConfig(allowed_roots=[Path("/data/watch"), Path("/tmp")])
        assert config.allowed_roots == [Path("/data/watch"), Path("/tmp")]

    def test_an_empty_list_is_allowed_and_means_none(self) -> None:
        assert APIConfig(allowed_roots=[]).allowed_roots == []

    @pytest.mark.parametrize("bad", ["relative/dir", "./here", "", "~/docs"])
    def test_a_relative_root_is_rejected(self, bad: str) -> None:
        # A relative root would silently depend on the server's working directory.
        with pytest.raises(ValidationError):
            APIConfig(allowed_roots=[Path(bad)])

    def test_the_filesystem_root_is_rejected(self) -> None:
        # Allowing "/" would defeat the allowlist while looking like a setting.
        with pytest.raises(ValidationError, match="filesystem root"):
            APIConfig(allowed_roots=[Path("/")])

    def test_an_env_var_json_list_is_understood(self, monkeypatch) -> None:
        from grimoire.config.settings import GrimoireSettings

        monkeypatch.setenv("GRIMOIRE_API__ALLOWED_ROOTS", '["/data/watch", "/tmp"]')
        assert GrimoireSettings().api.allowed_roots == [
            Path("/data/watch"),
            Path("/tmp"),
        ]
