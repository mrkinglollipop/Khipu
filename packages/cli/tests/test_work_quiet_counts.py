"""Synthetic Work counts; quiet commitments stay open and visible."""
from datetime import timedelta

from khipu import work
from tests.test_work import NOW, NO_LEDGER, ROUTING, cfg


def test_quiet_is_separate_at_fourteen_days(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    items = [{"id": i, "project": "example/widget", "text": f"Synthetic task {i}",
              "owner": "user", "status": "open", "kind": kind,
              "opened_at": (NOW - timedelta(days=days)).isoformat()}
             for i, kind, days in [(1, "blocker", 14), (2, "followup", 14),
                                   (3, "question", 13), (4, "promise", 40)]]
    items[-1]["last_seen_at"] = (NOW - timedelta(days=1)).isoformat()
    view = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER,
                           owed={"items": items}, routing_info=ROUTING)
    assert view["counts"]["waiting"] == 2
    assert view["counts"]["quiet"] == 2
    assert view["counts"]["blocking"] == 0
    assert len(view["waiting"]) == 4
    assert all(i["status"] == "open" for i in items)
    assert {i["id"] for i in view["waiting"] if i.get("quiet")} == {"1", "2"}
    assert "2 quiet" in work.render_text(view)
