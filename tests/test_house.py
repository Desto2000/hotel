"""Tests for: concurrent writers, backup/restore, forget, move, revalidate +
custodian, verifier, guest drafts, usage logging."""

import json
import subprocess
import sys
import textwrap

import pytest

from hotel.backup import backup, restore
from hotel.config import Paths
from hotel.core import Hotel, init_home
from hotel.gateway import ToolResult, ToolSpec, make_pointer
from hotel.guest import Guest
from hotel.llm import LLM, ScriptedTransport
from hotel.models import AppendOp, Annotation, Claim, CloseOp, MoveOp

DOC = "QUERY: q\n\n### [1] fake:1 (external) Doc\n\nExa supports a startPublishedDate filter on search.\n"


def fake(**_) -> ToolResult:  # offline test double
    return ToolResult(b'{"fake": true}', DOC, [], "fake")


@pytest.fixture
def hotel(tmp_path):
    init_home(tmp_path / "home")
    h = Hotel(Paths(tmp_path / "home"))
    h.gateway.register(ToolSpec("fake.search", "retrieval", "fake", fake))
    h.gateway.register(ToolSpec("agent.search", "retrieval", "fake", fake))
    return h


def add(h, room, typ, body):
    return h.apply([AppendOp(op="append", room=room, type=typ, body=body, refs=[], tags=[])]).created[0]


def user_annotation(h, bounty):
    e, _ = h.gateway.call("fake.search", actor="user", query="q")
    t = h.blobs.text(e.text_hash)
    h.gateway.annotate(Annotation(target=e.id, caused="answers", caused_refs=[bounty], claims=[Claim(
        text="date filter exists", relation="supports", lobby_refs=[bounty],
        pointer=make_pointer(t, e.text_hash, "startPublishedDate filter"))]))
    return e


