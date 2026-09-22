"""Operator command-line (`maskroom-admin`): database bootstrap and moving the
admin rules overlay between YAML and the database.

    maskroom-admin db init                     create the schema (idempotent)
    maskroom-admin policy export [FILE]        current rules as YAML (stdout or FILE)
    maskroom-admin policy import FILE [--user] load a YAML overlay, recording a revision

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

    args = parser.parse_args(argv)
    from .store import PolicyStore, connect, default_url

    if args.group == "db":
        connect()
        print(f"schema ready at {default_url()}")
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
