"""Headless TUI snapshots — make the UI feedback-loopable.

Runs the real app with Textual's pilot, types scripted commands, and returns
the visible state as TEXT (status bar, message line, lobby, active side tab).
Agents (and tests) read text far faster than screenshots; pass --svg when a
human or vision check of the actual pixels/colours is needed.

    hotel snapshot --cmd "/room harness" --cmd "/bounty why?" --svg shot.svg
"""

from __future__ import annotations

import asyncio


def _plain(widget) -> str:
    r = widget.render()
    return getattr(r, "plain", str(r))


def snapshot(hotel, commands: list[str], keys: list[str] | None = None, svg: str | None = None,
             size: str = "190x46", wait: float = 0.5) -> str:
    from textual.widgets import TabbedContent

    from .tui import HotelApp

    w, h = (int(x) for x in size.lower().split("x"))

    async def go() -> str:
        app = HotelApp(hotel)
        async with app.run_test(size=(w, h)) as pilot:
            await pilot.pause()
            for c in commands:
                app.query_one("#cmd").value = c
                await pilot.press("enter")
                await pilot.pause(wait)
                # thread workers: poll until every worker has finished
                while any(not w.is_finished for w in app.workers):
                    await pilot.pause(0.2)
                await pilot.pause()
            for k in keys or []:
                await pilot.press(k)
                await pilot.pause(wait)
            if svg:
                app.save_screenshot(svg)
            out = [f"screen: {type(app.screen).__name__}"]
            if type(app.screen).__name__ != "Screen":
                out.append("modal fields: " + "; ".join(
                    f"{i.id}={i.value!r}" for i in app.screen.query("Input")))
                errs = [_plain(s) for s in app.screen.query("#a-err")]
                if errs and errs[0].strip():
                    out.append("modal error: " + errs[0])
                return "\n".join(out)
            tabs = app.query_one(TabbedContent)
            out += [
                "status: " + _plain(app.query_one("#status")),
                "message: " + _plain(app.query_one("#msg")),
                "--- lobby ---", _plain(app.query_one("#lobby")).rstrip(),
                f"--- side tab: {tabs.active} ---",
            ]
            pane = {"t-res": "#results", "t-rev": "#review", "t-blob": "#blob", "t-guest": "#guest"}.get(tabs.active)
            if pane:
                out.append(_plain(app.query_one(pane)).rstrip()[:4000])
            else:
                t = app.query_one("#reg-table")
                for row in range(min(t.row_count, 15)):
                    out.append("  ".join(str(getattr(c, "plain", c)) for c in t.get_row_at(row)))
            if svg:
                out.append(f"svg: {svg}")
            return "\n".join(out)

    return asyncio.run(go())
