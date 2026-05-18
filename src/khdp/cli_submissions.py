"""``khdp submissions`` subcommand group.

Wraps ``/open/dataset-submissions/*`` endpoints. Requires a cached
OAuth user token (``khdp login``) -- the submission area is OAuth-only
on the backend.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx

from khdp.cli_datasets import _check_response, _emit, _fmt_size, _try_json
from khdp.session import Session

_POLICY_CHOICES = ["open", "restricted", "credentialed", "contributor_review"]


# ── argparse wiring ───────────────────────────────────────────────────


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("submissions", help="own dataset submission operations")
    sp = p.add_subparsers(dest="submissions_command", required=True)

    p_list = sp.add_parser("list", help="list my dataset submissions")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--limit", type=int, default=10)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_show = sp.add_parser("show", help="show one submission detail")
    p_show.add_argument("ref", help="<code>[@<version>] (defaults to @1.0.0)")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=_cmd_show)

    p_create = sp.add_parser("create", help="create a new dataset submission")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--code", required=True)
    p_create.add_argument(
        "--version", default="1.0.0", help="semver, default 1.0.0",
    )
    p_create.add_argument(
        "--license-id", type=int, required=True, dest="license_id",
        help="license ID from `khdp api GET /open/dataset-submissions/licenses`",
    )
    p_create.add_argument("--summary", required=True)
    p_create.add_argument(
        "--policy", choices=_POLICY_CHOICES, default="open",
        help="access policy (default: open)",
    )
    p_create.set_defaults(func=_cmd_create)

    p_mkdir = sp.add_parser("mkdir", help="create a directory in a submission")
    p_mkdir.add_argument("ref")
    p_mkdir.add_argument(
        "--path", required=True,
        help="absolute directory path inside the submission, e.g. /imaging",
    )
    p_mkdir.set_defaults(func=_cmd_mkdir)

    p_upload = sp.add_parser("upload", help="upload a local file to a submission")
    p_upload.add_argument("ref")
    p_upload.add_argument("local", help="local file path to upload")
    p_upload.add_argument(
        "--to", default="/",
        help="remote directory inside the submission (default: /)",
    )
    p_upload.add_argument(
        "--name",
        help="remote filename (default: local basename)",
    )
    p_upload.set_defaults(func=_cmd_upload)

    p_list_files = sp.add_parser(
        "list-files", help="list files in a submission",
    )
    p_list_files.add_argument("ref")
    p_list_files.add_argument(
        "--path", default="/", help="directory path (default: /)",
    )
    p_list_files.add_argument("--json", action="store_true")
    p_list_files.set_defaults(func=_cmd_list_files)

    p_delete = sp.add_parser(
        "delete", help="delete a file from a submission",
    )
    p_delete.add_argument("ref")
    p_delete.add_argument(
        "--key", required=True,
        help="file key relative to the submission root, e.g. imaging/scan.dcm",
    )
    p_delete.set_defaults(func=_cmd_delete)

    p_submit = sp.add_parser(
        "submit",
        help="finalise a submission (Writing → AuthorReview / AdminReview)",
    )
    p_submit.add_argument("ref")
    p_submit.set_defaults(func=_cmd_submit)


# ── helpers ───────────────────────────────────────────────────────────


def _parse_ref(ref: str) -> tuple[str, str]:
    """Parse a submission ref ``<code>[@<version>]``.

    Defaults to ``@1.0.0`` -- the canonical first-version for a freshly
    created submission. ``latest`` is intentionally not used here
    because submissions can be in non-published states.
    """
    code, _, version = ref.partition("@")
    if not code:
        raise SystemExit(f"[khdp] submission ref is empty: {ref!r}")
    return code, version or "1.0.0"


def _split_dir_and_name(path: str) -> tuple[str, str]:
    """Split ``/foo/bar/baz`` into ``('/foo/bar', 'baz')``.

    The KHDP ``files/directory`` endpoint expects ``currentPath`` (the
    parent) + ``name`` (the leaf), not a full path.
    """
    normalised = "/" + path.strip("/")
    parent, _, name = normalised.rpartition("/")
    if not name:
        raise SystemExit(f"[khdp] path has no trailing segment: {path!r}")
    return (parent or "/"), name


def _normalise_dir(p: str) -> str:
    """Trim trailing slashes; preserve a single root '/'."""
    stripped = p.strip("/")
    return "/" + stripped if stripped else "/"


# ── pretty printers ───────────────────────────────────────────────────


def _print_submission_list(body: Any) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        _emit(body)
        return
    items = body["data"]
    if not items:
        print("(no submissions)")
        return
    print(f"{'code':<24}  {'version':<10}  {'status':<6}  title")
    print(f"{'-' * 24}  {'-' * 10}  {'-' * 6}  -----")
    for it in items:
        print(
            f"{it.get('code', it.get('ciCode', '')):<24}  "
            f"{it.get('version', ''):<10}  "
            f"{it.get('cvStatus', ''):<6}  "
            f"{it.get('ciTitle', it.get('title', ''))}"
        )
    total = body.get("totalCnt")
    if total is not None:
        print(f"\ntotal {total}")


def _print_files(body: Any) -> None:
    if not isinstance(body, dict):
        _emit(body)
        return
    sub_dirs = body.get("subDirs") or []
    contents = body.get("contents") or []
    for d in sub_dirs:
        print(f"D  {d.get('key', '')}")
    for f in contents:
        size = int(f.get("size") or 0)
        print(f"F  {f.get('key', '')}  ({_fmt_size(size)})")
    if not sub_dirs and not contents:
        print("(empty)")


# ── commands ──────────────────────────────────────────────────────────


def _cmd_list(session: Session, args: argparse.Namespace) -> int:
    resp = session.authed_request(
        "GET",
        "/open/dataset-submissions",
        params={"page": str(args.page), "limit": str(args.limit)},
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    if args.json:
        _emit(body)
    else:
        _print_submission_list(body)
    return 0


def _cmd_show(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    resp = session.authed_request(
        "GET", f"/open/dataset-submissions/{code}/{version}",
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    _emit(body)
    return 0


def _cmd_create(session: Session, args: argparse.Namespace) -> int:
    body_req = {
        "title": args.title,
        "version": args.version,
        "lId": args.license_id,
        "code": args.code,
        "summary": args.summary,
        "accessPolicy": args.policy,
    }
    resp = session.authed_request(
        "POST", "/open/dataset-submissions", json=body_req,
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    _emit(body)
    return 0


def _cmd_mkdir(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    current_path, name = _split_dir_and_name(args.path)
    resp = session.authed_request(
        "POST",
        f"/open/dataset-submissions/{code}/{version}/files/directory",
        json={"currentPath": current_path, "name": name},
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    _emit(body)
    return 0


def _cmd_upload(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    local_path = Path(args.local).expanduser().resolve()
    if not local_path.is_file():
        raise SystemExit(f"[khdp] local file not found: {local_path}")

    name = args.name or local_path.name
    current_path = _normalise_dir(args.to)

    # 1) presigned URL 발급
    resp = session.authed_request(
        "POST",
        f"/open/dataset-submissions/{code}/{version}/files/presigned-url",
        json={"currentPath": current_path, "name": name},
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    upload_url = body.get("uploadUrl") if isinstance(body, dict) else None
    if not upload_url:
        print("[khdp] presigned-url response missing uploadUrl", file=sys.stderr)
        _emit(body)
        return 1

    # 2) PUT 본문
    size = local_path.stat().st_size
    remote = (
        f"{current_path}/{name}" if current_path != "/" else f"/{name}"
    )
    print(
        f"[khdp] uploading {local_path.name} ({_fmt_size(size)}) → {remote}",
        file=sys.stderr,
    )
    with local_path.open("rb") as fh, httpx.Client(timeout=600.0) as client:
        put_resp = client.put(upload_url, content=fh.read())
    if put_resp.status_code not in (200, 204):
        print(
            f"[khdp] PUT failed: {put_resp.status_code} "
            f"{put_resp.text[:200]}",
            file=sys.stderr,
        )
        return 1
    print(f"[khdp] uploaded {_fmt_size(size)}", file=sys.stderr)
    return 0


def _cmd_list_files(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    resp = session.authed_request(
        "GET",
        f"/open/dataset-submissions/{code}/{version}/files",
        params={"path": args.path},
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    if args.json:
        _emit(body)
    else:
        _print_files(body)
    return 0


def _cmd_delete(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    resp = session.authed_request(
        "DELETE",
        f"/open/dataset-submissions/{code}/{version}/files",
        params={"key": args.key},
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    _emit(body)
    return 0


def _cmd_submit(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    resp = session.authed_request(
        "POST",
        f"/open/dataset-submissions/{code}/{version}/submit",
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    _emit(body)
    return 0
