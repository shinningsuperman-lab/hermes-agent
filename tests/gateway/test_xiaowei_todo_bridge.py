import json

from gateway import run


def test_xiaowei_todo_extract_accepts_space_form():
    assert run._extract_xiaowei_todo_add("待辦 數字化的 MPS 要研究一下") == "數字化的 MPS 要研究一下"
    assert (
        run._extract_xiaowei_todo_add("待辦 下週要跟值日生 改一下 lenovo系統量")
        == "下週要跟值日生 改一下 lenovo系統量"
    )


def test_xiaowei_todo_sync_bridges_session_tool_to_durable_store(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "todos.json").write_text(
        json.dumps({"version": 1, "items": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    messages = [
        {
            "role": "tool",
            "name": "todo",
            "content": json.dumps(
                {
                    "todos": [
                        {
                            "id": "research-digital-mps",
                            "content": "數字化的 MPS 要研究一下",
                            "status": "pending",
                        },
                        {
                            "id": "done-item",
                            "content": "已完成的不該同步",
                            "status": "completed",
                        },
                    ]
                },
                ensure_ascii=False,
            ),
        }
    ]

    assert run._sync_xiaowei_todos_from_agent_messages(messages, trigger_text="待辦 新增一下") == 1
    assert run._sync_xiaowei_todos_from_agent_messages(messages, trigger_text="待辦 新增一下") == 0

    data = json.loads((data_dir / "todos.json").read_text(encoding="utf-8"))
    assert [item["content"] for item in data["items"]] == ["數字化的 MPS 要研究一下"]


def test_xiaowei_todo_sync_ignores_non_todo_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "todos.json").write_text(
        json.dumps({"version": 1, "items": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    messages = [
        {
            "role": "tool",
            "name": "todo",
            "content": json.dumps(
                {"todos": [{"id": "plan", "content": "內部工作計畫", "status": "pending"}]},
                ensure_ascii=False,
            ),
        }
    ]

    assert run._sync_xiaowei_todos_from_agent_messages(messages, trigger_text="幫我分析一下") == 0
    data = json.loads((data_dir / "todos.json").read_text(encoding="utf-8"))
    assert data["items"] == []


def test_xiaowei_multiline_todo_batch_uses_durable_store(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "todos.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "existing",
                        "content": "KH86 庫存跟 UTS 要調整",
                        "status": "open",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reply = run._handle_xiaowei_todo_batch(
        "待辦 測試 durable bridge 001\n待辦更新\n今日待辦呢？"
    )

    assert reply is not None
    assert "今日待辦有 2 項" in reply
    assert "**KH86 庫存跟 UTS 要調整**" in reply
    assert "**測試 durable bridge 001**" in reply
    assert "今日待辦目前有 6 項" not in reply

    data = json.loads((data_dir / "todos.json").read_text(encoding="utf-8"))
    assert [item["content"] for item in data["items"]] == [
        "KH86 庫存跟 UTS 要調整",
        "測試 durable bridge 001",
    ]
