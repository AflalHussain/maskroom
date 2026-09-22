"""Operator command-line (`maskroom-admin`): database bootstrap and moving the
admin rules overlay between YAML and the database.

    maskroom-admin db init                     create the schema (idempotent)
    maskroom-admin policy export [FILE]        current rules as YAML (stdout or FILE)
    maskroom-admin policy import FILE [--user] load a YAML overlay, recording a revision
    maskroom-admin user list | role EMAIL ROLE | disable EMAIL | enable EMAIL
    maskroom-admin key create NAME [--role R] | list | revoke ID

Honours MASKROOM_DATABASE_URL and MASKROOM_DATA_DIR like the web app.
"""
import argparse
import sys

import yaml

from . import overlay as overlay_mod


def main(argv=None):
    parser = argparse.ArgumentParser(prog="maskroom-admin",
                                     description="Maskroom operator commands.")
    sub = parser.add_subparsers(dest="group", required=True)

    db = sub.add_parser("db", help="database bootstrap").add_subparsers(dest="cmd", required=True)
    db.add_parser("init", help="create the schema if missing and print the URL")

    pol = sub.add_parser("policy", help="admin rules overlay").add_subparsers(dest="cmd", required=True)
    exp = pol.add_parser("export", help="write the live overlay as YAML")
    exp.add_argument("file", nargs="?", help="output path (default: stdout)")
    imp = pol.add_parser("import", help="replace the live overlay from a YAML file")
    imp.add_argument("file")
    imp.add_argument("--user", default="maskroom-admin", help="who to record in the revision")

    usr = sub.add_parser("user", help="sign-on users and roles").add_subparsers(dest="cmd", required=True)
    usr.add_parser("list", help="every user with role and status")
    p_role = usr.add_parser("role", help="set a user's role (staff, auditor, admin)")
    p_role.add_argument("email"); p_role.add_argument("role")
    usr.add_parser("disable", help="block a user and end their sessions").add_argument("email")
    usr.add_parser("enable", help="re-enable a user").add_argument("email")

    key = sub.add_parser("key", help="service keys for scripts and gateways").add_subparsers(dest="cmd", required=True)
    p_new = key.add_parser("create", help="issue a key; the secret is printed once")
    p_new.add_argument("name"); p_new.add_argument("--role", default="staff")
    key.add_parser("list", help="every key, revoked ones included")
    key.add_parser("revoke", help="revoke a key by id").add_argument("key_id")

    args = parser.parse_args(argv)
    from .store import (ApiKeyStore, LoginSessionStore, PolicyStore, UserStore, connect,
                        default_url)

    if args.group == "db":
        connect()
        print(f"schema ready at {default_url()}")
        return 0

    if args.group == "user":
        users = UserStore(connect())
        if args.cmd == "list":
            for u in users.list():
                flag = " (disabled)" if u.disabled else ""
                print(f"{u.email:40} {u.role:8} {u.name}{flag}")
            return 0
        u = users.get_by_email(args.email)
        if not u:
            parser.error(f"no user {args.email!r} (users are created on first sign-in)")
        if args.cmd == "role":
            try:
                users.set_role(u.id, args.role)
            except ValueError as e:
                parser.error(str(e))
            print(f"{u.email}: role {args.role}")
        else:
            disabled = args.cmd == "disable"
            users.set_disabled(u.id, disabled)
            if disabled:
                LoginSessionStore(connect()).revoke_user(u.id)
            print(f"{u.email}: {'disabled, sessions ended' if disabled else 'enabled'}")
        return 0

    if args.group == "key":
        keys = ApiKeyStore(connect())
        if args.cmd == "create":
            try:
                k, secret = keys.create(args.name, role=args.role, created_by="maskroom-admin")
            except ValueError as e:
                parser.error(str(e))
            print(f"{k.id} {k.name} ({k.role})\nsecret (shown once): {secret}")
        elif args.cmd == "list":
            for k in keys.list():
                state = "revoked" if k.revoked_at else "active"
                print(f"{k.id:18} {k.name:24} {k.role:8} {state}")
        else:
            print("revoked" if keys.revoke(args.key_id) else "no such active key")
            return 0
        return 0

    store = PolicyStore(connect())
    if args.cmd == "export":
        text = yaml.safe_dump(store.load(), sort_keys=False, allow_unicode=True)
        if args.file:
            with open(args.file, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"wrote {args.file}")
        else:
            sys.stdout.write(text)
        return 0

    data = overlay_mod.load(path=args.file)
    try:
        norm, rev = store.save(data, user_id=args.user)
    except overlay_mod.PolicyError as e:
        parser.error(str(e))
    print(f"revision {rev}: deny={len(norm['deny_terms'])} allow={len(norm['allow_terms'])} "
          f"regex={len(norm['regex_rules'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
