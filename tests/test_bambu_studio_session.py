from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from printing_agent.bambu_studio_session import (
    BambuStudioSessionAdapter,
    BambuStudioSessionError,
)

FORMAT_KEY = b"i4crL3LESLnWapLS"


def _write_session(
    config_dir: Path,
    *,
    country_code: str = "CN",
    login_status: int = 1,
    token: str = "synthetic-access-token",
    account: str = "13800123456",
) -> Path:
    document = {
        "country_code": country_code,
        "user": {
            "account": account,
            "login_status": login_status,
            "refresh_token": "synthetic-refresh-token",
            "token": token,
        },
    }
    plaintext = json.dumps(document).encode("utf-8")
    plaintext += b"\x00" * (-len(plaintext) % 16)
    encryptor = Cipher(algorithms.AES(FORMAT_KEY), modes.ECB()).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "BambuNetworkEngine.conf"
    path.write_bytes(ciphertext)
    return path


def _studio_executable(tmp_path: Path) -> Path:
    path = tmp_path / "Bambu Studio" / "bambu-studio.exe"
    path.parent.mkdir()
    path.write_bytes(b"synthetic executable")
    return path


def test_imports_china_session_without_writing_plaintext(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _write_session(config_dir)
    adapter = BambuStudioSessionAdapter(
        _studio_executable(tmp_path),
        config_dir,
        process_detector=lambda _: True,
    )

    session = adapter.read_signed_in_session()
    status = adapter.status()

    assert session.region == "china"
    assert session.account_hint == "***3456"
    assert session.access_token.get_secret_value() == "synthetic-access-token"
    assert status.signed_in is True
    assert status.running is True
    assert status.region == "china"
    assert sorted(item.name for item in config_dir.iterdir()) == [
        "BambuNetworkEngine.conf"
    ]


def test_nonempty_token_is_signed_in_even_when_studio_status_is_zero(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    _write_session(config_dir, login_status=0)
    adapter = BambuStudioSessionAdapter(None, config_dir)

    session = adapter.read_signed_in_session()

    assert session.region == "china"


def test_imports_global_session_and_masks_email(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _write_session(
        config_dir,
        country_code="US",
        account="operator@example.com",
    )
    adapter = BambuStudioSessionAdapter(None, config_dir)

    session = adapter.read_signed_in_session()

    assert session.region == "global"
    assert session.account_hint == "o***@example.com"


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda path: path.write_bytes(b"not-a-full-block"), "session_format_unsupported"),
        (
            lambda path: _write_session(path.parent, login_status=0, token=""),
            "not_signed_in",
        ),
        (
            lambda path: _write_session(path.parent, country_code="UNKNOWN"),
            "region_unsupported",
        ),
    ],
)
def test_rejects_invalid_or_logged_out_sessions(
    tmp_path: Path,
    mutate,
    expected_code: str,
) -> None:
    config_dir = tmp_path / "config"
    path = _write_session(config_dir)
    mutate(path)
    adapter = BambuStudioSessionAdapter(None, config_dir)

    with pytest.raises(BambuStudioSessionError) as caught:
        adapter.read_signed_in_session()

    assert caught.value.code == expected_code
    assert "token" not in caught.value.message.casefold()


def test_status_reports_missing_session_without_raising(tmp_path: Path) -> None:
    adapter = BambuStudioSessionAdapter(None, tmp_path / "missing")

    status = adapter.status()

    assert status.installed is False
    assert status.session_present is False
    assert status.signed_in is False
    assert status.error_code == "session_missing"


def test_open_uses_only_configured_executable(tmp_path: Path) -> None:
    executable = _studio_executable(tmp_path)
    launched: list[Path] = []
    adapter = BambuStudioSessionAdapter(
        executable,
        tmp_path / "config",
        process_detector=lambda _: False,
        launcher=launched.append,
    )

    status = adapter.open()

    assert launched == [executable]
    assert status.running is True


def test_open_rejects_unexpected_executable_name(tmp_path: Path) -> None:
    executable = tmp_path / "other.exe"
    executable.write_bytes(b"synthetic executable")
    adapter = BambuStudioSessionAdapter(executable, tmp_path / "config")

    with pytest.raises(BambuStudioSessionError) as caught:
        adapter.open()

    assert caught.value.code == "studio_not_installed"
