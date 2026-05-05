#!/usr/bin/env python3
"""Phase 3 Scout: lightweight local CDP stealth scraper.

Implements donor-inspired flow:
1) Launch local browser with remote debugging flags.
2) Connect over CDP websocket endpoint.
3) Scrape text content.
4) Persist text to LanceDB.
5) Immediately terminate browser processes to free memory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyppeteer import connect, launch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_core import GatorMemoryCore


@dataclass
class ScoutResult:
    url: str
    chars_scraped: int
    memory_id: str
    title: str
    browser_pid: int | None
    elapsed_sec: float


class ScoutError(RuntimeError):
    pass


def _trim_text(value: str, max_chars: int) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + " ...[truncated]"


def _browser_executable() -> str | None:
    # Allow pyppeteer to auto-manage Chromium if no system browser exists.
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


async def _scrape(url: str, timeout_sec: int = 35) -> tuple[str, str, int | None]:
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-features=Translate,BackForwardCache",
        "--window-size=1365,768",
    ]

    browser = None
    attached = None
    proc_pid = None
    try:
        kwargs: dict[str, Any] = {
            "headless": True,
            "args": launch_args,
            "handleSIGINT": False,
            "handleSIGTERM": False,
            "handleSIGHUP": False,
        }
        exe = _browser_executable()
        if exe:
            kwargs["executablePath"] = exe

        browser = await launch(kwargs)
        proc = getattr(browser, "process", None)
        proc_pid = int(proc.pid) if proc and getattr(proc, "pid", None) else None

        # Camofox donor pattern: connect to the existing CDP endpoint.
        ws_endpoint = browser.wsEndpoint
        attached = await connect(browserWSEndpoint=ws_endpoint)

        page = await attached.newPage()
        await page.setUserAgent(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await page.evaluateOnNewDocument(
            """
            () => {
              Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
              Object.defineProperty(navigator, 'language', {get: () => 'en-US'});
              Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            }
            """
        )

        await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": timeout_sec * 1000})
        title = await page.title()
        text = await page.evaluate(
            """
            () => {
              const bodyText = document && document.body ? document.body.innerText : '';
                            return (bodyText || '').replace(/\\s+/g, ' ').trim();
            }
            """
        )
        return str(title or ""), str(text or ""), proc_pid
    finally:
        # Kill attached context first, then launcher process to free memory immediately.
        try:
            if attached:
                await attached.close()
        except Exception:
            pass


async def _scrape_snapshot(url: str, mode: str = "markdown", timeout_sec: int = 35) -> tuple[str, str, int | None]:
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-features=Translate,BackForwardCache",
        "--window-size=1365,768",
    ]

    browser = None
    attached = None
    proc_pid = None
    try:
        kwargs: dict[str, Any] = {
            "headless": True,
            "args": launch_args,
            "handleSIGINT": False,
            "handleSIGTERM": False,
            "handleSIGHUP": False,
        }
        exe = _browser_executable()
        if exe:
            kwargs["executablePath"] = exe

        browser = await launch(kwargs)
        proc = getattr(browser, "process", None)
        proc_pid = int(proc.pid) if proc and getattr(proc, "pid", None) else None
        ws_endpoint = browser.wsEndpoint
        attached = await connect(browserWSEndpoint=ws_endpoint)

        page = await attached.newPage()
        await page.setUserAgent(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await page.evaluateOnNewDocument(
            """
            () => {
              Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
              Object.defineProperty(navigator, 'language', {get: () => 'en-US'});
              Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            }
            """
        )

        await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": timeout_sec * 1000})
        title = await page.title()

        if mode == "a11y":
            tree = await page.evaluate(
                """
                () => {
                  const out = [];
                  const q = document.querySelectorAll('a,button,input,textarea,select,[role],h1,h2,h3,h4,h5,h6,p,li');
                  for (const el of Array.from(q).slice(0, 220)) {
                    const role = el.getAttribute('role') || el.tagName.toLowerCase();
                    const name = (el.getAttribute('aria-label') || el.innerText || el.textContent || '').trim();
                    const href = el.getAttribute('href') || '';
                    if (!name && !href) continue;
                    out.push({role, name: name.slice(0,180), href});
                  }
                  return out;
                }
                """
            )
            snapshot = json.dumps(tree, ensure_ascii=True, indent=2)
        else:
            snapshot = await page.evaluate(
                """
                () => {
                  const lines = [];
                  const title = (document.title || '').trim();
                  if (title) lines.push(`# ${title}`);
                  const nodes = document.querySelectorAll('h1,h2,h3,p,li');
                  for (const n of Array.from(nodes).slice(0, 260)) {
                                        const t = (n.innerText || n.textContent || '').replace(/\\s+/g,' ').trim();
                    if (!t) continue;
                    if (n.tagName === 'H1') lines.push(`\n# ${t}`);
                    else if (n.tagName === 'H2') lines.push(`\n## ${t}`);
                    else if (n.tagName === 'H3') lines.push(`\n### ${t}`);
                    else if (n.tagName === 'LI') lines.push(`- ${t}`);
                    else lines.push(t);
                  }
                                    return lines.join(String.fromCharCode(10));
                }
                """
            )

        return str(title or ""), str(snapshot or ""), proc_pid
    finally:
        try:
            if attached:
                await attached.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            if browser and getattr(browser, "process", None):
                p = browser.process
                if p and p.poll() is None:
                    p.terminate()
        except Exception:
            pass


def camoufox_snapshot(url: str, mode: str = "markdown", max_chars: int = 7000) -> dict[str, Any]:
    if mode not in {"markdown", "a11y"}:
        raise ScoutError("mode must be markdown or a11y")
    started = time.time()
    title, snap, _ = asyncio.run(_scrape_snapshot(url=url, mode=mode))
    cleaned = _trim_text(snap, max_chars=max_chars)
    return {
        "url": url,
        "mode": mode,
        "title": title,
        "snapshot": cleaned,
        "chars": len(cleaned),
        "elapsed_sec": round(time.time() - started, 3),
    }


def scout_url(url: str, server: str = "http://127.0.0.1:8081") -> ScoutResult:
    started = time.time()
    title, text, browser_pid = asyncio.run(_scrape(url))
    if not text:
        raise ScoutError("No text content was scraped")

    core = GatorMemoryCore(server_url=server)
    payload = f"[SCOUT_CAPTURE] url={url}\ntitle={title}\n\n{text[:16000]}"
    ingested = core.ingest_document(payload)

    elapsed = round(time.time() - started, 3)
    return ScoutResult(
        url=url,
        chars_scraped=len(text),
        memory_id=ingested.id,
        title=title,
        browser_pid=browser_pid,
        elapsed_sec=elapsed,
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator Scout CDP scraper")
    parser.add_argument("--url", required=True, help="Target URL")
    parser.add_argument("--server", default="http://127.0.0.1:8081", help="llama-server URL")
    args = parser.parse_args()

    out = scout_url(args.url, server=args.server)
    print(json.dumps(out.__dict__, indent=2))


if __name__ == "__main__":
    try:
        _main()
    except Exception as exc:
        raise SystemExit(f"[ERROR] {exc}")
