"""Jarvis command-line interface.

    jarvis                     start the assistant (voice if available)
    jarvis --text              start in console chat mode
    jarvis --ui                also serve the local status orb page
    jarvis setup-google        run the Google authorization flow now
    jarvis secrets set NAME    store a secret in the Windows keyring
    jarvis permissions list    show persistent grants
    jarvis permissions revoke CAPABILITY
    jarvis audit [-n N] [--verify]
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from .config import load_config

_SECRET_ALIASES = {"anthropic": "anthropic-api-key"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis voice assistant")
    parser.add_argument("--config", help="Path to a config YAML override", default=None)
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Start the assistant (default)")
    for p in (parser, run_p):
        p.add_argument("--text", action="store_true", help="Console chat mode (no mic)")
        p.add_argument("--ui", action="store_true", help="Serve the local status page")

    sub.add_parser("setup-google", help="Authorize Google Drive/Gmail access now")

    sec = sub.add_parser("secrets", help="Manage secrets in the Windows keyring")
    sec.add_argument("action", choices=["set", "delete"])
    sec.add_argument("name", help="Secret name, e.g. 'anthropic'")

    perm = sub.add_parser("permissions", help="Review or revoke capability grants")
    perm.add_argument("action", choices=["list", "revoke"])
    perm.add_argument("capability", nargs="?", default="")

    aud = sub.add_parser("audit", help="Show or verify the audit log")
    aud.add_argument("-n", type=int, default=20, help="How many entries to show")
    aud.add_argument("--verify", action="store_true", help="Verify the hash chain")

    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.cmd in (None, "run"):
        from .app import JarvisApp

        app = JarvisApp(config, force_text=getattr(args, "text", False),
                        with_ui=getattr(args, "ui", False))
        app.run()
        return 0

    if args.cmd == "setup-google":
        from .integrations.google_auth import get_credentials

        creds = get_credentials(config.google_credentials_file, config.google_scopes)
        print("Google authorization complete." if creds and creds.valid
              else "Authorization did not complete.")
        return 0

    if args.cmd == "secrets":
        from .security import secrets as store

        name = _SECRET_ALIASES.get(args.name, args.name)
        if args.action == "set":
            value = getpass.getpass(f"Value for '{name}' (input hidden): ").strip()
            if not value:
                print("Empty value — nothing stored.")
                return 1
            ok = store.set_secret(name, value)
            print("Stored in Windows Credential Manager." if ok
                  else "Failed — no keyring backend available.")
            return 0 if ok else 1
        ok = store.delete_secret(name)
        print("Deleted." if ok else "Nothing to delete (or keyring unavailable).")
        return 0

    if args.cmd == "permissions":
        from .io_channel import TextIO
        from .security import AuditLog, PermissionManager

        pm = PermissionManager(TextIO(), AuditLog(),
                               session_grant_minutes=config.session_grant_minutes)
        if args.action == "list":
            grants = pm.list_grants()
            if not grants:
                print("No persistent grants.")
            for cap, meta in sorted(grants.items()):
                print(f"  {cap}: {meta.get('scope', '?')}")
            return 0
        if not args.capability:
            print("Usage: jarvis permissions revoke <capability>")
            return 1
        removed = pm.revoke(args.capability)
        print("Revoked." if removed else "No such persistent grant.")
        return 0

    if args.cmd == "audit":
        from .security import AuditLog

        log = AuditLog()
        if args.verify:
            intact, count = log.verify_chain()
            print(f"Audit chain {'INTACT' if intact else 'BROKEN'} ({count} entries verified).")
            return 0 if intact else 1
        for entry in log.tail(args.n):
            print(json.dumps(entry, ensure_ascii=False))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
