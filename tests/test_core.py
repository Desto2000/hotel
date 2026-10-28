import sqlite3

import pytest

from hotel.config import Paths
from hotel.core import Hotel, ValidationFailed, init_home
from hotel.gateway import AnnotationRejected, GateBlocked, ToolResult, ToolSpec, make_pointer
from hotel.librarian import run_librarian
from hotel.models import Annotation, AppendOp, Claim, CloseOp, SupersedeOp
from hotel.ops import OpError
from hotel.replay import replay
from hotel.validator import validate_transition

FAKE_DOC = "QUERY: q\n\n### [1] fake:1 (external) Doc\n\nExa supports a startPublishedDate filter on search.\n"


def fake_search(query: str) -> ToolResult:  # offline test double, not a real provider
    return ToolResult(b'{"fake": true}', FAKE_DOC.replace("q", query, 1), [], "fake")


@pytest.fixture
def hotel(tmp_path):
    init_home(tmp_path)
    h = Hotel(Paths(tmp_path))
    h.gateway.register(ToolSpec("fake.search", "retrieval", "fake", fake_search))
    return h


def add(h, room, typ, body):
    return h.apply([AppendOp(op="append", room=room, type=typ, body=body, refs=[], tags=[])]).created[0]


def annotate_supporting(h, rid, eid, caused_refs=None):
    e = h.reg.get(rid)
    text = h.blobs.text(e.text_hash)
    ptr = make_pointer(text, e.text_hash, "supports a startPublishedDate   filter")
    ann = Annotation(target=rid, claims=[Claim(text="Exa has a date filter", pointer=ptr,
                                                relation="supports", lobby_refs=[eid])],
                     caused="answers bounty", caused_refs=caused_refs or [eid])
    return h.gateway.annotate(ann)


def test_reg_is_append_only_and_chained(hotel):
    hotel.gateway.log("internal", "user", payload={"x": 1})
    with pytest.raises(sqlite3.IntegrityError):
        hotel.reg.conn.execute("UPDATE reg SET actor='evil'")
    with pytest.raises(sqlite3.IntegrityError):
        hotel.reg.conn.execute("DELETE FROM reg")
    assert hotel.reg.verify_chain() == []


def test_reg_appends_from_threads_share_one_connection(hotel):
    """The TUI's worker threads and its main thread append through the same Reg."""
    import threading
    errors = []

    def writer():
        try:
            for _ in range(100):
                hotel.reg.append(hotel.session_id, "internal", "t")
        except Exception as ex:
            errors.append(ex)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(hotel.reg.list(session_id=hotel.session_id, kind="internal", limit=1000)) == 400
    assert hotel.reg.verify_chain() == []


def test_identical_blobs_written_concurrently(tmp_path):
    """Two writers storing the same content (two processes opening one lobby store the same
    snapshot) must both succeed: the store is content-addressed, so either copy is the blob."""
    import hashlib
    import threading
    from hotel.blobs import BlobStore
    errors = []
    for trial in range(10):
        store, data = BlobStore(tmp_path / f"b{trial}"), b"x" * 500_000 + bytes([trial])

        def writer():
            try:
                store.put(data)
            except Exception as ex:
                errors.append(ex)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert store.get(hashlib.sha256(data).hexdigest()) == data
    assert errors == []
    assert not list(tmp_path.rglob("*.tmp"))


def test_event_log_mirrors_every_reg_row_without_content(hotel):
    """The timeline gets every row, but queries, results and lobby text stay in forgettable blobs."""
    import json
    add(hotel, "inbox", "bounty", "Body words zebrafinch?")
    hotel.gateway.call("fake.search", query="query words quokka")
    with pytest.raises(GateBlocked):
        hotel.gateway.call("fake.search", query="second query axolotl")
    raw = hotel.paths.log.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in raw.splitlines() if '"event": "reg"' in line]
    assert [(r["id"], r["kind"]) for r in rows] == [(e.id, e.kind) for e in hotel.reg.list()]
    assert rows[-1]["pending"] == [rows[-2]["id"]]
    for word in ("zebrafinch", "quokka", "axolotl", "startPublishedDate"):
        assert word not in raw


