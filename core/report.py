import json
from datetime import datetime, timezone
from pathlib import Path


class StageWriter:
    def __init__(self, report: "Report", name: str):
        self.report = report
        self.name = name
        self.data: dict = {}

    def __enter__(self):
        self.data["started_at"] = datetime.now(timezone.utc).isoformat()
        return self.data

    def __exit__(self, exc_type, exc, tb):
        self.data["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.report.data["stages"][self.name] = self.data
        self.report.save()
        return False


class Report:
    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"schema": "crawl2md/report@1", "job": self.path.parent.name, "stages": {}}

    def stage(self, name: str) -> StageWriter:
        return StageWriter(self, name)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def render_markdown(self) -> str:
        lines = [f"# 리포트 — {self.data.get('job', '')}", ""]
        for name, stage in self.data.get("stages", {}).items():
            lines.append(f"## {name}")
            lines.append("")
            lines.append(f"```json\n{json.dumps(stage, ensure_ascii=False, indent=2)}\n```")
            lines.append("")
        return "\n".join(lines)
