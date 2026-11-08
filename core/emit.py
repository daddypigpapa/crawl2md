import shutil
import zipfile
from pathlib import Path
from urllib.parse import quote

from core.adapter import Adapter
from core.convert import DocResult


def write_index(docs: list[DocResult], out_dir: Path, adapter: Adapter, meta: dict) -> Path | None:
    index_file = adapter["output.index_file"]
    if not index_file:
        return None

    book_title = meta.get("book_title") or ""
    root_url = meta.get("root_url", "")
    license_ = adapter["license"]
    n_images = meta.get("n_images", 0)

    lines = [f"# {book_title} — 목차", ""]
    lines.append(f"원본: <{root_url}> · 라이선스: {license_}")
    lines.append("")
    lines.append(f"총 {len(docs)}개 문서 · 이미지 {n_images}개")
    lines.append("")

    for doc in sorted(docs, key=lambda d: d.entry.i):
        stem = doc.filename[:-3] if doc.filename.endswith(".md") else doc.filename
        title = doc.entry.title
        if adapter["output.index_wikilink"]:
            link = f"[[{stem}|{title}]]"
        else:
            link = f"[{title}]({quote(doc.filename)})"
        suffix = " *(유료·비공개)*" if doc.paywalled else ""
        lines.append(f"- {link}{suffix}")

    path = out_dir / index_file
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def finalize(out_dir: Path, adapter: Adapter, job: str) -> dict:
    target = adapter["output.target"]
    files = list(out_dir.rglob("*"))
    file_list = [f for f in files if f.is_file()]
    total_bytes = sum(f.stat().st_size for f in file_list)

    license_ = adapter["license"]
    if license_ == "unknown" or "NC" in license_ or "ND" in license_:
        print(f"주의: 라이선스가 '{license_}' 입니다. 재배포 조건을 확인하세요.")

    result = {"target": target, "files": len(file_list), "bytes": total_bytes}

    if target == "folder":
        result["path"] = str(out_dir)
    elif target == "zip":
        zip_path = Path(adapter["output.path"])
        if not str(zip_path).endswith(".zip"):
            zip_path = zip_path.with_suffix(".zip")
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in file_list:
                zf.write(f, arcname=f"{job}/{f.relative_to(out_dir)}")
        result["path"] = str(zip_path)
    elif target == "obsidian":
        dest = Path(adapter["output.path"])
        if dest.exists():
            raise FileExistsError(f"{dest} 가 이미 존재합니다. --force 를 지정하세요.")
        shutil.copytree(out_dir, dest)
        result["path"] = str(dest)

    return result
