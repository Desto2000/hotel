"""Headless TUI regression tests (Textual pilot)."""

import asyncio
import time

from hotel.config import Paths
from hotel.core import Hotel, init_home
from hotel.guest import Guest
from hotel.llm import LLM, ScriptedTransport
from hotel.tui import HotelApp


def test_two_fast_guest_messages_both_answered(tmp_path):
    init_home(tmp_path)
    h = Hotel(Paths(tmp_path))
    def slow(reply):
        def step(body):
            time.sleep(0.5)  # turns must overlap, as with a real model
            return {"content": reply}
        return step

    tr = ScriptedTransport([slow("first reply"), slow("second reply")])

    async def go():
        app = HotelApp(h)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.guest = Guest(h, LLM(h, "groq", transport=tr))
            app.guest_mode = True
            for line in ["thought one", "> quick follow-up, not a note"]:  # no wait between them
                app.query_one("#cmd").value = line
                await pilot.press("enter")
                await pilot.pause(0.2)  # second message arrives while the first turn is mid-flight
            for _ in range(100):
                await pilot.pause(0.1)
                if all(w.is_finished for w in app.workers) and len(h.reg.list(kind="output")) == 2:
                    break
            return app.guest_log.plain

    log = asyncio.run(go())
    assert "first reply" in log and "second reply" in log
    bodies = [e.text for e in h.lobby().entries.values()]
    assert bodies == ["thought one"]  # the '>' line was talk, not a note
    assert len(h.reg.list(kind="output")) == 2
