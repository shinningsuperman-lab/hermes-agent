import json
from types import SimpleNamespace

from gateway import run


def test_xiaowei_todo_extract_accepts_space_form():
    assert run._extract_xiaowei_todo_add("待辦 數字化的 MPS 要研究一下") == "數字化的 MPS 要研究一下"
    assert (
        run._extract_xiaowei_todo_add("待辦 下週要跟值日生 改一下 lenovo系統量")
        == "下週要跟值日生 改一下 lenovo系統量"
    )


def test_xiaowei_cronjob_json_leak_is_repaired_as_reminder(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    monkeypatch.setattr(run, "_is_xiaowei_wechat_profile", lambda platform: str(platform) == "weixin")

    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", tmp_path / "cron" / "output")

    source = SimpleNamespace(platform="weixin", chat_id="wxid_self", chat_name="小薇")
    leaked = json.dumps(
        {
            "name": "cronjob",
            "arguments": {
                "action": "create",
                "schedule": "2026-06-01T20:00",
                "prompt": "提醒 賣 PANW CRWD",
                "name": "sell_reminder",
                "deliver": "local",
            },
        },
        ensure_ascii=False,
    )

    reply = run._sanitize_internal_instruction_leak(leaked, source)

    assert reply == "排好了：6/1（一）20:00 提醒你賣 PANW CRWD。"
    assert '"name": "cronjob"' not in reply
    jobs = jobs_mod.load_jobs()
    assert len(jobs) == 1
    assert jobs[0]["no_agent"] is True


def test_xiaowei_referential_todo_add_does_not_list_current_todos():
    assert run._is_xiaowei_todo_query("待辦確認") is True
    assert run._is_xiaowei_todo_query("把這兩件事情加到待辦裡面") is False
    assert run._handle_xiaowei_todo_batch("把這兩件事情加到待辦裡面") is None


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


def test_xiaowei_todo_complete_uses_visible_open_indices(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "todos.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {"id": "done-old", "content": "已完成舊項", "status": "completed"},
                    {"id": "open-1", "content": "第一個未完成", "status": "open"},
                    {"id": "open-2", "content": "第二個未完成", "status": "open"},
                    {"id": "open-3", "content": "第三個未完成", "status": "open"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reply = run._handle_xiaowei_todo_batch("2 完成了")

    assert reply is not None
    assert "第二個未完成" in reply
    data = json.loads((data_dir / "todos.json").read_text(encoding="utf-8"))
    statuses = {item["id"]: item["status"] for item in data["items"]}
    assert statuses == {
        "done-old": "completed",
        "open-1": "open",
        "open-2": "completed",
        "open-3": "open",
    }


def test_xiaowei_todo_complete_and_query_in_one_bubble(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "todos.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {"id": "a", "content": "A", "status": "open"},
                    {"id": "b", "content": "B", "status": "open"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reply = run._handle_xiaowei_todo_batch("1 完成了\n今日待辦")

    assert reply is not None
    assert "已完成" in reply
    assert "今日待辦有 1 項" in reply
    assert "**B**" in reply


def test_xiaowei_todo_complete_accepts_multiple_numbers_with_filler(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "todos.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {"id": f"open-{idx}", "content": f"待辦 {idx}", "status": "open"}
                    for idx in range(1, 14)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reply = run._handle_xiaowei_todo_batch("12，13 也完成了")

    assert reply is not None
    assert "第 12 項" in reply
    assert "第 13 項" in reply
    data = json.loads((data_dir / "todos.json").read_text(encoding="utf-8"))
    statuses = {item["id"]: item["status"] for item in data["items"]}
    assert statuses["open-12"] == "completed"
    assert statuses["open-13"] == "completed"
    assert statuses["open-11"] == "open"


def test_xiaowei_todo_relist_short_command_uses_durable_store(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "todos.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {"id": "open", "content": "真正存在檔案裡的待辦", "status": "open"}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reply = run._handle_xiaowei_todo_batch("再列一下")

    assert reply is not None
    assert "目前待辦有 1 項" in reply
    assert "**真正存在檔案裡的待辦**" in reply


def test_xiaowei_todo_completion_uses_last_visible_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "todos.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {"id": "a", "content": "第一項", "status": "open"},
                    {"id": "b", "content": "第二項", "status": "open"},
                    {"id": "c", "content": "第三項", "status": "open"},
                    {"id": "d", "content": "第四項", "status": "open"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    listed = run._handle_xiaowei_todo_batch("再列一下")
    assert listed is not None
    assert "3. **第三項**" in listed

    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][1]["status"] = "completed"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    reply = run._handle_xiaowei_todo_batch("3 完成了")

    assert reply is not None
    assert "第三項" in reply
    data = json.loads(path.read_text(encoding="utf-8"))
    statuses = {item["id"]: item["status"] for item in data["items"]}
    assert statuses == {
        "a": "open",
        "b": "completed",
        "c": "completed",
        "d": "open",
    }