def test_gate_blocks_until_annotated(hotel, tmp_path):
    b = add(hotel, "harness", "bounty", "Does Exa support a date filter?")
    e1, _ = hotel.gateway.call("fake.search", query="exa date")
    with pytest.raises(GateBlocked):
        hotel.gateway.call("fake.search", query="again")
    # a fresh session does not escape the gate
    h2 = Hotel(Paths(tmp_path), new_session=True)
    h2.gateway.register(ToolSpec("fake.search", "retrieval", "fake", fake_search))
    with pytest.raises(GateBlocked):
        h2.gateway.call("fake.search", query="again")
    assert any(r.kind == "blocked" for r in hotel.reg.list())
    # internal lobby search is logged but not gated
    hotel.gateway.call("lobby.search", query="exa")
    annotate_supporting(hotel, e1.id, b)
    hotel.gateway.call("fake.search", query="now ok")


def test_annotation_pointer_must_match_blob(hotel):
    e, _ = hotel.gateway.call("fake.search", query="x")
    bad = Annotation(target=e.id, claims=[Claim(
        text="t", relation="new",
        pointer={"blob": e.text_hash, "start": 0, "end": 5, "quote": "WRONG"})], caused="c")
    with pytest.raises(AnnotationRejected):
        hotel.gateway.annotate(bad)
    with pytest.raises(AnnotationRejected):  # supports without lobby ref
        ptr = make_pointer(hotel.blobs.text(e.text_hash), e.text_hash, "Exa supports")
        hotel.gateway.annotate(Annotation(target=e.id, claims=[Claim(text="t", relation="supports", pointer=ptr)], caused="c"))
    assert hotel.gateway.pending()  # still locked


def test_lobby_append_only_rules(hotel):
    a = add(hotel, "ideas", "reasoning", "Reg first, lobby second.")
    old = hotel.text()
    assert validate_transition(old, old.replace("Reg first", "Lobby first")), "body edits must fail"
    assert validate_transition(old, old.replace(f"@{a}", "@L0099")), "id change must fail"
    assert validate_transition(old, old.replace("[.]", "[x]")), "static can't close"
    res = hotel.apply([SupersedeOp(op="supersede", id=a, type="reasoning", body="Reg first, gate with it.", reason="refined")])
    lob = hotel.lobby()
    assert lob.entries[a].sigil == "~" and lob.entries[res.created[0]].metas("supersedes") == [a]
    b = add(hotel, "ideas", "bounty", "Open question")
    with pytest.raises(OpError):
        hotel.apply([CloseOp(op="close", id=b, closed_by="L0001")])  # bounty needs Reg
    with pytest.raises(ValidationFailed):
        hotel.apply([CloseOp(op="close", id=b, closed_by="R:1")])  # R:1 not an annotated retrieval
    assert hotel.check() == []


def test_resident_policy_and_review(hotel):
    b = add(hotel, "harness", "bounty", "Does Exa support a date filter?")
    g = add(hotel, "harness", "goal", "Use only Exa for phase 1")
    e, _ = hotel.gateway.call("fake.search", query="exa date")
    text = hotel.blobs.text(e.text_hash)
    p1 = make_pointer(text, e.text_hash, "startPublishedDate filter")
    hotel.gateway.annotate(Annotation(target=e.id, claims=[
        Claim(text="date filter exists", pointer=p1, relation="supports", lobby_refs=[b]),
        Claim(text="contradiction demo", pointer=p1, relation="contradicts", lobby_refs=[g]),
    ], caused="answers bounty", caused_refs=[b]))
    out, results = run_librarian(hotel, e.id)
    routes = {(r.op.op, getattr(r.op, "id", "")): r.route for r in results}
    assert routes[("close", b)] == "applied"
    assert routes[("contest", g)] == "queued"
    lob = hotel.lobby()
    assert lob.entries[b].sigil == "x" and lob.entries[b].metas("closed-by") == [f"R:{e.id}"]
    # residents can't author goals
    r = hotel.route_resident_op(AppendOp(op="append", room="harness", type="goal", body="x", refs=[], tags=[]), "librarian", None)
    assert r.route == "rejected"
    (rv,) = hotel.reg.pending_reviews()
    assert hotel.decide(rv.id, True).route == "applied"
    assert hotel.lobby().entries[g].sigil == "!"
    assert hotel.reg.verify_chain() == []


def test_replay_reproduces_session(hotel):
    b = add(hotel, "harness", "bounty", "Does Exa support a date filter?")
    e, _ = hotel.gateway.call("fake.search", query="exa date")
    annotate_supporting(hotel, e.id, b)
    run_librarian(hotel, e.id)
    add(hotel, "harness", "monologue", "after the librarian")
    rep = replay(hotel, hotel.session_id, "rule")
    assert rep.diff() == "", rep.diff() + "\n".join(rep.log)


