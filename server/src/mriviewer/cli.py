"""mrictl - index, materialize and inspect from the command line."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import load_config
from .db.session import connect, init_db
from .labels import export_labelset, get_labelset, list_labelsets, load_labelset_files


def _progress(stage: str, done: int, total: int) -> None:
    pct = (100.0 * done / total) if total else 100.0
    sys.stderr.write("\r  %-10s %6d/%-6d %5.1f%%" % (stage, done, total, pct))
    sys.stderr.flush()
    if stage == "done":
        sys.stderr.write("\n")


def cmd_init(args) -> int:
    cfg = load_config(args.config)
    cfg.ensure_dirs()
    conn = init_db(cfg.db_path)
    seeded = load_labelset_files(conn, cfg.labelsets_dir)
    print("database   %s" % cfg.db_path)
    print("cache      %s" % cfg.cache_dir)
    print("labelsets  %s" % (", ".join(seeded) or "(none)"))
    for d in cfg.datasets:
        mark = "ok " if Path(d.root).exists() else "MISSING"
        print("dataset    %-14s %-12s %s  %s" % (d.key, d.adapter, mark, d.root))
    return 0


def cmd_scan(args) -> int:
    from .scan.runner import scan_dataset
    cfg = load_config(args.config)
    cfg.ensure_dirs()
    conn = init_db(cfg.db_path)
    load_labelset_files(conn, cfg.labelsets_dir)
    keys = [args.dataset] if args.dataset else [d.key for d in cfg.datasets if d.enabled]
    rc = 0
    for key in keys:
        ds = cfg.dataset(key)
        if not Path(ds.root).exists():
            print("skip %s: root not found (%s)" % (key, ds.root))
            rc = 1
            continue
        print("scanning %s (%s)" % (key, ds.root))
        t0 = time.time()
        stats = scan_dataset(conn, cfg, ds, limit=args.limit, force=args.force,
                             workers=args.workers or cfg.scan_workers,
                             progress=_progress)
        stats["seconds"] = round(time.time() - t0, 1)
        print("  " + json.dumps(stats, ensure_ascii=False))
    return rc


def cmd_materialize(args) -> int:
    from .scan.runner import materialize_series
    from .seg.ingest import materialize_segmentation
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    if args.series:
        ids = [int(args.series)]
    else:
        q = ("SELECT s.id FROM series s JOIN study st ON st.id=s.study_id"
             " JOIN patient p ON p.id=st.patient_id JOIN dataset d ON d.id=p.dataset_id"
             " WHERE s.volume_state!='ready'")
        params: list = []
        if args.dataset:
            q += " AND d.key=?"
            params.append(args.dataset)
        q += " ORDER BY s.id"
        if args.limit:
            q += " LIMIT %d" % args.limit
        ids = [int(r["id"]) for r in conn.execute(q, params).fetchall()]
    print("materializing %d series" % len(ids))
    ok = fail = 0
    for n, sid in enumerate(ids, 1):
        try:
            meta = materialize_series(conn, cfg, sid, force=args.force)
            ok += 1
            _progress("volume", n, len(ids))
        except Exception as exc:  # noqa: BLE001
            fail += 1
            conn.execute("UPDATE series SET volume_state='failed', volume_error=? WHERE id=?",
                         ("%s: %s" % (type(exc).__name__, exc), sid))
            conn.commit()
            sys.stderr.write("\n  series %d failed: %s\n" % (sid, exc))
    _progress("done", len(ids), len(ids))
    print("  volumes ok=%d failed=%d" % (ok, fail))

    seg_q = ("SELECT sg.id FROM segmentation sg JOIN dataset d ON d.id=sg.dataset_id"
             " WHERE sg.series_id IS NOT NULL AND (sg.seg_state!='ready' OR ?)")
    seg_params: list = [1 if args.force else 0]
    if args.dataset:
        seg_q += " AND d.key=?"
        seg_params.append(args.dataset)
    seg_ids = [int(r["id"]) for r in conn.execute(seg_q, seg_params).fetchall()]
    sok = sfail = 0
    for n, gid in enumerate(seg_ids, 1):
        try:
            materialize_segmentation(conn, cfg, gid, force=args.force)
            sok += 1
        except Exception as exc:  # noqa: BLE001
            sfail += 1
            sys.stderr.write("  segmentation %d failed: %s\n" % (gid, exc))
        _progress("labelmap", n, len(seg_ids))
    if seg_ids:
        _progress("done", len(seg_ids), len(seg_ids))
    print("  labelmaps ok=%d failed=%d" % (sok, sfail))
    return 0 if fail == 0 and sfail == 0 else 1


def cmd_mesh(args) -> int:
    from .seg.mesh import build_meshes_for_segmentation
    if getattr(args, "terracing_report", False):
        return _terracing_report(args)
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    q = "SELECT id FROM segmentation WHERE seg_state='ready'"
    params: list = []
    if args.segmentation:
        q = "SELECT id FROM segmentation WHERE id=?"
        params = [int(args.segmentation)]
    elif args.limit:
        q += " ORDER BY id LIMIT %d" % args.limit
    ids = [int(r["id"]) for r in conn.execute(q, params).fetchall()]
    print("building meshes for %d segmentations" % len(ids))
    for n, sid in enumerate(ids, 1):
        try:
            built = build_meshes_for_segmentation(conn, cfg, sid, force=args.force)
            _progress("mesh", n, len(ids))
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("\n  segmentation %d: %s\n" % (sid, exc))
    _progress("done", len(ids), len(ids))
    return 0


def cmd_labelset(args) -> int:
    cfg = load_config(args.config)
    conn = connect(cfg.db_path, readonly=not args.export)
    if args.export:
        out = export_labelset(conn, args.key, Path(args.export))
        print("wrote %s" % out)
        return 0
    if args.key:
        print(json.dumps(get_labelset(conn, args.key), ensure_ascii=False, indent=2))
    else:
        for ls in list_labelsets(conn):
            print("%-30s %s (%d labels)" % (ls["key"], ls["name"], len(ls["labels"])))
    return 0


def cmd_stats(args) -> int:
    cfg = load_config(args.config)
    conn = connect(cfg.db_path, readonly=True)
    rows = conn.execute(
        "SELECT d.key, COUNT(DISTINCT p.id) patients, COUNT(DISTINCT st.id) studies,"
        " COUNT(s.id) series,"
        " SUM(CASE WHEN s.volume_state='ready' THEN 1 ELSE 0 END) ready"
        " FROM dataset d LEFT JOIN patient p ON p.dataset_id=d.id"
        " LEFT JOIN study st ON st.patient_id=p.id LEFT JOIN series s ON s.study_id=st.id"
        " GROUP BY d.key ORDER BY d.key").fetchall()
    print("%-16s %9s %8s %8s %8s" % ("dataset", "patients", "studies", "series", "cached"))
    for r in rows:
        print("%-16s %9d %8d %8d %8d" % (r["key"], r["patients"] or 0, r["studies"] or 0,
                                         r["series"] or 0, r["ready"] or 0))
    segs = conn.execute(
        "SELECT d.key, sg.origin, COUNT(*) n,"
        " SUM(CASE WHEN sg.seg_state='ready' THEN 1 ELSE 0 END) ready"
        " FROM segmentation sg JOIN dataset d ON d.id=sg.dataset_id"
        " GROUP BY d.key, sg.origin ORDER BY d.key").fetchall()
    if segs:
        print("\n%-16s %-14s %6s %8s" % ("dataset", "origin", "segs", "cached"))
        for r in segs:
            print("%-16s %-14s %6d %8d" % (r["key"], r["origin"], r["n"], r["ready"] or 0))
    errs = conn.execute("SELECT kind, COUNT(*) n FROM scan_error GROUP BY kind").fetchall()
    if errs:
        print("\nerrors: " + ", ".join("%s=%d" % (r["kind"], r["n"]) for r in errs))
    return 0


def cmd_doctor(args) -> int:
    """Assert every runtime dependency imports. Catches a wrong-ABI wheel at
    install time rather than three weeks later on the first 3D click."""
    rc = 0
    for mod, why in [("numpy", "arrays"), ("pydicom", "DICOM"), ("nibabel", "NIfTI"),
                     ("SimpleITK", "resampling"), ("scipy", "downsampling"),
                     ("openpyxl", "patient list.xlsx"), ("yaml", "label sets"),
                     ("fastapi", "API"), ("uvicorn", "server"), ("vtk", "3D meshes")]:
        try:
            m = __import__(mod)
            v = getattr(m, "__version__", getattr(m, "VTK_VERSION", "?"))
            print("  ok      %-12s %-10s (%s)" % (mod, v, why))
        except Exception as exc:  # noqa: BLE001
            lvl = "MISSING" if mod != "vtk" else "warn   "
            print("  %s %-12s %-10s (%s) - %s" % (lvl, mod, "", why, exc))
            if mod != "vtk":
                rc = 1
    try:
        cfg = load_config(args.config)
        print("  ok      config       %s" % getattr(cfg, "config_path", "?"))
        for d in cfg.datasets:
            print("  %s dataset      %-12s %s"
                  % ("ok     " if Path(d.root).exists() else "MISSING", d.key, d.root))
        cfg.ensure_dirs()
        print("  ok      state_dir    %s" % cfg.state_dir)
    except Exception as exc:  # noqa: BLE001
        print("  MISSING config       %s" % exc)
        rc = 1
    return rc


def cmd_serve(args) -> int:
    import uvicorn
    cfg = load_config(args.config)
    uvicorn.run("mriviewer.main:app", host=args.host or cfg.bind_host,
                port=args.port or cfg.bind_port, reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mrictl")
    ap.add_argument("-c", "--config")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    p = sub.add_parser("scan", help="tier-1 index of a dataset")
    p.add_argument("-d", "--dataset")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("materialize", help="tier-2 volume + labelmap cache")
    p.add_argument("-d", "--dataset")
    p.add_argument("-s", "--series")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_materialize)

    p = sub.add_parser("mesh", help="precompute 3D label surfaces")
    p.add_argument("--terracing-report", action="store_true",
                   help="print slice-facing area fraction, old vs new level set, "
                        "for the chosen segmentations; builds nothing")
    p.add_argument("-g", "--segmentation")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_mesh)

    p = sub.add_parser("labelset")
    p.add_argument("key", nargs="?")
    p.add_argument("--export")
    p.set_defaults(func=cmd_labelset)

    sub.add_parser("stats").set_defaults(func=cmd_stats)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    p = sub.add_parser("serve")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())


def _terracing_report(args) -> int:
    """Old vs new display level set, measured rather than eyeballed.

    Prints, per label, the fraction of surface area whose normal lies within
    15 degrees of the slice axis. Terraces face the slice axis; a smooth
    surface only does where the anatomy is genuinely flat.
    """
    import numpy as np

    from .config import load_config
    from .db.session import connect
    from .seg.ingest import series_geometry
    from .seg.mesh import (_choose_level_set, build_label_mesh,
                           slice_facing_fraction)
    from .volume.cache import VolumeCache

    cfg = load_config(getattr(args, "config", None))
    conn = connect(cfg.db_path, readonly=True)
    q = "SELECT id FROM segmentation WHERE seg_state='ready'"
    params: list = []
    if args.segmentation:
        q = "SELECT id FROM segmentation WHERE id=?"
        params = [int(args.segmentation)]
    elif args.limit:
        q += " ORDER BY id LIMIT %d" % args.limit
    ids = [int(r["id"]) for r in conn.execute(q, params).fetchall()]
    cache = VolumeCache(cfg.cache_dir)
    print("%6s %6s %10s %10s %8s  %s" % ("seg", "label", "antialias", "shape", "change", "auto->"))
    for sid in ids:
        row = conn.execute("SELECT * FROM segmentation WHERE id=?", (sid,)).fetchone()
        if row is None or not row["seg_key"]:
            continue
        entry = cache.segmentation(row["seg_key"])
        meta = entry.read_meta()
        d = meta["dimensions"]
        canonical = entry.read_array((d[2], d[1], d[0]), "uint8")
        geom, _ = series_geometry(conn, int(row["series_id"]))
        for value in meta.get("presentValues", []):
            mask = canonical == value
            old = build_label_mesh(mask, geom, level_set="antialias")
            new = build_label_mesh(mask, geom, level_set="shape_interp")
            if old is None or new is None:
                continue
            f_old = slice_facing_fraction(old[0], old[1], geom.normal)
            f_new = slice_facing_fraction(new[0], new[1], geom.normal)
            idx = np.argwhere(mask)
            lo, hi = idx.min(0), idx.max(0) + 1
            chosen = _choose_level_set(
                np.ascontiguousarray(mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]),
                (geom.spacing[2], geom.spacing[1], geom.spacing[0]))
            print("%6d %6d %9.1f%% %9.1f%% %7.0f%%  %s" % (
                sid, value, 100 * f_old, 100 * f_new,
                100 * (f_new - f_old) / f_old if f_old else 0.0, chosen))
    return 0