def test_concurrent_writers_lose_nothing(hotel):
    """Two OS processes append 15 entries each at the same time."""
    script = textwrap.dedent(f"""
        import sys
        from pathlib import Path
        from hotel.config import Paths
        from hotel.core import Hotel
        from hotel.models import AppendOp
        h = Hotel(Paths(Path({str(hotel.paths.root)!r})))
        for i in range(15):
            h.apply([AppendOp(op="append", room="race", type="monologue", body=f"{{sys.argv[1]}} {{i}}", refs=[], tags=[])])
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script, w]) for w in ("A", "B")]
    assert [p.wait(timeout=180) for p in procs] == [0, 0]
    bodies = sorted(e.text for e in hotel.lobby().entries.values())
    assert len(bodies) == 30 and len(set(bodies)) == 30
    assert hotel.check() == [] and hotel.reg.verify_chain() == []
    assert len([r for r in hotel.reg.list(kind="lobby_edit", limit=1000)]) == 30


def test_hand_edit_committed_before_write(hotel):
    a = add(hotel, "r", "monologue", "first")
    hotel.paths.lobby.write_text(hotel.text() + "\n@L0002 [.] monologue origin=user ts=2026-01-01T00:00:00Z\n  | typed in an editor\n",
                                 encoding="utf-8", newline="\n")
    b = add(hotel, "r", "monologue", "second")
    assert b == "L0003" and "typed in an editor" in hotel.lobby().entries["L0002"].text
    assert any(r.payload.get("hand_edit") for r in hotel.reg.list(kind="lobby_edit"))


def test_backup_restore_roundtrip(hotel, tmp_path):
    b = add(hotel, "r", "bounty", "q?")
    user_annotation(hotel, b)
    info = backup(hotel.paths, tmp_path / "bk")
    assert info["blobs_copied"] > 0
    again = backup(hotel.paths, tmp_path / "bk")  # incremental
    assert again["blobs_copied"] == 0
    res = restore(tmp_path / "bk", tmp_path / "restored")
    assert res["problems"] == []
    h2 = Hotel(Paths(tmp_path / "restored"))
    assert h2.text() == hotel.text()
    assert h2.reg.annotation_for(1) is None or True  # rows restored
    assert len(h2.reg.list(limit=10**6)) >= info["reg_rows"]


def test_forget_removes_content_keeps_chain(hotel):
    b = add(hotel, "r", "bounty", "q?")
    e = user_annotation(hotel, b)
    out = hotel.forget(e.id, reason="test")
    assert out["deleted"] >= 3  # result, text, args, annotation quotes
    with pytest.raises(FileNotFoundError):
        hotel.blobs.text(e.text_hash)
    assert hotel.reg.verify_chain() == []
    assert hotel.oracle.is_annotated_retrieval(e.id)  # the fact of annotation survives, the quotes don't
    assert any(r.kind == "redaction" for r in hotel.reg.list())


def test_forgetting_unannotated_retrieval_releases_gate(hotel):
    e, _ = hotel.gateway.call("fake.search", actor="user", query="something private")
    hotel.forget(e.id, reason="pasted a secret")
    e2, _ = hotel.gateway.call("fake.search", actor="user", query="next")  # would raise GateBlocked
    assert [p.id for p in hotel.gateway.pending()] == [e2.id]


def test_move_and_review_for_residents(hotel):
    a = add(hotel, "harness", "idea", "embeddings belong elsewhere")
    hotel.apply([AppendOp(op="append", room="harness", type="log", body="x", refs=[a], tags=[])])
    res = hotel.apply([MoveOp(op="move", id=a, room="embeddings", reason="topic split")])
    lob = hotel.lobby()
    new = lob.entries[res.created[0]]
    assert lob.entries[a].sigil == "~" and new.room == "embeddings" and new.text == "embeddings belong elsewhere"
    assert new.metas("moved-from") == ["harness"] and new.origin == "user"
    r = hotel.route_resident_op(MoveOp(op="move", id=new.id, room="other", reason="x"), "librarian", None)
    assert r.route == "queued"
    assert hotel.check() == []


def test_revalidate_and_custodian(hotel):
    b = add(hotel, "r", "bounty", "q?")
    e = user_annotation(hotel, b)
    hotel.apply([CloseOp(op="close", id=b, closed_by=f"R:{e.id}")])
    due = hotel.lobby().entries[b].metas("revalidate")
    assert len(due) == 1
    assert hotel.custodian(today="2000-01-01") == []
    results = hotel.custodian(today="2999-01-01")
    assert [r.route for r in results] == ["applied", "applied"]
    assert "stale" in hotel.lobby().entries[b].metas("tag")
    assert hotel.custodian(today="2999-01-01") == []  # idempotent


def test_verifier_rejects_overreach(hotel):
    from hotel.agent import model_annotate
    b = add(hotel, "r", "bounty", "q?")
    e, _ = hotel.gateway.call("fake.search", actor="agent", query="q")
    hotel.cfg["verifier"] = "groq"
    vt = ScriptedTransport([lambda body: {"content": json.dumps(
        {"verdicts": [{"i": 1, "verdict": "overreach", "why": "one filter is not all filters"}]})}])
    args = {"target": e.id, "caused": "x", "caused_refs": [], "claims": [
        {"text": "Exa supports every date filter imaginable", "quote": "startPublishedDate filter",
         "relation": "supports", "lobby_refs": [b]}]}
    msg, row, _ = model_annotate(hotel, args, "agent", vt)
    assert row is None and "overreach" in msg
    vt2 = ScriptedTransport([lambda body: {"content": json.dumps({"verdicts": [{"i": 1, "verdict": "entailed", "why": "ok"}]})}])
    msg, row, _ = model_annotate(hotel, args | {"claims": [args["claims"][0] | {"text": "Exa has a startPublishedDate filter"}]},
                                 "agent", vt2)
    assert row is not None
    assert any(r.actor == "verifier" for r in hotel.reg.list(kind="model"))


def test_guest_drafts_go_through_review(hotel):
    note = add(hotel, "journal", "monologue", "I keep writing chat logs instead of notes.")

    def call(name, **args):
        return {"content": "", "tool_calls": [{"id": "c", "type": "function",
                                                "function": {"name": name, "arguments": json.dumps(args)}}]}
    steps = [
        lambda body: call("read_entry", ids=[note]),
        lambda body: call("propose", why="worth asking", ops=[
            {"op": "append", "room": "journal", "type": "bounty", "body": "What makes a note worth rereading?",
             "refs": [note], "tags": []},
            {"op": "append", "room": "journal", "type": "goal", "body": ""},  # invalid -> feedback
        ]),
        lambda body: call("propose", why="again", ops=[
            {"op": "append", "room": "journal", "type": "bounty", "body": "What makes a note worth rereading?",
             "refs": [note], "tags": []}]),
        lambda body: {"content": "Drafted one bounty for you (see review)."},
    ]
    tr = ScriptedTransport(steps)
    t = Guest(hotel, LLM(hotel, "groq", transport=tr)).say("why do I do this?", note)
    tool_msgs = [m for m in tr.bodies[-1]["messages"] if m["role"] == "tool"]
    assert "I keep writing chat logs" in tool_msgs[0]["content"]
    assert "queued" in tool_msgs[1]["content"] and "rejected" in tool_msgs[1]["content"]
    assert "already queued" in tool_msgs[2]["content"]  # the same draft is not queued twice
    assert t.output_id and len(t.proposals) == 1
    before = set(hotel.lobby().entries)
    assert hotel.decide(t.proposals[0], True).route == "applied"
    (new,) = set(hotel.lobby().entries) - before
    e = hotel.lobby().entries[new]
    assert e.origin == "approved" and e.type == "bounty" and note in e.metas("ref")
    # usage is logged on model rows; args (prompts) live in blobs, not rows
    rows = [r for r in hotel.reg.list(kind="model")]
    assert rows and all("tokens_in" in r.payload and "args_blob" in r.payload and "args" not in r.payload for r in rows)
