"""musiclib: JSON-emitting commands the agent calls. See docs/SPEC.md."""

import argparse
import contextlib
import json
import os
import sys

from . import acoustid, config, db, dupes, inventory, report, verify


def _emit(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def cmd_inventory(cfg: config.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    _emit(inventory.run(conn, cfg.source_dir, workers=args.workers,
                        fingerprint=not args.no_fingerprint, subdir=args.subdir, limit=args.limit))


def cmd_acoustid(cfg: config.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    if args.titles:
        _emit(acoustid.fetch_titles(conn, cfg.acoustid_key))
    else:
        _emit(acoustid.run(conn, cfg.acoustid_key, limit=args.limit, retry_errors=args.retry_errors))


def cmd_verify(cfg: config.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    _emit(verify.run(conn, top=args.top))


def cmd_dupes(cfg: config.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    _emit(dupes.find(conn))


def cmd_match(cfg: config.Config, args) -> None:
    from . import beetsenv
    beetsenv.setup(cfg)  # before anything imports beets (D9)
    from . import match

    conn = db.connect(cfg.db_path)
    if args.summary:
        _emit(match.summary(conn, top=args.top))
    else:
        _emit(match.run(conn, cfg.source_dir, subdir=args.subdir, limit=args.limit,
                        rematch=args.rematch))


def cmd_import(cfg: config.Config, args) -> None:
    from . import beetsenv
    beetsenv.setup(cfg)
    from . import importer

    conn = db.connect(cfg.db_path)
    importer.migrate(conn)
    mutating = args.apply or args.remove
    with importer.library_lock(cfg.state_dir) if mutating else contextlib.nullcontext():
        if args.prune_duplicates:
            _emit(importer.prune_duplicates(conn, apply=args.apply))
            return
        if args.remove:
            if not args.reason:
                raise SystemExit("--remove needs --reason")
            _emit(importer.remove_from_library(conn, args.remove, args.reason, args.by))
            return
        if args.repair_extras:
            _emit(importer.repair_extras(conn, cfg.source_dir, apply=args.apply))
            return
        if not args.apply:
            _emit(importer.plan(conn, cfg.source_dir, args.which, args.limit, args.album))
            return
        _emit(importer.run(conn, cfg.source_dir, cfg.state_dir / "staging", args.which,
                           limit=args.limit, only=args.album))


def cmd_review(cfg: config.Config, args) -> None:
    from . import review

    conn = db.connect(cfg.db_path)
    if args.review_cmd == "stats":
        _emit(review.stats(conn))
    elif args.review_cmd == "list":
        _emit(review.listing(conn, args.kind, args.limit, args.offset))
    elif args.review_cmd == "auto":
        _emit(review.auto_approve(conn, kind=args.kind, limit=args.limit, apply=args.apply))
    else:
        data = json.load(sys.stdin if args.file == "-" else open(args.file))
        _emit(review.decide(conn, data, args.by))


def cmd_retag(cfg: config.Config, args) -> None:
    from . import beetsenv
    beetsenv.setup(cfg)
    from . import importer, retag

    conn = db.connect(cfg.db_path)
    if args.scan:
        _emit(retag.swap_suspects(conn))
        return
    if not args.album:
        raise SystemExit("--album or --scan required")
    lib = importer.open_library()
    if args.apply:
        with importer.library_lock(cfg.state_dir):
            _emit(retag.apply(conn, lib, args.album, args.by, args.reason or "tags were on the wrong audio"))
    else:
        p = retag.plan(conn, lib, args.album)
        _emit({k: v for k, v in p.items() if not k.startswith("_")})


def cmd_merge(cfg: config.Config, args) -> None:
    from . import beetsenv
    beetsenv.setup(cfg)
    from . import match

    conn = db.connect(cfg.db_path)
    if args.suggest:
        _emit(match.merge_suggestions(conn))
    elif args.albums and len(args.albums) > 1:
        _emit(match.merge(conn, cfg.source_dir, args.albums, apply=args.apply))
    else:
        raise SystemExit("--suggest, or --albums with two or more album keys")


def cmd_resync(cfg: config.Config, args) -> None:
    from . import beetsenv
    beetsenv.setup(cfg)
    from . import importer, resync

    conn = db.connect(cfg.db_path)
    with importer.library_lock(cfg.state_dir) if args.apply else contextlib.nullcontext():
        _emit(resync.run(conn, apply=args.apply, limit=args.limit))


def cmd_ingest(cfg: config.Config, args) -> None:
    from . import ingest

    conn = db.connect(cfg.db_path)
    if args.ingest_cmd == "list":
        _emit(ingest.listing(conn, cfg))
    elif args.ingest_cmd == "claim":
        _emit(ingest.claim(conn, cfg, args.folder, apply=args.apply, decided_by=args.by))
    elif args.ingest_cmd == "scan":
        _emit(ingest.scan(conn, cfg, args.batch, workers=args.workers))
    elif args.ingest_cmd == "dedupe":
        _emit(ingest.dedupe(conn, args.batch))


def cmd_report(cfg: config.Config, args) -> None:
    conn = db.connect(cfg.db_path)
    if args.not_imported:
        summary, rows = report.not_imported(conn)
        cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        out = cfg.reports_dir / f"not-imported-{report_date()}.json"
        out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
        _emit({**summary, "written": str(out)})
        return
    _emit(report.inventory_summary(conn, top=args.top))


def report_date() -> str:
    from datetime import date
    return date.today().isoformat()


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
    aid.add_argument("--titles", action="store_true",
                     help="fetch recording titles for mismatch/suggest files (run after verify)")
    aid.set_defaults(func=cmd_acoustid)

    ver = sub.add_parser("verify", help="check MusicBrainz tags against AcoustID results")
    ver.add_argument("--top", type=int, default=10, help="examples per section")
    ver.set_defaults(func=cmd_verify)

    dup = sub.add_parser("dupes", help="group duplicates and propose keepers (run after verify)")
    dup.set_defaults(func=cmd_dupes)

    mat = sub.add_parser("match", help="dry run: match albums on MusicBrainz via beets (read-only)")
    mat.add_argument("--subdir", help="only top-level folders starting with this")
    mat.add_argument("--limit", type=int, help="match at most N albums")
    mat.add_argument("--rematch", action="store_true", help="re-match albums already matched")
    mat.add_argument("--summary", action="store_true", help="summarize existing matches only")
    mat.add_argument("--top", type=int, default=10, help="examples in --summary")
    mat.set_defaults(func=cmd_match)

    imp = sub.add_parser("import", help="import matched albums into the library (dry run without --apply)")
    imp.add_argument("--which", choices=["auto", "unsorted", "approved"], default="auto",
                     help="auto: strong matches; unsorted: no match, as-is (D13); approved: reviewed")
    imp.add_argument("--limit", type=int, help="import at most N albums")
    imp.add_argument("--album", action="append", help="only this album key (repeatable)")
    imp.add_argument("--apply", action="store_true", help="actually copy files into the library")
    imp.add_argument("--remove", metavar="LIBRARY_FILE", help="remove one file from the library (logged)")
    imp.add_argument("--reason", help="why (required with --remove)")
    imp.add_argument("--by", choices=["agent", "user"], default="user", help="who decided (with --remove)")
    imp.add_argument("--repair-extras", action="store_true",
                     help="copy unmapped files missing from already-imported albums (with --apply)")
    imp.add_argument("--prune-duplicates", action="store_true",
                     help="remove library copies of files later found to be duplicates (with --apply)")
    imp.set_defaults(func=cmd_import)

    rev = sub.add_parser("review", help="M4 review queue: stats, list batches, record decisions")
    rsub = rev.add_subparsers(dest="review_cmd", required=True)
    rsub.add_parser("stats", help="open/decided counts per kind")
    rl = rsub.add_parser("list", help="a batch of albums with context and a suggestion")
    rl.add_argument("--kind", choices=["close", "weak", "none", "dupe", "error"], default="close")
    rl.add_argument("--limit", type=int, default=20)
    rl.add_argument("--offset", type=int, default=0)
    ra = rsub.add_parser("auto", help="standing approvals: D21/D22 (close) or D24 (none); the rest to ask")
    ra.add_argument("--kind", choices=["close", "weak", "none"], default="close")
    ra.add_argument("--limit", type=int, default=20)
    ra.add_argument("--apply", action="store_true", help="record the D21 approvals (otherwise dry run)")
    rd = rsub.add_parser("decide", help="record a user-approved batch of decisions (JSON list)")
    rd.add_argument("--file", default="-", help="JSON file, or - for stdin")
    rd.add_argument("--by", choices=["agent", "user"], required=True,
                    help="agent: agent proposed, user approved the batch; user: user decided directly")
    rev.set_defaults(func=cmd_review)

    rt = sub.add_parser("retag", help="relabel an imported album by fingerprint (dry run without --apply)")
    rt.add_argument("--scan", action="store_true", help="list imported albums with swapped-track evidence")
    rt.add_argument("--album", help="album key")
    rt.add_argument("--apply", action="store_true")
    rt.add_argument("--by", choices=["agent", "user"], default="user")
    rt.add_argument("--reason")
    rt.set_defaults(func=cmd_retag)

    mg = sub.add_parser("merge", help="combine folders of one release into one album and re-match it")
    mg.add_argument("--suggest", action="store_true", help="list albums sharing a top-candidate release")
    mg.add_argument("--albums", nargs="+", help="album keys to merge; the first names the result")
    mg.add_argument("--apply", action="store_true")
    mg.set_defaults(func=cmd_merge)

    rs = sub.add_parser("resync", help="D25: re-fetch non-Latin-script albums so English artist names apply")
    rs.add_argument("--apply", action="store_true")
    rs.add_argument("--limit", type=int)
    rs.set_defaults(func=cmd_resync)

    ing = sub.add_parser("ingest", help="M5: take new folders from the inbox through the pipeline")
    isub = ing.add_subparsers(dest="ingest_cmd", required=True)
    isub.add_parser("list", help="folders waiting in the inbox, and claimed batches")
    ic = isub.add_parser("claim", help="D28: move a folder to inbox/.processed/<date>/ (dry run without --apply)")
    ic.add_argument("folder", help="folder name inside inbox_dir")
    ic.add_argument("--apply", action="store_true")
    ic.add_argument("--by", choices=["agent", "user"], default="user")
    isc = isub.add_parser("scan", help="inventory + AcoustID + verify for one batch (read-only on files)")
    isc.add_argument("--batch", required=True, help="batch id or folder name")
    isc.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    idd = isub.add_parser("dedupe", help="D30: duplicates within the batch and against the library (proposals)")
    idd.add_argument("--batch", required=True, help="batch id or folder name")
    ing.set_defaults(func=cmd_ingest)

    rep = sub.add_parser("report", help="summarize the inventory as JSON")
    rep.add_argument("--top", type=int, default=10, help="examples per section")
    rep.add_argument("--not-imported", action="store_true",
                     help="D18: status of every dump file; writes state/reports/not-imported-<date>.json")
    rep.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    args.func(config.load(args.config), args)


if __name__ == "__main__":
    main()
