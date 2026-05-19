"""``khdp submissions`` subcommand group.

Wraps ``/open/dataset-submissions/*`` endpoints. Requires a cached
OAuth user token (``khdp login``) -- the submission area is OAuth-only
on the backend.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from khdp.cli_datasets import _check_response, _emit, _fmt_size, _try_json
from khdp.session import Session

_POLICY_CHOICES = ["open", "restricted", "credentialed", "contributor_review"]


# ── prompt helpers ────────────────────────────────────────────────────


def _prompt(
    label: str,
    *,
    default: str | None = None,
    required: bool = False,
) -> str | None:
    """Read one line from stdin. ``required`` re-prompts until non-empty."""
    suffix = f" ({default})" if default is not None else ""
    while True:
        try:
            raw = input(f"{label}{suffix}: ").strip()
        except EOFError as exc:
            raise SystemExit(
                "\n[khdp] interactive input ended unexpectedly"
            ) from exc
        if raw:
            return raw
        if default is not None:
            return default
        if not required:
            return None
        print("[khdp] this field is required")


def _prompt_int(
    label: str,
    *,
    default: int | None = None,
    required: bool = False,
) -> int | None:
    while True:
        raw = _prompt(
            label,
            default=None if default is None else str(default),
            required=required,
        )
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            print("[khdp] please enter an integer")


def _prompt_choice(
    label: str,
    choices: list[str],
    *,
    default: str | None = None,
) -> str:
    options = "/".join(c.upper() if c == default else c for c in choices)
    while True:
        raw = _prompt(f"{label} [{options}]", default=default)
        if raw is None:
            # default was None and user gave nothing; loop until one is chosen
            print(f"[khdp] choose one of: {', '.join(choices)}")
            continue
        if raw in choices:
            return raw
        print(f"[khdp] choose one of: {', '.join(choices)}")


def _confirm(label: str, *, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = _prompt(f"{label} {suffix}", default="y" if default else "n")
    return (raw or "").lower() in ("y", "yes")


def _is_interactive(args: argparse.Namespace) -> bool:
    if getattr(args, "no_input", False):
        return False
    return sys.stdin.isatty()


# ── argparse wiring ───────────────────────────────────────────────────


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("submissions", help="own dataset submission operations")
    sp = p.add_subparsers(dest="submissions_command", required=True)

    p_list = sp.add_parser("list", help="list my dataset submissions")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--limit", type=int, default=10)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_lic = sp.add_parser(
        "licenses", help="list available licenses (use the id with `create`)",
    )
    p_lic.add_argument("--json", action="store_true")
    p_lic.set_defaults(func=_cmd_licenses)

    p_show = sp.add_parser("show", help="show one submission detail")
    p_show.add_argument("ref", help="<code>[@<version>] (defaults to @1.0.0)")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=_cmd_show)

    p_create = sp.add_parser(
        "create",
        help=(
            "create a new dataset submission "
            "(prompts for missing fields on a TTY, npm-init style)"
        ),
    )
    p_create.add_argument("--title")
    p_create.add_argument("--code")
    p_create.add_argument(
        "--version", help="semver (default: 1.0.0 if omitted)",
    )
    p_create.add_argument(
        "--license-id", type=int, dest="license_id",
        help="license ID -- look one up via `khdp submissions licenses`",
    )
    p_create.add_argument("--summary")
    p_create.add_argument(
        "--policy", choices=_POLICY_CHOICES,
        help="access policy (default: open)",
    )
    p_create.add_argument(
        "--no-input", action="store_true", dest="no_input",
        help=(
            "do not prompt for missing fields; require every value via "
            "flags. Implies --yes (skips the confirmation prompt)."
        ),
    )
    p_create.add_argument(
        "--details-file", dest="details_file",
        help="JSON file containing [{name, content}, ...]",
    )
    p_create.add_argument(
        "--details-md", dest="details_md",
        help="Markdown file -- '# heading' lines become section names",
    )
    p_create.set_defaults(func=_cmd_create)

    p_update = sp.add_parser(
        "update",
        help="patch fields of an existing submission (Writing stage only)",
    )
    p_update.add_argument("ref")
    p_update.add_argument("--title")
    p_update.add_argument("--code")
    p_update.add_argument("--version")
    p_update.add_argument("--license-id", type=int, dest="license_id")
    p_update.add_argument("--summary")
    p_update.add_argument("--policy", choices=_POLICY_CHOICES)
    p_update.add_argument(
        "--details-file", dest="details_file",
        help="JSON file containing [{name, content}, ...]",
    )
    p_update.add_argument(
        "--details-md", dest="details_md",
        help="Markdown file -- '# heading' lines become section names",
    )
    p_update.set_defaults(func=_cmd_update)

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


# ── details loaders ───────────────────────────────────────────────────


def _load_details_from_json(path: str) -> list[dict[str, Any]]:
    """Parse a JSON file of ``[{"name": ..., "content": ...}, ...]``."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[khdp] invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, list):
        raise SystemExit(
            f"[khdp] details JSON must be an array of {{name, content}}: {path}"
        )
    for i, item in enumerate(data):
        if (
            not isinstance(item, dict)
            or "name" not in item
            or "content" not in item
        ):
            raise SystemExit(
                f"[khdp] details[{i}] must have 'name' and 'content' keys "
                f"({path})"
            )
    return data


