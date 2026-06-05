#!/usr/bin/env python3
"""Build Xiaowei's durable contact extension index.

The gateway reads ~/.hermes/profiles/wechat/data/contact_directory.json for
fast extension lookups. This importer accepts CSV/TSV/JSON/free text, and PDF
when PyMuPDF is installed.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable


DEFAULT_OUTPUT = Path.home() / ".hermes/profiles/wechat/data/contact_directory.json"

EXT_KEYS = {"extension", "ext", "分機", "分机", "分機號碼", "分机号码", "phone_ext"}
ENGLISH_KEYS = {"english_name", "english", "eng", "英文名", "英文姓名", "英文"}
CHINESE_KEYS = {"chinese_name", "chinese", "中文名", "中文姓名", "姓名", "name_cn"}
NAME_KEYS = {"name", "姓名", "員工姓名", "员工姓名", "display_name"}
ALIAS_KEYS = {"aliases", "alias", "別名", "别名"}


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip()


def normalize_key(value: str) -> str:
    text = normalize_text(value).lower()
    return re.sub(r"[\s　._\-・·/\\()（）［］\[\]{}<>《》「」『』:：,，。!?！？]+", "", text)


def first_raw_value(row: dict[str, Any], keys: set[str]) -> Any:
    normalized = {normalize_key(str(key)): value for key, value in row.items()}
    for key in keys:
        value = normalized.get(normalize_key(key))
        if value is not None:
            return value
    return ""


def first_value(row: dict[str, Any], keys: set[str]) -> str:
    value = first_raw_value(row, keys)
    if isinstance(value, list):
        return normalize_text(" ".join(str(item) for item in value))
    return normalize_text(str(value))


def split_aliases(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        aliases: list[str] = []
        for item in value:
            aliases.extend(split_aliases(item))
        return aliases
    parts = re.split(r"[,，;/、\s]+", normalize_text(str(value)))
    return [part for part in (normalize_text(part) for part in parts) if part]


def make_entry(
    *,
    english_name: str = "",
    chinese_name: str = "",
    name: str = "",
    extension: str,
    aliases: Iterable[str] = (),
    source_line: str = "",
) -> dict[str, Any] | None:
    extension = normalize_text(extension)
    extension_match = re.search(r"\b\d{3,6}\b", extension)
    if not extension_match:
        return None
    extension = extension_match.group(0)

    english_name = normalize_text(english_name)
    chinese_name = normalize_text(chinese_name)
    name = normalize_text(name)

    if not english_name and name and re.search(r"[A-Za-z]", name):
        english_name = name
    if not chinese_name and name and re.search(r"[\u4e00-\u9fff]", name):
        chinese_name = name
    if not (english_name or chinese_name or name):
        return None

    alias_set: dict[str, str] = {}
    for alias in [name, english_name, chinese_name, *aliases]:
        alias = normalize_text(str(alias))
        key = normalize_key(alias)
        if alias and key:
            alias_set.setdefault(key, alias)

    entry: dict[str, Any] = {
        "english_name": english_name,
        "chinese_name": chinese_name,
        "extension": extension,
        "aliases": sorted(alias_set.values(), key=lambda value: normalize_key(value)),
    }
    if name and name not in {english_name, chinese_name}:
        entry["name"] = name
    if source_line:
        entry["source_line"] = source_line[:240]
    return entry


def load_existing(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return [entry for entry in entries if isinstance(entry, dict)]


def parse_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]]
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        rows = [row for row in data["entries"] if isinstance(row, dict)]
    elif isinstance(data, list):
        rows = [row for row in data if isinstance(row, dict)]
    else:
        rows = []
    return [entry for row in rows if (entry := entry_from_row(row))]


def parse_delimited(path: Path) -> list[dict[str, Any]]:
    sample = path.read_text(encoding="utf-8-sig")
    dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.Sniffer().sniff(sample[:4096], delimiters=",\t;")
    reader = csv.DictReader(sample.splitlines(), dialect=dialect)
    return [entry for row in reader if (entry := entry_from_row(dict(row)))]


def parse_xlsx(path: Path) -> list[dict[str, Any]]:
    import zipfile
    import xml.etree.ElementTree as ET

    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(".//a:si", ns):
                shared.append("".join(text.text or "" for text in item.findall(".//a:t", ns)))
        sheet_name = next((name for name in archive.namelist() if name.startswith("xl/worksheets/sheet")), "")
        if not sheet_name:
            return []
        root = ET.fromstring(archive.read(sheet_name))

    rows: list[list[str]] = []
    for row_node in root.findall(".//a:row", ns):
        row: list[str] = []
        for cell in row_node.findall("a:c", ns):
            value_node = cell.find("a:v", ns)
            value = value_node.text if value_node is not None else ""
            if cell.attrib.get("t") == "s" and value.isdigit():
                value = shared[int(value)] if int(value) < len(shared) else ""
            row.append(normalize_text(value or ""))
        if any(row):
            rows.append(row)
    if not rows:
        return []
    headers = rows[0]
    entries: list[dict[str, Any]] = []
    for values in rows[1:]:
        row = {headers[index]: value for index, value in enumerate(values) if index < len(headers)}
        if entry := entry_from_row(row):
            entries.append(entry)
    return entries


def read_pdf_text(path: Path) -> str:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("PDF import needs PyMuPDF installed in this Python environment") from exc
    with fitz.open(path) as document:  # type: ignore[attr-defined]
        return "\n".join(page.get_text("text") for page in document)


def parse_free_text(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = normalize_text(raw_line)
        if not line:
            continue
        ext_match = re.search(r"(?:分機|分机|ext\.?|extension)?\s*[:：#]?\s*(\d{3,6})\b", line, flags=re.I)
        if not ext_match:
            continue
        extension = ext_match.group(1)
        before = line[: ext_match.start()].strip(" \t,，|/:-：")
        after = line[ext_match.end() :].strip(" \t,，|/:-：")
        name_part = before or after
        english = " ".join(re.findall(r"\b[A-Za-z][A-Za-z .'-]{1,30}\b", name_part)).strip()
        chinese_match = re.search(r"[\u4e00-\u9fff]{2,5}", name_part)
        chinese = chinese_match.group(0) if chinese_match else ""
        if entry := make_entry(
            english_name=english,
            chinese_name=chinese,
            name=name_part,
            extension=extension,
            source_line=line,
        ):
            entries.append(entry)
    return entries


def parse_stacked_text(text: str) -> list[dict[str, Any]]:
    """Parse OCR/PDF text where a contact is split across adjacent lines.

    Common exported directory shape:

        Terry
        林淑媛
        15201
        0937-030-628
    """
    lines = [normalize_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    entries: list[dict[str, Any]] = []
    for index in range(max(0, len(lines) - 2)):
        english = lines[index]
        chinese = lines[index + 1]
        extension = lines[index + 2]
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,30}", english):
            continue
        if not re.fullmatch(r"[\u4e00-\u9fff]{2,5}", chinese):
            continue
        if not re.fullmatch(r"\d{3,6}", extension):
            continue
        if entry := make_entry(
            english_name=english,
            chinese_name=chinese,
            extension=extension,
            source_line=" / ".join(lines[index:index + 3]),
        ):
            entries.append(entry)
    return entries


def entry_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    extension = first_value(row, EXT_KEYS)
    name = first_value(row, NAME_KEYS)
    english_name = first_value(row, ENGLISH_KEYS)
    chinese_name = first_value(row, CHINESE_KEYS)
    aliases = split_aliases(first_raw_value(row, ALIAS_KEYS))
    if not extension:
        for value in row.values():
            text = normalize_text(str(value))
            if re.fullmatch(r"\d{3,6}", text):
                extension = text
                break
    return make_entry(
        english_name=english_name,
        chinese_name=chinese_name,
        name=name,
        extension=extension,
        aliases=aliases,
    )


def parse_input(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return parse_json(path)
    if suffix in {".csv", ".tsv"}:
        return parse_delimited(path)
    if suffix == ".xlsx":
        return parse_xlsx(path)
    if suffix == ".pdf":
        text = read_pdf_text(path)
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    return [*parse_free_text(text), *parse_stacked_text(text)]


def merge_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        extension = normalize_text(str(entry.get("extension") or ""))
        if not extension:
            continue
        english = normalize_text(str(entry.get("english_name") or ""))
        chinese = normalize_text(str(entry.get("chinese_name") or ""))
        name = normalize_text(str(entry.get("name") or ""))
        key = (normalize_key(english or name), normalize_key(chinese or name), extension)
        current = merged.setdefault(key, {"aliases": []})
        for field in ("english_name", "chinese_name", "name", "extension", "source_line"):
            value = normalize_text(str(entry.get(field) or ""))
            if value and not current.get(field):
                current[field] = value
        aliases = current.setdefault("aliases", [])
        for alias in entry.get("aliases") or []:
            alias = normalize_text(str(alias))
            if alias and alias not in aliases:
                aliases.append(alias)
    for entry in merged.values():
        entry["aliases"] = sorted(entry.get("aliases", []), key=lambda value: normalize_key(str(value)))
    return sorted(
        merged.values(),
        key=lambda entry: (
            normalize_key(str(entry.get("english_name") or entry.get("name") or "")),
            normalize_key(str(entry.get("chinese_name") or "")),
            str(entry.get("extension") or ""),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Contact directory source file")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--merge", action="store_true", help="Merge with existing output instead of replacing it")
    parser.add_argument("--source", default="", help="Human-readable source label")
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 2

    entries = parse_input(args.input)
    if args.merge:
        entries = [*load_existing(args.output), *entries]
    entries = merge_entries(entries)

    payload = {
        "version": 1,
        "source": args.source or str(args.input),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "entries": entries,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(entries)} contact(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
