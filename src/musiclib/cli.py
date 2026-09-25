"""musiclib: JSON-emitting commands the agent calls. See docs/SPEC.md."""

import argparse
import json
import os
import sys

from . import acoustid, config, db, inventory, report


def _emit(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def cmd_inventory(cfg: config.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    _emit(inventory.run(conn, cfg.source_dir, workers=args.workers,
                        fingerprint=not args.no_fingerprint, subdir=args.subdir, limit=args.limit))


def cmd_acoustid(cfg: config.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    _emit(acoustid.run(conn, cfg.acoustid_key, limit=args.limit, retry_errors=args.retry_errors))


def cmd_report(cfg: config.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    _emit(report.inventory_summary(conn, top=args.top))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="musiclib", description=__doc__)
    p.add_argument("--config", default=config.DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="scan the source dump into the state DB (read-only)")
    inv.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    inv.add_argument("--no-fingerprint", action="store_true", help="skip Chromaprint (much faster)")
    inv.add_argument("--subdir", help="only scan this path, relative to source_dir")
    inv.add_argument("--limit", type=int, help="scan at most N files (for testing)")
    inv.set_defaults(func=cmd_inventory)

    aid = sub.add_parser("acoustid", help="look up fingerprints on AcoustID (resumable)")
    aid.add_argument("--limit", type=int, help="look up at most N fingerprints")
    aid.add_argument("--retry-errors", action="store_true", help="retry earlier failed lookups")
    aid.set_defaults(func=cmd_acoustid)

    rep = sub.add_parser("report", help="summarize the inventory as JSON")
    rep.add_argument("--top", type=int, default=10, help="examples per section")
    rep.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    args.func(config.load(args.config), args)


if __name__ == "__main__":
    main()
