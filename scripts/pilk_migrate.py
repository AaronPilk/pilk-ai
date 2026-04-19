#!/usr/bin/env python3
"""pilk-migrate — export local pilkd state + optionally upload to cloud.

Usage:
    pilk-migrate export [--home ~/PILK] [--output ~/pilk-bundle.zip]
    pilk-migrate upload --bundle ~/pilk-bundle.zip \\
        --api https://pilk-ai-production.up.railway.app \\
        --token <supabase-jwt>

The export half runs against the local filesystem only (no network).
The upload half POSTs the zip to ``<api>/migration/upload`` with a
bearer token — the operator signs in to pilk.ai, grabs their JWT from
the browser's ``localStorage``, and pastes it here.

This script lives under ``scripts/`` so it's shipped with the repo but
doesn't end up on every ``pilkd`` install's PATH. Run it with
``uv run python scripts/pilk_migrate.py ...``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib import error, request

from core.migration import build_bundle


def _cmd_export(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    clients_dir_arg: Path | None = (
        Path(args.clients).expanduser().resolve() if args.clients else None
    )

    if not home.is_dir():
        print(
            f"error: home directory not found: {home}", file=sys.stderr
        )
        return 2

    print(f"Reading pilkd home: {home}")
    print(f"Writing bundle to:  {output}")
    if clients_dir_arg:
        print(f"Including clients:  {clients_dir_arg}")

    manifest = build_bundle(
        home=home, output_path=output, clients_dir=clients_dir_arg
    )

    counts = manifest.table_counts.model_dump()
    print("\nTable row counts:")
    for table, count in counts.items():
        if isinstance(count, int) and count > 0:
            print(f"  {table:<24} {count:>6}")
    print(f"\nAccount-token blobs:  {manifest.account_count}")
    print(f"Client YAMLs:         {manifest.client_count}")
    print(f"Total files archived: {len(manifest.files)}")
    print(f"\nBundle ready at: {output}")
    print(f"Size: {output.stat().st_size:,} bytes")
    print(
        "\nNext step: run\n"
        f"  pilk-migrate upload --bundle {output} \\\n"
        "    --api https://pilk-ai-production.up.railway.app \\\n"
        "    --token <your-supabase-jwt>"
    )
    return 0


def _cmd_upload(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle).expanduser().resolve()
    if not bundle_path.is_file():
        print(f"error: bundle not found: {bundle_path}", file=sys.stderr)
        return 2

    api_base = args.api.rstrip("/")
    url = f"{api_base}/migration/upload"
    size = bundle_path.stat().st_size
    print(f"Uploading {bundle_path.name} ({size:,} bytes) → {url}")
    print("This overwrites the remote pilkd home. Backup is automatic.\n")

    # Build a minimal multipart request by hand so we don't pull in
    # requests/httpx for a one-off CLI.
    boundary = "pilkboundary" + "x" * 8
    body = _multipart_body(
        boundary=boundary,
        bundle_path=bundle_path,
        confirm="MIGRATE",
    )
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {args.token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )

    try:
        with request.urlopen(req, timeout=120) as resp:
            status = resp.status
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:2000]
        print(f"HTTP {e.code}: {body_text}", file=sys.stderr)
        return 1
    except error.URLError as e:
        print(f"network error: {e}", file=sys.stderr)
        return 1

    print(f"Status: {status}")
    print(json.dumps(payload, indent=2, default=str))
    if not payload.get("ok"):
        return 1

    print(
        "\nImport ok. Next step: hit Railway's 'Redeploy' to restart "
        "pilkd so in-memory stores load the imported data."
    )
    return 0


def _multipart_body(
    *, boundary: str, bundle_path: Path, confirm: str
) -> bytes:
    """Assemble a multipart/form-data body with one file field
    (``bundle``) and one text field (``confirm``). Hand-rolled so
    the CLI has no third-party dependencies."""
    crlf = b"\r\n"
    parts: list[bytes] = []
    b = boundary.encode("ascii")

    # text field — confirm
    parts.append(b"--" + b + crlf)
    parts.append(b'Content-Disposition: form-data; name="confirm"' + crlf)
    parts.append(crlf)
    parts.append(confirm.encode("utf-8") + crlf)

    # file field — bundle
    parts.append(b"--" + b + crlf)
    parts.append(
        (
            'Content-Disposition: form-data; name="bundle"; '
            f'filename="{bundle_path.name}"'
        ).encode()
        + crlf
    )
    parts.append(b"Content-Type: application/zip" + crlf)
    parts.append(crlf)
    parts.append(bundle_path.read_bytes())
    parts.append(crlf)

    parts.append(b"--" + b + b"--" + crlf)
    return b"".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pilk-migrate",
        description="Export + upload pilkd state from local → cloud.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_export = sub.add_parser("export", help="Create a migration bundle.")
    sp_export.add_argument(
        "--home",
        default="~/PILK",
        help="pilkd data home (default: %(default)s).",
    )
    sp_export.add_argument(
        "--output",
        default="~/pilk-migration-bundle.zip",
        help="Where to write the zip (default: %(default)s).",
    )
    sp_export.add_argument(
        "--clients",
        default=None,
        help="Optional repo-root clients/ directory to include.",
    )
    sp_export.set_defaults(func=_cmd_export)

    sp_upload = sub.add_parser(
        "upload", help="Upload a bundle to a cloud pilkd."
    )
    sp_upload.add_argument("--bundle", required=True)
    sp_upload.add_argument(
        "--api",
        required=True,
        help=(
            "API base URL, e.g. https://pilk-ai-production.up.railway.app."
        ),
    )
    sp_upload.add_argument(
        "--token",
        required=True,
        help="Supabase JWT (from the dashboard's localStorage).",
    )
    sp_upload.set_defaults(func=_cmd_upload)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
