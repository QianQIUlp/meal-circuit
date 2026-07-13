from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


FORBIDDEN_ROOTS = {"data", "tmp", "exports", "backups"}
FORBIDDEN_NAMES = {"doctrine.private.md", "profile.md", "settings.private.json", "减脂增肌饮食系统总纲.md"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
ALLOWED_IMAGE_ROOTS = {("docs", "assets"), ("tests", "fixtures")}
TEXT_SUFFIXES = {".py", ".md", ".ps1", ".json", ".toml", ".txt", ".yml", ".yaml", ".example"}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "email": re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    "china_mobile": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}


def candidate_files(root: Path) -> list[Path]:
    if (root / ".git").is_dir():
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return sorted(root / line for line in result.stdout.splitlines() if line)
    return sorted(path for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts)


def scan(root: str | Path) -> list[dict[str, str]]:
    root = Path(root).resolve()
    findings: list[dict[str, str]] = []
    home_text = str(Path.home()).lower()
    for path in candidate_files(root):
        relative = path.relative_to(root)
        # Private runtime roots are forbidden at the repository boundary. A
        # conventional source package named ``data`` is not itself a leak.
        if relative.parts[0].lower() in FORBIDDEN_ROOTS:
            findings.append({"path": str(relative), "reason": "forbidden_private_directory"})
            continue
        lower_name = relative.name.lower()
        if lower_name in {name.lower() for name in FORBIDDEN_NAMES} and relative.parts[0].lower() != "templates":
            findings.append({"path": str(relative), "reason": "forbidden_private_file"})
        if lower_name.startswith("context") and path.suffix.lower() == ".json":
            findings.append({"path": str(relative), "reason": "generated_context"})
        if lower_name.startswith("result") and path.suffix.lower() == ".json":
            findings.append({"path": str(relative), "reason": "generated_result"})
        try:
            prefix = path.read_bytes()[:32]
        except OSError as exc:
            findings.append({"path": str(relative), "reason": f"unreadable:{exc}"})
            continue
        if prefix.startswith(b"SQLite format 3\x00") or ".db" in lower_name:
            findings.append({"path": str(relative), "reason": "sqlite_database"})
        if path.suffix.lower() in IMAGE_SUFFIXES:
            pair = tuple(part.lower() for part in relative.parts[:2])
            if pair not in ALLOWED_IMAGE_ROOTS:
                findings.append({"path": str(relative), "reason": "unapproved_image_location"})
            if b"Exif\x00\x00" in path.read_bytes()[:256 * 1024]:
                findings.append({"path": str(relative), "reason": "image_contains_exif"})
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"LICENSE"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if home_text and home_text in text.lower():
            findings.append({"path": str(relative), "reason": "absolute_user_home_path"})
        for name, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append({"path": str(relative), "reason": name})
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description="MealCircuit 开源发布隐私检查")
    parser.add_argument("--root", default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    findings = scan(args.root)
    print(json.dumps({"ok": not findings, "findings": findings}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
