"""Command line helpers.

  python -m app.cli hash-password            # prints an ADMIN_PASSWORD_HASH value
  python -m app.cli import [source-id]       # run an import now (all sources if omitted)
  python -m app.cli test-rule <file> <rule>  # run a rule (id from rules/ or YAML file) against a PDF/HTML/image
  python -m app.cli geocode [--force]        # fill missing coordinates of locations (OpenStreetMap Nominatim)
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "hash-password":
        from argon2 import PasswordHasher

        pw = getpass.getpass("Admin password: ")
        if len(pw) < 12:
            print("Please use at least 12 characters.", file=sys.stderr)
            return 1
        if pw != getpass.getpass("Repeat: "):
            print("Passwords differ.", file=sys.stderr)
            return 1
        print(PasswordHasher().hash(pw))
        return 0

    from . import db
    from .config import get_settings
    from .profiles import ProfileStore

    settings = get_settings()
    store = ProfileStore(settings.profiles_dir, settings.rules_dir)
    if cmd == "import":
        from .importer.pipeline import Importer
        from .llm.base import get_provider

        db.init_db(settings.db_url)
        importer = Importer(settings, store, get_provider(settings))
        for source_id in args or list(store.sources):
            run_id = importer.run_source(source_id, "manual")
            with db.session() as s:
                run = s.get(db.ImportRun, run_id)
                print(f"== {source_id}: {run.status}\n{run.log}")
        return 0
    if cmd == "geocode":
        from .geocode import fill_missing

        for line in fill_missing(store, force="--force" in args):
            print(line)
        return 0
    if cmd == "test-rule":
        from ruamel.yaml import YAML

        from .extract.common import Context
        from .extract.engine import run_rule

        from .extract.engine import load_document
        from .importer.fetch import sniff_media_type

        path, rule_ref = Path(args[0]), args[1]
        if rule_ref.endswith(".yml"):
            spec = YAML(typ="safe").load(Path(rule_ref).read_text(encoding="utf-8"))
            rule = store.resolve_rule({k: v for k, v in spec.items() if k != "id"})
        else:
            rule = store.resolve_rule(rule_ref)
        data = path.read_bytes()
        doc = load_document(data, sniff_media_type(data), args[2] if len(args) > 2 else None)
        ex = run_rule(rule, doc, Context(fetch_date=dt.date.today(), url=str(path)))
        print(json.dumps(ex.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print(f"unknown command {cmd}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
