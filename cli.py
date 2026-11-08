import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from core import collect, convert, emit, probe
from core.adapter import load as load_adapter
from core.errors import Crawl2mdError, ValidationError

JOB_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _slugify(root_url: str) -> str:
    last = urlsplit(root_url).path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_-]", "-", last)


def _default_job(adapter, site: str) -> str:
    slug = _slugify(adapter["entry.root_url"])
    date = datetime.now().strftime("%Y%m%d")
    return f"{site}-{slug}-{date}"


def _resolve_job(args, adapter, site: str, work_dir: Path) -> str:
    if args.job:
        job = args.job
    elif getattr(args, "_needs_existing_job", False):
        site_dir = work_dir / site
        candidates = sorted(
            (p for p in site_dir.iterdir() if p.is_dir()) if site_dir.exists() else [],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            print(f"work/{site}/ 아래에 job이 없습니다. --job 을 지정하세요.", file=sys.stderr)
            sys.exit(2)
        job = candidates[0].name
    else:
        job = _default_job(adapter, site)

    if not JOB_ALLOWED_RE.match(job):
        raise ValidationError(f"job 이름에 허용되지 않은 문자가 있습니다: {job!r}")
    return job


def _load(args) -> "Adapter":
    adapter_path = Path(args.adapter_dir) / f"{args.site}.yaml"
    return load_adapter(adapter_path)


def cmd_probe(args):
    # probe 는 어댑터 없이 도는 유일한 단계라 job 을 root_url 에서 직접 만든다 (§3.1).
    job = args.job or f"{args.site}-{_slugify(args.root_url)}-{datetime.now().strftime('%Y%m%d')}"
    if not JOB_ALLOWED_RE.match(job):
        raise ValidationError(f"job 이름에 허용되지 않은 문자가 있습니다: {job!r}")

    work_dir = Path(args.work_dir) / args.site / job
    probe.run(args.root_url, args.site, sample=args.sample, work_dir=work_dir,
              save_fixtures=Path(args.save_fixtures) if args.save_fixtures else None)
    print(f"probe.json: {work_dir / 'probe.json'}")
    print(f"draft adapter: sites/{args.site}.draft.yaml")


def cmd_collect(args):
    adapter = _load(args)
    args._needs_existing_job = False
    job = _resolve_job(args, adapter, args.site, Path(args.work_dir))
    raw_path = collect.run(
        adapter, job, work_dir=Path(args.work_dir), limit=args.limit, resume=not args.no_resume
    )
    print(f"raw.json: {raw_path}")


def cmd_convert(args):
    adapter = _load(args)
    args._needs_existing_job = True
    job = _resolve_job(args, adapter, args.site, Path(args.work_dir))
    out_dir = Path(args.out) if args.out else Path(adapter["output.path"].format(job=job, site=args.site))
    result = convert.run(adapter, job, work_dir=Path(args.work_dir), out_dir=out_dir)
    print(f"documents: {len(result.documents)}, warnings: {len(result.warnings)}")
    print(f"out_dir: {out_dir}")


def cmd_build(args):
    import json

    adapter = _load(args)
    args._needs_existing_job = True
    job = _resolve_job(args, adapter, args.site, Path(args.work_dir))
    out_dir = Path(adapter["output.path"].format(job=job, site=args.site))

    job_dir = Path(args.work_dir) / args.site / job
    raw = json.loads((job_dir / "raw.json").read_text(encoding="utf-8"))
    report_data = json.loads((job_dir / "report.json").read_text(encoding="utf-8"))
    doc_index = report_data["stages"]["convert"]["doc_index"]

    from core.convert import DocResult
    from core.models import TocEntry

    entries_by_id = {e["id"]: TocEntry(**e) for e in raw["toc"]}
    docs_resolved = [
        DocResult(entry=entries_by_id[d["id"]], filename=d["filename"], markdown_len=0, paywalled=d["paywalled"])
        for d in doc_index
        if d["id"] in entries_by_id
    ]

    n_images = report_data["stages"]["convert"]["images"]["unique"]
    emit.write_index(docs_resolved, out_dir, adapter, {**raw["meta"], "n_images": n_images})

    if args.force and adapter["output.target"] == "obsidian":
        dest = Path(adapter["output.path"])
        if dest.exists():
            import shutil

            shutil.rmtree(dest)

    stats = emit.finalize(out_dir, adapter, job)

    from core.report import Report

    report = Report(Path(args.work_dir) / args.site / job / "report.json")
    with report.stage("build") as stage:
        stage.update(stats)
        stage["index"] = adapter["output.index_file"]

    print(f"build: {stats}")


def cmd_run(args):
    cmd_collect(args)
    args.job = args.job or None
    cmd_convert(args)
    cmd_build(args)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="crawl2md")
    parser.add_argument("--adapter-dir", default="sites")
    parser.add_argument("--work-dir", default="work")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe")
    p_probe.add_argument("root_url")
    p_probe.add_argument("--site", required=True)
    p_probe.add_argument("--job")
    p_probe.add_argument("--sample", type=int, default=20)
    p_probe.add_argument("--save-fixtures")
    p_probe.set_defaults(func=cmd_probe)

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--site", required=True)
    p_collect.add_argument("--job")
    p_collect.add_argument("--limit", type=int)
    p_collect.add_argument("--no-resume", action="store_true")
    p_collect.set_defaults(func=cmd_collect)

    p_convert = sub.add_parser("convert")
    p_convert.add_argument("--site", required=True)
    p_convert.add_argument("--job")
    p_convert.add_argument("--out")
    p_convert.set_defaults(func=cmd_convert)

    p_build = sub.add_parser("build")
    p_build.add_argument("--site", required=True)
    p_build.add_argument("--job")
    p_build.add_argument("--force", action="store_true")
    p_build.set_defaults(func=cmd_build)

    p_run = sub.add_parser("run")
    p_run.add_argument("--site", required=True)
    p_run.add_argument("--job")
    p_run.add_argument("--limit", type=int)
    p_run.add_argument("--no-resume", action="store_true")
    p_run.add_argument("--out")
    p_run.add_argument("--force", action="store_true")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else (logging.ERROR if args.quiet else logging.INFO)
    )

    try:
        args.func(args)
    except Crawl2mdError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(2)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"예상치 못한 오류: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