def test_theme_builds():
    from hotel.theme import lobby_styles, palettes, textual_theme
    pals = palettes()
    assert set(pals) == {"calculus-dark", "calculus-light"}
    t = textual_theme(pals["calculus-dark"])
    assert t.primary.upper() == "#2B83FF" and t.background.upper() == "#000000"
    assert lobby_styles(pals["calculus-light"])["type"].color


def test_claim_refs_and_suggested_links_go_to_review(hotel):
    from hotel.llm import ScriptedTransport
    import json as _json
    b = add(hotel, "harness", "bounty", "Does Exa support a date filter?")
    other = add(hotel, "harness", "idea", "Filter search results by publication date")
    e, _ = hotel.gateway.call("fake.search", query="exa date")
    annotate_supporting(hotel, e.id, b)
    tr = ScriptedTransport([lambda body: {"content": _json.dumps({"links": [
        {"claim": 1, "entry": other, "why": "same filter"},
        {"claim": 1, "entry": "L9999", "why": "invented"},   # not a candidate: dropped
        {"claim": 7, "entry": other, "why": "no such claim"},  # invented claim: dropped
    ]})}])
    _, results = run_librarian(hotel, e.id, related_transport=tr)
    lob = hotel.lobby()
    assert f"R:{e.id}#c1" in lob.entries[b].metas("ref")  # named by the user: applied, as a claim ref
    assert lob.entries[other].metas("ref") == []  # inferred by the model: not applied
    (rv,) = hotel.reg.pending_reviews()
    assert rv.payload["op"] == {"op": "ref", "id": other, "ref": f"R:{e.id}#c1"}
    assert hotel.decide(rv.id, True).route == "applied"
    assert f"R:{e.id}#c1" in hotel.lobby().entries[other].metas("ref")
    assert "Exa has a date filter" in hotel.read_entries([other])  # readers get the claim, not the whole search
    assert hotel.check() == []


def test_reading_view_folds_but_never_changes_content(hotel):
    from hotel import reading
    from hotel.llm import ScriptedTransport
    from hotel.theme import lobby_styles, palettes
    doc_text = ("QUERY: q\n\n### [1] fake:1 (external) Doc\n\nDoc\n\nSign in\n\n"
                "## Results\n\nExa supports a **startPublishedDate** filter.\n\nFollow the steps below.\n")
    hotel.gateway.register(ToolSpec("fake.page", "retrieval", "fake", lambda query: ToolResult(b"{}", doc_text, [], "fake")))
    e, _ = hotel.gateway.call("fake.page", query="q")
    doc = reading.parse(hotel.blobs.text(e.text_hash))
    lines = {doc.line_text(n).strip(): n for n in range(len(doc.lines))}
    assert {lines["Doc"], lines["Sign in"]} <= reading.rule_chrome(doc)
    # the model may only name candidate line numbers; anything else is ignored
    tr = ScriptedTransport([lambda body: {"content": '{"chrome": [%d, 0, 2, 9999]}' % lines["Follow the steps below."]}])
    got = reading.model_chrome(hotel, e.id, "groq", transport=tr)
    assert got == {lines["Follow the steps below."]}
    assert hotel.blobs.text(e.text_hash) == doc_text  # stored text untouched
    chrome, _ = reading.chrome_for(hotel, e.id, doc)
    st = lobby_styles(palettes()["calculus-dark"])
    view = reading.render(doc, st, chrome).plain
    assert "Sign in" not in view and "Follow the steps" not in view
    assert "Exa supports a startPublishedDate filter." in view  # marks hidden, words kept
    assert "Sign in" in reading.render(doc, st, chrome, show_all=True).plain
    # forum usernames stay: they show who is speaking
    forum = reading.parse("QUERY: q\n\n### [1] exa:1 (external) Thread\n\nalice\n\nForgetting is harder.\n\n"
                          "Edit 2022-06-20 20:21\n\nbob\n\nLoving is harder, for me at least.\n")
    kept = {forum.line_text(n) for n in range(len(forum.lines))} - {forum.line_text(n) for n in reading.rule_chrome(forum)}
    assert {"alice", "bob"} <= kept and "Edit 2022-06-20 20:21" not in kept
    # text copied from the view still quotes against the stored blob
    assert make_pointer(doc_text, e.text_hash, "Results Exa supports a startPublishedDate filter.") is not None
