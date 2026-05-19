"""Unit + integration coverage for ``khdp submissions`` subcommands."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from khdp.cli import main as cli_main
from khdp.cli_submissions import (
    _load_details_from_json,
    _load_details_from_md,
    _normalise_dir,
    _parse_ref,
    _split_dir_and_name,
)
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


def test_load_details_from_json(tmp_path: Path) -> None:
    p = tmp_path / "d.json"
    p.write_text(
        '[{"name": "Abstract", "content": "<p>a</p>"},'
        '{"name": "Methods", "content": "<p>m</p>"}]',
        encoding="utf-8",
    )
    out = _load_details_from_json(str(p))
    assert out == [
        {"name": "Abstract", "content": "<p>a</p>"},
        {"name": "Methods", "content": "<p>m</p>"},
    ]


def test_load_details_from_json_rejects_non_array(tmp_path: Path) -> None:
    p = tmp_path / "d.json"
    p.write_text('{"name": "x", "content": "y"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        _load_details_from_json(str(p))


def test_load_details_from_json_rejects_missing_keys(tmp_path: Path) -> None:
    p = tmp_path / "d.json"
    p.write_text('[{"name": "x"}]', encoding="utf-8")
    with pytest.raises(SystemExit):
        _load_details_from_json(str(p))


def test_load_details_from_md_splits_by_h1(tmp_path: Path) -> None:
    p = tmp_path / "d.md"
    p.write_text(
        "# Abstract\n"
        "first section body\n"
        "second line\n"
        "\n"
        "# Methods\n"
        "methods body\n",
        encoding="utf-8",
    )
    out = _load_details_from_md(str(p))
    assert out == [
        {"name": "Abstract", "content": "first section body\nsecond line"},
        {"name": "Methods", "content": "methods body"},
    ]


def test_load_details_from_md_no_sections(tmp_path: Path) -> None:
    p = tmp_path / "d.md"
    p.write_text("just a paragraph, no headings\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        _load_details_from_md(str(p))


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


def test_submissions_create_no_input_with_all_flags(
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
        "submissions", "create", "--no-input",
        "--title", "T", "--code", "MY-DS",
        "--license-id", "1", "--summary", "S",
    ])
    assert rc == 0
    assert '"code": "MY-DS"' in capsys.readouterr().out


def test_submissions_create_no_input_missing_flag_fails(
    env: None, capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main([
            "submissions", "create", "--no-input",
            "--title", "T",  # missing code / license-id / summary
        ])
    assert "missing required field" in str(exc.value)


def test_submissions_create_interactive_prompts_for_missing(
    env: None, httpx_mock: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Pretend stdin is a TTY so the create command enters interactive mode.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    # Sequence of "user keystrokes" for: title, code, version (accept default),
    # license id, summary, policy (accept default), confirm (Y).
    responses = iter([
        "Interactive Title",     # title
        "INT-DS",                # code
        "",                       # version → accept default 1.0.0
        "7",                      # license id
        "an interactive summary", # summary
        "",                       # policy → accept default open
        "y",                      # confirm
    ])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))

    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions",
        method="POST",
        match_json={
            "title": "Interactive Title", "version": "1.0.0", "lId": 7,
            "code": "INT-DS", "summary": "an interactive summary",
            "accessPolicy": "open",
        },
        json={"code": "INT-DS", "version": "1.0.0", "status": 0, "title": "Interactive Title"},
    )

    rc = cli_main(["submissions", "create"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "About to create:" in out
    assert '"code": "INT-DS"' in out


def test_submissions_create_interactive_abort_on_no(
    env: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    responses = iter([
        "T", "C", "", "1", "S", "", "n",   # confirm = n → abort
    ])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))

    rc = cli_main(["submissions", "create"])
    assert rc == 1
    assert "aborted" in capsys.readouterr().out


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


def test_submissions_create_with_details_md(
    env: None, httpx_mock: Any, tmp_path: Path,
) -> None:
    md = tmp_path / "body.md"
    md.write_text(
        "# Abstract\nthis is the abstract\n\n# Methods\nthe methods\n",
        encoding="utf-8",
    )
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions",
        method="POST",
        match_json={
            "title": "T", "version": "1.0.0", "lId": 1,
            "code": "MY-DS", "summary": "S", "accessPolicy": "open",
            "details": [
                {"name": "Abstract", "content": "this is the abstract"},
                {"name": "Methods", "content": "the methods"},
            ],
        },
        json={"code": "MY-DS", "version": "1.0.0", "status": 0, "title": "T"},
    )
    rc = cli_main([
        "submissions", "create", "--no-input",
        "--title", "T", "--code", "MY-DS",
        "--license-id", "1", "--summary", "S",
        "--details-md", str(md),
    ])
    assert rc == 0


def test_submissions_update_only_changed_fields(
    env: None, httpx_mock: Any,
) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/MY-DS/1.0.0",
        method="PATCH",
        match_json={"title": "new title", "summary": "new summary"},
        json={
            "code": "MY-DS", "version": "1.0.0", "status": 0,
            "title": "new title",
        },
    )
    rc = cli_main([
        "submissions", "update", "MY-DS",
        "--title", "new title",
        "--summary", "new summary",
    ])
    assert rc == 0


def test_submissions_update_with_details_file(
    env: None, httpx_mock: Any, tmp_path: Path,
) -> None:
    p = tmp_path / "d.json"
    p.write_text(
        '[{"name": "Abstract", "content": "<p>x</p>"}]',
        encoding="utf-8",
    )
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/MY-DS/1.0.0",
        method="PATCH",
        match_json={
            "details": [{"name": "Abstract", "content": "<p>x</p>"}],
        },
        json={"code": "MY-DS", "version": "1.0.0", "status": 0, "title": "T"},
    )
    rc = cli_main([
        "submissions", "update", "MY-DS",
        "--details-file", str(p),
    ])
    assert rc == 0


def test_submissions_update_requires_at_least_one_field(env: None) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main(["submissions", "update", "MY-DS"])
    assert "nothing to update" in str(exc.value)


def test_submissions_submit(env: None, httpx_mock: Any) -> None:
    httpx_mock.add_response(
        url=f"{_API}/open/dataset-submissions/MY-DS/1.0.0/submit",
        method="POST",
        json={"code": "MY-DS", "version": "1.0.0", "status": 1},
    )
    rc = cli_main(["submissions", "submit", "MY-DS"])
    assert rc == 0
