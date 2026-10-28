"""Agent loop tests with a scripted model (no network). Each step is what a
model would plausibly emit; the harness responses are real."""

import json
import re

import pytest

from hotel.agent import ResearchAgent, normalize_citations
from hotel.config import Paths
from hotel.core import Hotel, init_home
from hotel.gateway import GateBlocked, ToolResult, ToolSpec
from hotel.librarian import ModelLibrarian, run_librarian
from hotel.llm import LLM, ScriptedTransport
from hotel.models import AppendOp
from hotel.search import Hit, make_tools

DOC = ("QUERY: q\n\n### [1] fake:1 (external) Exa docs\nurl: https://exa.ai/docs\n\n"
       "- Exa search accepts startPublishedDate and endPublishedDate filters in ISO 8601.\n")


def fake_agent_search(queries, objective, skip_urls=None) -> ToolResult:  # offline test double
    return ToolResult(b'{"fake": true}', DOC, [], "fake")


def test_deep_research_is_gated_and_budgeted(hotel):
    seen = []

    def fake_deep(query, depth="deep", angles=None) -> ToolResult:
        seen.append((query, depth, angles))
        return ToolResult(b'{"fake": true}', DOC, [], "fake")

    hotel.gateway.register(ToolSpec("exa.deep", "retrieval", "fake", fake_deep))
    b = hotel.apply([AppendOp(op="append", room="harness", type="bounty",
                              body="Compare Exa date filters across endpoints", refs=[], tags=[])]).created[0]
    steps = [
        lambda body: call("deep_research", query="exa date filters", depth="agent", angles=["a", "b", "c", "d"]),
        lambda body: call("annotate", target=last_rid(body), caused="answers bounty", caused_refs=[b],
                          claims=[{"text": "date filters", "quote": "accepts startPublishedDate and endPublishedDate",
                                   "relation": "supports", "lobby_refs": [b]}]),
        lambda body: call("deep_research", query="again", depth="deep"),  # over budget (max_deep=1)
        lambda body: {"content": f"Yes [R:{last_rid(body)}]."},
    ]
    tr = ScriptedTransport(steps)
    run = ResearchAgent(hotel, LLM(hotel, "groq", transport=tr)).run(b)
    tool_msgs = [m["content"] for m in tr.bodies[-1]["messages"] if m["role"] == "tool"]
    assert seen == [("exa date filters", "agent", ["a", "b", "c"])]  # angles capped at 3, second call never ran
    assert "deep_research budget exhausted" in tool_msgs[2]
    assert run.output_id and len(run.retrievals) == 1


@pytest.fixture
def hotel(tmp_path):
    init_home(tmp_path)
    h = Hotel(Paths(tmp_path))
    h.gateway.register(ToolSpec("agent.search", "retrieval", "fake", fake_agent_search))
    return h


