#!/usr/bin/env python3
r"""Browser-only screen recorder for Flow demos (0 credits) — OBS-free.

Long-term, reusable recording harness that captures ONLY the browser page via
Playwright's native ``record_video_dir`` (CDP screencast). Unlike OBS window
capture it does NOT depend on window titles, desktop state, monitor layout, or
the GPU compositor, and it never records anything outside the browser tab — so
it is safe to run on a machine with private windows open.

It is deliberately *dev/test-scoped*: it lives in ``scripts/dev/`` and is NOT
imported by the ``gflow_cli`` package. It reuses gflow's profile resolution,
browser channel, and route builders, but launches its OWN Playwright context
(the core transport has no video-recording hook, by design).

What it records: the real Flow character editor for an EXISTING character entity
(navigation + a gentle scripted pan), so it costs zero credits. Point it at any
already-generated character.

Usage (PowerShell):
    $env:GFLOW_CLI_PROFILE='promo-denon82'
    .venv\Scripts\python.exe scripts\dev\record_flow_capture.py `
        --project f5d0d08b-0617-40ea-a5b3-1d716c60d07f `
        --entity  00743eac-c975-4ea4-a3a4-bfe419087d5f `
        --seconds 22 --out scripts\dev\_spike_out\flow-capture.mp4

Output: an .mp4 (H.264) written to --out (default: scripts/dev/_spike_out/).
Requires ffmpeg on PATH for the webm->mp4 transcode.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _spike_common import default_out_path, resolve_profile_dir, step  # noqa: E402

from gflow_cli.api import routes  # noqa: E402
from gflow_cli.browser_manager import channel_for_profile  # noqa: E402

# Clean 16:9 viewport — also the recorded video size.
_VIEWPORT = {"width": 1280, "height": 720}
_READY_SELECTOR = 'div[role="textbox"][data-slate-editor="true"]'


def _short_locale(locale: str) -> str:
    """Flow URL locale path segment is a SHORT code (pt, en) not BCP-47.

    Mirrors the language-agnostic fix tracked in gflow-cli issue #153: a raw
    ``en-US`` path segment 404s; the primary subtag works on any account.
    """
    return locale.split("-")[0].lower() if locale else "en"


async def _gentle_tour(page: Any, seconds: int) -> None:
    """Create soft motion for a watchable b-roll: slow scroll down/up + hover."""
    deadline = time.monotonic() + seconds
    width = _VIEWPORT["width"]
    height = _VIEWPORT["height"]
    phase = 0
    while time.monotonic() < deadline:
        if phase % 2 == 0:
            await page.mouse.move(width * 0.5, height * 0.45, steps=18)
            await page.mouse.wheel(0, 220)
        else:
            await page.mouse.move(width * 0.4, height * 0.55, steps=18)
            await page.mouse.wheel(0, -220)
        await page.wait_for_timeout(2200)
        phase += 1


async def _run(
    *,
    profile_dir: Path,
    project_id: str,
    entity_id: str,
    locale: str,
    seconds: int,
    out_path: Path,
    headless: bool,
) -> int:
    from playwright.async_api import async_playwright

    rec_dir = out_path.parent / "_rec_tmp"
    rec_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport=cast("Any", _VIEWPORT),
            locale=locale,
            channel=channel_for_profile(profile_dir),
            record_video_dir=str(rec_dir),
            record_video_size=cast("Any", _VIEWPORT),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--password-store=basic",
            ],
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})",
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            # Go straight to the editor (the persistent profile is already
            # authenticated) — skipping a slow _FLOW_URL warm-up keeps the
            # recording mostly real editor footage instead of a loading screen.
            url = routes.character_editor_url(_short_locale(locale), project_id, entity_id)
            step("nav", f"opening character editor: {url}", prefix="rec")
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

            try:
                await page.locator(_READY_SELECTOR).first.wait_for(state="visible", timeout=20_000)
                step("ok", "editor ready (prompt textbox visible)", prefix="rec")
            except Exception:  # noqa: BLE001
                step("warn", f"editor-ready gate timed out; URL={page.url}", prefix="rec")

            # Best-effort overlay dismissal (Escape), then the tour.
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(1500)
            await _gentle_tour(page, seconds)
        finally:
            await ctx.close()  # finalizes the .webm

    # Locate the finalized webm and transcode to mp4.
    webm = _finalize_video(rec_dir)
    if webm is None:
        step("ERR", "no .webm produced by Playwright", prefix="rec")
        return 1
    _transcode(webm, out_path)
    shutil.rmtree(rec_dir, ignore_errors=True)
    step("done", f"recorded -> {out_path}", prefix="rec")
    return 0


def _finalize_video(rec_dir: Path) -> Path | None:
    # The persistent context writes one .webm per page on close; pick the newest.
    webms = sorted(rec_dir.glob("*.webm"), key=lambda p: p.stat().st_mtime, reverse=True)
    return webms[0] if webms else None


def _transcode(src: Path, dst: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        # No ffmpeg: keep the raw webm next to the requested output.
        fallback = dst.with_suffix(".webm")
        shutil.copyfile(src, fallback)
        step("warn", f"ffmpeg not found; kept raw webm -> {fallback}", prefix="rec")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(dst),
        ],
        check=True,
        capture_output=True,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Browser-only Flow recorder (0 credits).")
    p.add_argument("--profile", default=os.environ.get("GFLOW_CLI_PROFILE", "denon82"))
    p.add_argument("--project", required=True)
    p.add_argument("--entity", required=True, help="Existing character entity id (credit-free).")
    p.add_argument("--locale", default="pt")
    p.add_argument("--seconds", type=int, default=22)
    p.add_argument("--out", default=None)
    p.add_argument("--headless", action="store_true")
    args = p.parse_args(argv)

    profile_dir = resolve_profile_dir(args.profile)
    out_path = Path(args.out) if args.out else default_out_path("flow-capture", ".mp4")
    step(
        "--",
        f"profile={args.profile} project={args.project} entity={args.entity} "
        f"locale={args.locale}->{_short_locale(args.locale)} out={out_path}",
        prefix="rec",
    )
    print("[rec] NOTE: this run spends 0 credits (navigation + record only).", flush=True)
    try:
        return asyncio.run(
            _run(
                profile_dir=profile_dir,
                project_id=args.project,
                entity_id=args.entity,
                locale=args.locale,
                seconds=args.seconds,
                out_path=out_path,
                headless=args.headless,
            )
        )
    except KeyboardInterrupt:
        print("[rec] aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
