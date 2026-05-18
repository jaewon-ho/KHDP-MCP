"""Unit + integration coverage for ``khdp submissions`` subcommands."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from khdp.cli import main as cli_main
from khdp.cli_submissions import _normalise_dir, _parse_ref, _split_dir_and_name
from khdp.oauth import TokenSet

# ── pure helpers ──────────────────────────────────────────────────────


def test_parse_ref_defaults_to_1_0_0() -> None:
    assert _parse_ref("MY-DS") == ("MY-DS", "1.0.0")
    assert _parse_ref("MY-DS@") == ("MY-DS", "1.0.0")


def test_parse_ref_explicit_version() -> None:
    assert _parse_ref("MY-DS@2.3.4") == ("MY-DS", "2.3.4")


def test_parse_ref_empty_raises() -> None:
    with pytest.raises(SystemExit):
        _parse_ref("")
    with pytest.raises(SystemExit):
        _parse_ref("@1.0.0")


def test_split_dir_and_name() -> None:
    assert _split_dir_and_name("/imaging") == ("/", "imaging")
    assert _split_dir_and_name("/imaging/scans") == ("/imaging", "scans")
    assert _split_dir_and_name("imaging/scans/2026") == (
        "/imaging/scans", "2026",
    )


def test_split_dir_and_name_trims_trailing_slash() -> None:
    # Trailing slash is silently normalised; the path still has a leaf.
    assert _split_dir_and_name("/imaging/") == ("/", "imaging")


def test_normalise_dir() -> None:
    assert _normalise_dir("/") == "/"
    assert _normalise_dir("") == "/"
    assert _normalise_dir("imaging") == "/imaging"
    assert _normalise_dir("/imaging") == "/imaging"
    assert _normalise_dir("/imaging/") == "/imaging"


# ── CLI integration ──────────────────────────────────────────────────


_APP_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_API = "https://api.example/_api"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KHDP_APP_ID", _APP_ID)
    monkeypatch.setenv("KHDP_API_BASE", _API)
    monkeypatch.setenv("KHDP_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("KHDP_USE_KEYRING", "0")
    from khdp.token_store import TokenStore
    TokenStore(tmp_path, use_keyring=False).save(TokenSet(
        access_token="AT", refresh_token="RT",
        expires_at=time.time() + 3600, app_id=_APP_ID,
    ))


def test_submissions_list_table(
    env: None, httpx_mock: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions?page=1&limit=10",
        method="GET",
        json={
            "totalCnt": 1,
            "data": [{
                "code": "MY-DS", "version": "1.0.0",
                "cvStatus": 0, "ciTitle": "My dataset",
            }],
        },
    )
    rc = cli_main(["submissions", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "MY-DS" in out
    assert "My dataset" in out
    assert "total 1" in out


def test_submissions_licenses_table(
    env: None, httpx_mock: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/licenses",
        method="GET",
        json=[
            {"lId": 1, "lCode": "CC-BY-4.0", "lName": "Creative Commons BY 4.0", "lLink": "..."},
            {"lId": 2, "lCode": "ODC-BY", "lName": "Open Data Commons BY", "lLink": "..."},
        ],
    )
    rc = cli_main(["submissions", "licenses"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "CC-BY-4.0" in out
    assert "Creative Commons BY 4.0" in out


def test_submissions_licenses_json(
    env: None, httpx_mock: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/licenses",
        method="GET",
        json=[{"lId": 1, "lCode": "CC0", "lName": "CC0 1.0", "lLink": "..."}],
    )
    rc = cli_main(["submissions", "licenses", "--json"])
    assert rc == 0
    assert '"lId": 1' in capsys.readouterr().out


def test_submissions_show_defaults_to_at_1_0_0(
    env: None, httpx_mock: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/MY-DS/1.0.0",
        method="GET",
        json={"code": "MY-DS", "version": "1.0.0", "cvStatus": 0},
    )
    rc = cli_main(["submissions", "show", "MY-DS"])
    assert rc == 0
    assert '"code": "MY-DS"' in capsys.readouterr().out


def test_submissions_create_posts_body(
    env: None, httpx_mock: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions",
        method="POST",
        match_json={
            "title": "T", "version": "1.0.0", "lId": 1,
            "code": "MY-DS", "summary": "S", "accessPolicy": "open",
        },
        json={
            "code": "MY-DS", "version": "1.0.0",
            "status": 0, "title": "T",
        },
    )
    rc = cli_main([
        "submissions", "create",
        "--title", "T", "--code", "MY-DS",
        "--license-id", "1", "--summary", "S",
    ])
    assert rc == 0
    assert '"code": "MY-DS"' in capsys.readouterr().out


def test_submissions_mkdir_splits_path(
    env: None, httpx_mock: Any,
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/MY-DS/1.0.0/files/directory",
        method="POST",
        match_json={"currentPath": "/imaging", "name": "scans"},
        json={"code": "MY-DS", "version": "1.0.0", "path": "imaging/scans"},
    )
    rc = cli_main([
        "submissions", "mkdir", "MY-DS",
        "--path", "/imaging/scans",
    ])
    assert rc == 0


def test_submissions_upload_two_step_flow(
    env: None, httpx_mock: Any, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1) presigned-url POST
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/MY-DS/1.0.0/files/presigned-url",
        method="POST",
        match_json={"currentPath": "/imaging", "name": "scan.dcm"},
        json={
            "code": "MY-DS", "version": "1.0.0",
            "path": "imaging", "name": "scan.dcm",
            "uploadUrl": "https://s3.example/signed",
        },
    )
    # 2) the actual upload PUT
    httpx_mock.add_response(
        url="https://s3.example/signed",
        method="PUT",
        status_code=200,
    )

    local = tmp_path / "scan.dcm"
    local.write_bytes(b"x" * 256)

    rc = cli_main([
        "submissions", "upload", "MY-DS",
        str(local),
        "--to", "/imaging",
    ])
    assert rc == 0
    err = capsys.readouterr().err
    assert "uploading scan.dcm" in err
    assert "→ /imaging/scan.dcm" in err


def test_submissions_list_files_path(env: None, httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/MY-DS/1.0.0/files?path=%2Fimaging",
        method="GET",
        json={
            "currentPath": "imaging/",
            "subDirs": [],
            "contents": [
                {"key": "imaging/a.dcm", "size": 1024},
            ],
        },
    )
    rc = cli_main([
        "submissions", "list-files", "MY-DS",
        "--path", "/imaging",
    ])
    assert rc == 0


def test_submissions_delete_passes_key(env: None, httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/MY-DS/1.0.0/files?key=imaging%2Fa.dcm",
        method="DELETE",
        json={"code": "MY-DS", "version": "1.0.0", "key": "imaging/a.dcm"},
    )
    rc = cli_main([
        "submissions", "delete", "MY-DS",
        "--key", "imaging/a.dcm",
    ])
    assert rc == 0


def test_submissions_submit(env: None, httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/MY-DS/1.0.0/submit",
        method="POST",
        json={"code": "MY-DS", "version": "1.0.0", "status": 1},
    )
    rc = cli_main(["submissions", "submit", "MY-DS"])
    assert rc == 0