def call(name, **args):
    return {"content": "", "tool_calls": [{"id": f"c-{name}", "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def last_rid(body):
    rids = re.findall(r"RETRIEVAL R:(\d+)", json.dumps(body["messages"]))
    return int(rids[-1])


def test_agent_loop_gate_feedback_and_accept(hotel):
    b = hotel.apply([AppendOp(op="append", room="harness", type="bounty",
                              body="Does Exa support a published-date filter?", refs=[], tags=[])]).created[0]
    steps = [
        lambda body: call("search", queries=["exa search api published date filter"]),
        lambda body: call("search", queries=["completely different query about exa dates"]),  # gate
        lambda body: {"content": "Yes it does [R:1]."},  # premature answer -> output gate
        lambda body: call("annotate", target=last_rid(body), caused="answers bounty", caused_refs=[b],
                          claims=[{"text": "date filter", "quote": "this is not in the doc",
                                   "relation": "supports", "lobby_refs": [b]}]),
        lambda body: call("annotate", target=last_rid(body), caused="answers bounty", caused_refs=[b],
                          claims=[{"text": "Exa has start/end published-date filters",
                                   "quote": "accepts startPublishedDate and endPublishedDate   filters",
                                   "relation": "supports", "lobby_refs": [b]}]),
        lambda body: {"content": f"Yes: Exa search accepts startPublishedDate/endPublishedDate 【{last_rid(body)}】."},
    ]
    tr = ScriptedTransport(steps)
    agent = ResearchAgent(hotel, LLM(hotel, "groq", transport=tr), budget_chars=10_000)
    run = agent.run(b)

    tool_msgs = [m for m in tr.bodies[-1]["messages"] if m["role"] == "tool"]
    assert "annotation gate" in tool_msgs[1]["content"]
    assert "not contiguous" in tool_msgs[2]["content"]
    assert tool_msgs[3]["content"].startswith("OK: annotation")
    assert any("Annotate before answering" in (m.get("content") or "") for m in tr.bodies[-1]["messages"])
    rid = run.retrievals[0]
    assert run.output_id and run.answer.endswith(f"[R:{rid}].")  # citation normalized
    assert DOC.split("\n\n", 1)[1] in tool_msgs[0]["content"]  # model saw the stored bytes

    # model-written annotation: librarian may ref, but the close goes to review
    _, results = run_librarian(hotel, rid)
    routes = {r.op.op: r.route for r in results}
    assert routes == {"ref": "applied", "close": "queued"}

    for _ in range(2001):  # the run's row is no longer among the latest internal rows; accept must still find it
        hotel.gateway.log("internal", "user", tool="lobby.search")
    res = hotel.accept_output(run.output_id)
    e = hotel.lobby().entries[res.created[0]]
    assert e.origin == "approved" and e.type == "reasoning" and f"R:{rid}" in e.metas("ref")
    assert res.created[0] in hotel.lobby().entries[b].metas("link")
    assert hotel.check() == [] and hotel.reg.verify_chain() == []
    # large model prompts are stored as blobs, not inlined into Reg rows
    model_rows = hotel.reg.list(kind="model")
    assert model_rows and all("args_blob" in r.payload and "ms" in r.payload for r in model_rows)


def test_agent_refuses_with_pending_user_retrieval(hotel):
    hotel.gateway.call("agent.search", actor="user", queries=["x y z w"], objective="o")
    with pytest.raises(GateBlocked):
        ResearchAgent(hotel, LLM(hotel, "groq", transport=ScriptedTransport([]))).run("L0001")


def test_compaction_replaces_annotated_retrievals(hotel):
    b = hotel.apply([AppendOp(op="append", room="r", type="bounty", body="q?", refs=[], tags=[])]).created[0]
    steps = [
        lambda body: call("search", queries=["exa search api published date filter"]),
        lambda body: call("annotate", target=last_rid(body), caused="nothing: test", caused_refs=[], claims=[]),
        lambda body: {"content": "unknown"},
    ]
    tr = ScriptedTransport(steps)
    ResearchAgent(hotel, LLM(hotel, "groq", transport=tr), budget_chars=300).run(b)
    assert "full text elided" in json.dumps(tr.bodies[-1]["messages"])


def test_model_librarian_repairs_invalid_json(hotel, tmp_path):
    b = hotel.apply([AppendOp(op="append", room="r", type="bounty", body="q?", refs=[], tags=[])]).created[0]
    e, _ = hotel.gateway.call("agent.search", actor="user", queries=["a b c d"], objective="o")
    from hotel.gateway import make_pointer
    from hotel.models import Annotation, Claim
    text = hotel.blobs.text(e.text_hash)
    hotel.gateway.annotate(Annotation(target=e.id, caused="answers", caused_refs=[b], claims=[
        Claim(text="t", relation="supports", lobby_refs=[b], pointer=make_pointer(text, e.text_hash, "startPublishedDate"))]))
    tr = ScriptedTransport([
        lambda body: {"content": '{"ops": [{"op": "ref", "id": "L0001"}]}'},  # invalid: missing fields
        lambda body: {"content": json.dumps({"rationale": "cite", "ops": [
            {"op": "ref", "id": b, "ref": f"R:{e.id}"}, {"op": "close", "id": b, "closed_by": f"R:{e.id}"}]})},
    ])
    out, results = run_librarian(hotel, e.id, ModelLibrarian(hotel, "groq", transport=tr))
    assert [r.route for r in results] == ["applied", "applied"]  # user annotation -> deterministic close
    assert "failed validation" in json.dumps(tr.bodies[1]["messages"])


def test_normalize_citations():
    assert normalize_citations("a 【12】 b [ 3 ] c [R:4]") == "a [R:12] b [R:3] c [R:4]"


def test_find_quote_tolerates_markdown_but_not_paraphrase():
    from hotel.gateway import find_quote
    t = "| **3B MLX Q8** | **recommended default** | **74.56** |\n"
    s = find_quote(t, "3B MLX Q8 | recommended default | 74.56")
    assert s and t[s[0]:s[1]].startswith("**3B") and "74.56" in t[s[0]:s[1]]
    assert find_quote(t, "3B MLX Q8 is recommended") is None


def test_web_search_fuses_exa_and_parallel(hotel):
    """Both providers run; one page under two URL spellings is one hit, and agreement ranks it first."""
    def hit(src, url, text):
        return Hit(src, url, url, url, text, "t", "external")

    class FakeExa:
        max_chars = 100

        def search(self, q, n, **kw):
            return b"{}", [hit("exa", "https://only-exa.com/x", "exa only"), hit("exa", "https://www.ex.com/a/", "short")]

    class FakeParallel:
        fail = False

        def ready(self):
            return True

        def search_many(self, queries, objective, n, max_chars=None):
            if self.fail:
                raise RuntimeError("429")
            return b"{}", [hit("parallel", "https://only-par.com/y", "par only"),
                           hit("parallel", "https://ex.com/a.md", "a longer excerpt")]

    par = FakeParallel()
    tools = make_tools(FakeExa(), hotel.rag, hotel.derived, parallel=par, backend="both")
    res = tools["agent.search"](["exa date filter api"], "o")
    assert [h.url for h in res.data] == ["https://ex.com/a.md", "https://only-exa.com/x", "https://only-par.com/y"]
    assert res.data[0].text == "a longer excerpt" and res.provider == "exa+parallel+rag"

    par.fail = True  # Parallel down: Exa still answers, and the retrieval says so
    res = tools["agent.search"](["exa date filter api"], "o")
    assert [h.source for h in res.data] == ["exa", "exa"] and "parallel failed" in res.text
