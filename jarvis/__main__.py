"""Jarvis command-line interface.

    jarvis                     start the assistant (voice if available)
    jarvis --text              start in console chat mode
    jarvis --ui                also serve the local status orb page
    jarvis setup               first-run consent wizard (what may Jarvis access?)
    jarvis setup-google        run the Google authorization flow now
    jarvis secrets set NAME    store a secret in the Windows keyring
    jarvis permissions list    show persistent grants
    jarvis permissions revoke CAPABILITY
    jarvis memory list         show what Jarvis remembers about you
    jarvis memory forget ID    delete one remembered fact
    jarvis audit [-n N] [--verify]
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from .config import load_config

_SECRET_ALIASES = {"anthropic": "anthropic-api-key"}


def main(argv: list[str] | None = None) -> int:
    # Under pythonw.exe (autostart) stdout/stderr are None and any print()
    # would crash the process — route them to devnull instead.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis voice assistant")
    parser.add_argument("--config", help="Path to a config YAML override", default=None)
    parser.add_argument("--text", action="store_true", help="Console chat mode (no mic)")
    parser.add_argument("--ui", action="store_true", help="Serve the local status page")
    sub = parser.add_subparsers(dest="cmd")

    # The run subparser accepts the same flags, but with SUPPRESS defaults so
    # parsing "jarvis --text run" doesn't clobber flags given before the
    # subcommand ("jarvis run --text" and "jarvis run --config X" also work).
    run_p = sub.add_parser("run", help="Start the assistant (default)")
    run_p.add_argument("--config", default=argparse.SUPPRESS,
                       help="Path to a config YAML override")
    run_p.add_argument("--text", action="store_true", default=argparse.SUPPRESS,
                       help="Console chat mode (no mic)")
    run_p.add_argument("--ui", action="store_true", default=argparse.SUPPRESS,
                       help="Serve the local status page")

    sub.add_parser("setup", help="First-run consent wizard: choose what Jarvis may access")
    sub.add_parser("setup-google", help="Authorize Google Drive/Gmail access now")

    sec = sub.add_parser("secrets", help="Manage secrets in the Windows keyring")
    sec.add_argument("action", choices=["set", "delete"])
    sec.add_argument("name", help="Secret name, e.g. 'anthropic'")

    perm = sub.add_parser("permissions", help="Review or revoke capability grants")
    perm.add_argument("action", choices=["list", "revoke"])
    perm.add_argument("capability", nargs="?", default="")

    mem = sub.add_parser("memory", help="Review or delete what Jarvis remembers")
    mem.add_argument("action", choices=["list", "forget", "clear"])
    mem.add_argument("fact_id", nargs="?", default="")

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

    if args.cmd == "setup":
        from .setup_wizard import run_setup

        run_setup(config)
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

        pm = PermissionManager(TextIO(), AuditLog(anchored=True),
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

    if args.cmd == "memory":
        from .memory import MemoryStore
        from .security import AuditLog

        store = MemoryStore()
        log = AuditLog(anchored=True)
        if args.action == "list":
            facts = store.all()
            if not facts:
                print("Jarvis hasn't remembered anything yet.")
                return 0
            live = store.live_ids()
            for fact in facts:
                # Stored-but-not-injected facts would otherwise look active.
                marker = " " if fact.id in live else " (stored, not currently used)"
                print(f"  [{fact.id}] ({fact.category}) {fact.text}"
                      f"   — {fact.created[:10]}{marker}")
            if len(live) < len(facts):
                print(f"\n{len(live)} of {len(facts)} facts fit in the per-turn "
                      "memory budget; the rest are kept but not shown to Jarvis.")
            return 0
        if args.action == "forget":
            if not args.fact_id:
                print("Usage: jarvis memory forget <id>   (see: jarvis memory list)")
                return 1
            removed = store.forget(args.fact_id)
            if removed:
                log.record("memory", tool="forget", detail=removed.id, decision="cli")
                print(f'Forgot: "{removed.text}"')
                return 0
            print("No such fact id.")
            return 1
        if args.fact_id:
            print("`memory clear` takes no id. Did you mean: "
                  f"jarvis memory forget {args.fact_id}")
            return 1
        pending = len(store.all())
        if not pending:
            print("Nothing to clear.")
            return 0
        answer = input(f"Delete all {pending} remembered fact(s)? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return 1
        count = store.clear()
        log.record("memory", tool="clear", detail=f"{count} facts", decision="cli")
        print(f"Cleared {count} remembered fact(s).")
        return 0

    if args.cmd == "audit":
        from .security import AuditLog

        log = AuditLog(anchored=True)
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
