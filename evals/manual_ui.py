#!/usr/bin/env python
"""MANUAL check (not part of the automated suite). Deny the approval in a real
browser, then prove no bill was written.

This is the single most important safety property in the project: a refusal from
the human has to actually stop the write, not just stop the report.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.config import get_settings  # noqa: E402

URL = "http://127.0.0.1:8000/"


def bills_for(invoice: str) -> list[tuple]:
    settings = get_settings()
    with sqlite3.connect(f"file:{settings.ap_db_path}?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT bill_id, invoice_number, amount FROM bills WHERE invoice_number = ?",
            (invoice,)).fetchall()
    return rows


async def main() -> int:
    before = bills_for("HL-2291")
    print(f"AP bills for HL-2291 before: {before}")

    problems: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

        await page.goto(URL, wait_until="networkidle")
        await page.fill("#goal", "Enter the latest open invoice from Hooli Cloud Services into AP.")
        await page.click("#start")

        await page.wait_for_selector("#scrim:not([hidden])", timeout=120_000)
        print(f"approval: {await page.inner_text('#q-title')}")
        await page.click("#q-deny")
        print("clicked: Deny")
        await page.wait_for_timeout(1500)
        assert await page.locator("#scrim").get_attribute("hidden") is not None

        # Let the run unwind on its own.
        for _ in range(240):
            await page.wait_for_timeout(500)
            if await page.locator("#report-panel:not([hidden])").count():
                break
        verdict = await page.inner_text("#report .verdict__status")
        print(f"verdict : {verdict}")
        await page.screenshot(path="/tmp/aiw-smoke/ui-denied.png", full_page=True)
        await browser.close()

    after = bills_for("HL-2291")
    print(f"AP bills for HL-2291 after : {after}")

    ok = after == before and not problems
    print("\nwrite prevented:", "PASS" if ok else "FAIL")
    if after != before:
        print("  a denied approval still wrote a bill")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))