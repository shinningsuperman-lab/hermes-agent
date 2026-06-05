from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_importer():
    script = Path(__file__).resolve().parents[2] / "scripts" / "import_contact_directory.py"
    spec = importlib.util.spec_from_file_location("import_contact_directory", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_csv_and_merge(tmp_path):
    importer = _load_importer()
    source = tmp_path / "contacts.csv"
    output = tmp_path / "contact_directory.json"
    source.write_text(
        "english_name,chinese_name,extension,aliases\n"
        "Terry,陳泰瑞,1234,terry chen;陳先生\n"
        "David,周家緯,2668,\n",
        encoding="utf-8",
    )

    assert importer.main([str(source), "-o", str(output)]) == 0
    data = output.read_text(encoding="utf-8")

    assert '"english_name": "Terry"' in data
    assert '"chinese_name": "陳泰瑞"' in data
    assert '"extension": "1234"' in data
    assert "陳先生" in data


def test_parse_free_text_line():
    importer = _load_importer()

    entries = importer.parse_free_text("Terry 陳泰瑞 分機 1234\nNora 馮薰潁 ext: 15503")

    assert [entry["extension"] for entry in entries] == ["1234", "15503"]
    assert entries[0]["english_name"] == "Terry"
    assert entries[0]["chinese_name"] == "陳泰瑞"


def test_parse_stacked_text_contact_block():
    importer = _load_importer()

    entries = importer.parse_stacked_text(
        "0968-370-922\n"
        "Terry\n"
        "林淑媛\n"
        "15201\n"
        "0937-030-628\n"
    )

    assert len(entries) == 1
    assert entries[0]["english_name"] == "Terry"
    assert entries[0]["chinese_name"] == "林淑媛"
    assert entries[0]["extension"] == "15201"


def test_parse_json_keeps_alias_list(tmp_path):
    importer = _load_importer()
    source = tmp_path / "contacts.json"
    source.write_text(
        '{"entries":[{"english_name":"David","chinese_name":"周家緯","extension":"2668",'
        '"aliases":["david","周家緯"]}]}',
        encoding="utf-8",
    )

    entries = importer.parse_json(source)

    assert entries[0]["aliases"] == ["David", "周家緯"]
