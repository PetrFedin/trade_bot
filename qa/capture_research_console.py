"""Offline Chrome CDP viewport captures of ASTRA's generated research console.

Screenshots are evidence of the current static research UI, not of a live
connected trading runtime. No orders, accounts, venue API calls or login.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shutil
import signal
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from websockets.asyncio.client import connect

VIEWPORTS = ((375, 812), (768, 1024), (1440, 900))
PAGE_MODES = ("overview", "capabilities")


class VisualQualificationError(ValueError):
    """Missing browser, mismatched viewport or unready research page."""


def _chrome() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found is not None:
            return found
    raise VisualQualificationError("headless Chromium/Chrome not available on runner")


async def _capture(
    chrome_socket: str, source: Path, output: Path, chrome_version: str,
) -> dict:
    counter = 0
    results = {}
    async with connect(chrome_socket, max_size=12 * 1024 * 1024) as socket:

        async def request(method: str, params=None, session: str | None = None) -> dict:
            nonlocal counter
            counter += 1
            command = {"id": counter, "method": method, "params": params or {}}
            if session:
                command["sessionId"] = session
            await socket.send(json.dumps(command))
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
                if message.get("id") != counter:
                    continue
                if "error" in message:
                    raise VisualQualificationError(
                        f"Chrome DevTools refused {method}: {message['error'].get('message')}"
                    )
                return message.get("result", {})

        created = await request("Target.createTarget", {"url": "about:blank"})
        attached = await request(
            "Target.attachToTarget",
            {"targetId": created["targetId"], "flatten": True},
        )
        session = attached["sessionId"]
        await request("Page.enable", session=session)
        await request("Runtime.enable", session=session)
        for width, height in VIEWPORTS:
            await request(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width, "height": height,
                    "deviceScaleFactor": 1, "mobile": width <= 768,
                },
                session,
            )
            await request("Page.navigate", {"url": source.resolve().as_uri()}, session)
            state = None
            for _ in range(35):
                evaluation = await request(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "({ready:document.readyState,"
                            "featureRows:document.querySelectorAll("
                            "'#capability-table tbody tr').length,"
                            "viewportWidth:window.innerWidth,"
                            "documentWidth:document.documentElement.scrollWidth,"
                            "scenarioExists:!!document.querySelector('#scenario-form')})"
                        ),
                        "returnByValue": True,
                    },
                    session,
                )
                state = evaluation.get("result", {}).get("value", {})
                if (
                    state.get("ready") == "complete"
                    and state.get("featureRows") == 12
                    and state.get("scenarioExists") is True
                ):
                    break
                await asyncio.sleep(0.15)
            if (
                state is None
                or state.get("featureRows") != 12
                or state.get("viewportWidth") != width
                or state.get("documentWidth", width + 1) > width
                or state.get("scenarioExists") is not True
            ):
                raise VisualQualificationError(
                    f"unready or mismatched {width}px document: {state}"
                )
            for mode in PAGE_MODES:
                await request(
                    "Runtime.evaluate",
                    {"expression": (
                        "window.scrollTo(0,0)"
                        if mode == "overview"
                        else "document.getElementById('capabilities').scrollIntoView()"
                    )},
                    session,
                )
                await asyncio.sleep(0.17)
                shot = await request(
                    "Page.captureScreenshot",
                    {
                        "format": "png",
                        "fromSurface": True,
                        "captureBeyondViewport": False,
                    },
                    session,
                )
                raw = base64.b64decode(shot["data"], validate=True)
                if raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
                    raise VisualQualificationError("invalid screenshot evidence format")
                size = struct.unpack(">II", raw[16:24])
                if size != (width, height):
                    raise VisualQualificationError(
                        f"viewport screenshot mismatch for {width}: {size}"
                    )
                name = f"astra-{width}-{mode}.png"
                (output / name).write_bytes(raw)
                results[name] = {
                    "viewport_px": [width, height],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "mode": mode,
                    "capability_rows": 12,
                }
        await request("Target.closeTarget", {"targetId": created["targetId"]})
    return {
        "schema_version": "astra-research-console-visual-evidence-v1",
        "status": "OFFLINE_UI_CAPTURE_NOT_LIVE_VENUE_QUALIFICATION",
        "browser": chrome_version,
        "source_html_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "screenshots": dict(sorted(results.items())),
        "trading_authority_allowed": False,
        "profitability_proven": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.html.is_file() or args.html.is_symlink():
        raise VisualQualificationError("generated offline HTML not found")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    executable = _chrome()
    version = subprocess.check_output([executable, "--version"], text=True).strip()
    with tempfile.TemporaryDirectory(
        prefix="astra-console-chrome-", ignore_cleanup_errors=True,
    ) as profile:
        proc = subprocess.Popen(
            [
                executable, "--headless=new", "--no-sandbox",
                "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-extensions",
                "--disable-dev-shm-usage", "--hide-scrollbars",
                "--remote-allow-origins=*",
                "--remote-debugging-port=0",
                "--user-data-dir=" + profile,
                "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            port_file = Path(profile) / "DevToolsActivePort"
            start = time.monotonic()
            while not port_file.exists():
                if proc.poll() is not None or time.monotonic() - start > 20:
                    raise VisualQualificationError("headless Chrome did not start")
                time.sleep(0.1)
            lines = port_file.read_text().splitlines()
            if len(lines) < 2:
                raise VisualQualificationError("invalid Chrome DevTools endpoint")
            socket_url = f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"
            report = asyncio.run(
                _capture(socket_url, args.html, args.output_dir, version),
            )
        finally:
            # Chrome starts multiple child processes sharing its private
            # session. Stopping only the parent races profile cleanup.
            # The ephemeral runner contains no user/browser credentials.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                if sig == signal.SIGTERM:
                    try:
                        proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        pass
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=6)
    (args.output_dir / "visual-evidence.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    print("ASTRA_RESEARCH_VISUAL_CAPTURE", len(report["screenshots"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