def _load_details_from_md(path: str) -> list[dict[str, Any]]:
    """Split a Markdown file by ``#`` (H1) headings.

    Each H1 becomes a section ``{"name": heading, "content": body}``;
    content is the verbatim text between this H1 and the next.
    """
    text = Path(path).expanduser().read_text(encoding="utf-8")
    sections: list[dict[str, Any]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            if current is not None:
                sections.append(
                    {"name": current["name"], "content": current["content"].strip()}
                )
            current = {"name": match.group(1).strip(), "content": ""}
        elif current is not None:
            current["content"] += line + "\n"
    if current is not None:
        sections.append(
            {"name": current["name"], "content": current["content"].strip()}
        )
    if not sections:
        raise SystemExit(
            f"[khdp] no `# heading` sections found in {path}"
        )
    return sections


def _resolve_details(args: argparse.Namespace) -> list[dict[str, Any]] | None:
    """Pick whichever details source was provided (mutually exclusive)."""
    file_path = getattr(args, "details_file", None)
    md_path = getattr(args, "details_md", None)
    if file_path and md_path:
        raise SystemExit(
            "[khdp] choose either --details-file or --details-md, not both"
        )
    if file_path:
        return _load_details_from_json(file_path)
    if md_path:
        return _load_details_from_md(md_path)
    return None


# ── pretty printers ───────────────────────────────────────────────────


def _print_submission_list(body: Any) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        _emit(body)
        return
    items = body["data"]
    if not items:
        print("(no submissions)")
        return

    def _cell(it: dict[str, Any], *keys: str) -> str:
        for k in keys:
            v = it.get(k)
            if v not in (None, ""):
                return str(v)
        return ""

    print(f"{'code':<24}  {'version':<10}  {'status':<14}  title")
    print(f"{'-' * 24}  {'-' * 10}  {'-' * 14}  -----")
    for it in items:
        code = _cell(it, "code", "ciCode")
        version = _cell(it, "version")
        status = _cell(it, "status", "cvStatus")
        title = _cell(it, "title", "ciTitle")
        print(f"{code:<24}  {version:<10}  {status:<14}  {title}")
    total = body.get("totalCnt")
    if total is not None:
        print(f"\ntotal {total}")


def _print_licenses(body: Any) -> None:
    if not isinstance(body, list):
        _emit(body)
        return
    if not body:
        print("(no licenses)")
        return
    id_w = max(len("id"), max(len(str(it.get("lId", ""))) for it in body))
    code_w = max(len("code"), max(len(str(it.get("lCode", ""))) for it in body))
    print(f"{'id':<{id_w}}  {'code':<{code_w}}  name")
    print(f"{'-' * id_w}  {'-' * code_w}  ----")
    for it in body:
        print(
            f"{it.get('lId', '')!s:<{id_w}}  "
            f"{it.get('lCode', '')!s:<{code_w}}  "
            f"{it.get('lName', '')}"
        )


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


def _cmd_licenses(session: Session, args: argparse.Namespace) -> int:
    resp = session.authed_request(
        "GET", "/open/dataset-submissions/licenses",
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    if args.json:
        _emit(body)
    else:
        _print_licenses(body)
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
    interactive = _is_interactive(args)

    title = args.title
    code = args.code
    version = args.version or ("1.0.0" if not interactive else None)
    license_id = args.license_id
    summary = args.summary
    policy = args.policy or ("open" if not interactive else None)

    if interactive:
        if not title:
            title = _prompt("title", required=True)
        if not code:
            code = _prompt("code", required=True)
        version = _prompt("version", default=version or "1.0.0")
        if license_id is None:
            license_id = _prompt_int(
                "license id (run `khdp submissions licenses` to discover)",
                required=True,
            )
        if not summary:
            summary = _prompt("summary", required=True)
        policy = _prompt_choice(
            "access policy", _POLICY_CHOICES, default=policy or "open",
        )

    # Non-interactive (or after prompts): every field must be present.
    missing = [
        name for name, val in [
            ("title", title),
            ("code", code),
            ("license-id", license_id),
            ("summary", summary),
        ] if val in (None, "")
    ]
    if missing:
        flags = " ".join(f"--{m}" for m in missing)
        raise SystemExit(
            f"[khdp] missing required field(s): {', '.join(missing)}. "
            f"Pass {flags} or run interactively (without --no-input)."
        )

    if interactive:
        print(
            "\nAbout to create:\n"
            f"  code:    {code}\n"
            f"  version: {version}\n"
            f"  title:   {title}\n"
            f"  license: {license_id}\n"
            f"  policy:  {policy}\n"
            f"  summary: {summary}\n"
        )
        if not _confirm("Create this submission?", default=True):
            print("[khdp] aborted")
            return 1

    body_req: dict[str, Any] = {
        "title": title,
        "version": version,
        "lId": license_id,
        "code": code,
        "summary": summary,
        "accessPolicy": policy,
    }
    details = _resolve_details(args)
    if details is not None:
        body_req["details"] = details
    resp = session.authed_request(
        "POST", "/open/dataset-submissions", json=body_req,
    )
    body = _try_json(resp)
    if (rc := _check_response(resp, body)) is not None:
        return rc
    _emit(body)
    return 0


def _cmd_update(session: Session, args: argparse.Namespace) -> int:
    code, version = _parse_ref(args.ref)
    body_req: dict[str, Any] = {}
    if args.title is not None:
        body_req["title"] = args.title
    if args.code is not None:
        body_req["code"] = args.code
    if args.version is not None:
        body_req["version"] = args.version
    if args.license_id is not None:
        body_req["lId"] = args.license_id
    if args.summary is not None:
        body_req["summary"] = args.summary
    if args.policy is not None:
        body_req["accessPolicy"] = args.policy
    details = _resolve_details(args)
    if details is not None:
        body_req["details"] = details

    if not body_req:
        raise SystemExit(
            "[khdp] nothing to update -- pass at least one field "
            "(--title / --summary / --details-file ...)"
        )

    resp = session.authed_request(
        "PATCH",
        f"/open/dataset-submissions/{code}/{version}",
        json=body_req,
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
