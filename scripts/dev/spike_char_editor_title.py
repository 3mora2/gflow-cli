#!/usr/bin/env python3
r"""Title-widget recon — why does the character editor show "Untitled Character"? (0 credits).

Decisive experiment for the "Untitled Character" bug (docs/CHARACTER.md §13.1 / §14):
gflow sets ``entityInfo.displayName`` via REST PATCH and ``show``/``list`` read it
back correctly, yet the editor UI shows "Untitled Character". Two candidate fixes:
  (A) editor title is displayName-driven but gflow's editor render happened before
      the PATCH landed → a fresh load should show the name (timing / cosmetic).
  (B) editor title reads a DIFFERENT field/endpoint than displayName → the PATCH to
      displayName can never update it → need to find the real field.

This spike disambiguates A vs B, all credit-free:
  1. create (or reuse) a throwaway CHARACTER entity
  2. open the editor, record the title text + the network reads the editor fires
  3. PATCH entityInfo.displayName to a distinctive marker
  4. RELOAD the editor, record the title text again
  5. dump the DOM of any element whose text is the marker or "Untitled"

If after reload the title == marker  → mechanism A (displayName-driven; timing).
If after reload the title still "Untitled" → mechanism B (look at captured reads).

Credit cost: 0 (createEntity + PATCH + DOM navigation only; no generation).

Usage:
    ! .venv\Scripts\python.exe scripts\dev\spike_char_editor_title.py \
        --profile denon82 --project 580a6bbf-d433-4153-80b9-1842b5a560ea

Outputs go to scripts/dev/_spike_out/ (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _spike_common import build_client, default_out_path, resolve_profile_dir, step  # noqa: E402

from gflow_cli.api import routes  # noqa: E402
from gflow_cli.api.client import FlowApiClient  # noqa: E402


def _unwrap_trpc(data: Any) -> dict[str, Any]:
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError(f"unexpected tRPC reply shape: {type(data).__name__}")
    result = data.get("result", {})
    inner = result.get("data", {}) if isinstance(result, dict) else {}
    payload = inner.get("json", inner) if isinstance(inner, dict) else {}
    if not isinstance(payload, dict):
        raise ValueError("tRPC reply missing result.data.json object")
    return payload


async def _create_entity(client: FlowApiClient, project_id: str) -> str:
    body = {"json": {"projectId": project_id}}
    data = await client._post_json(  # noqa: SLF001
        routes.CREATE_ENTITY_URL, body, content_type="application/json", route_name="createEntity"
    )
    entity_id = _unwrap_trpc(data).get("entityId")
    if not entity_id:
        raise ValueError("createEntity returned no entityId")
    step("0 OK", f"minted entityId={entity_id}", prefix="title")
    return str(entity_id)


# Locale-agnostic probe: (a) elements whose short text == the unique marker, and
# (b) candidate title widgets near the top of the editor (headings / inputs /
# contenteditable) regardless of language, so we can see the title element even
# when it shows a localized default.
_PROBE_JS = r"""
(marker) => {
  const out = [];
  const seen = new Set();
  const push = (el, why) => {
    const txt = (el.value ?? el.getAttribute('placeholder') ?? el.getAttribute('aria-label') ?? el.textContent ?? '').trim();
    if (!txt || txt.length > 80) return;
    const sig = el.tagName + '|' + txt + '|' + why;
    if (seen.has(sig)) return;
    seen.add(sig);
    out.push({
      why,
      tag: el.tagName.toLowerCase(),
      text: txt,
      isInput: el.tagName === 'INPUT' || el.tagName === 'TEXTAREA',
      contentEditable: el.getAttribute('contenteditable'),
      id: el.id || null,
      cls: (el.getAttribute('class') || '').slice(0, 120),
      role: el.getAttribute('role'),
      placeholder: el.getAttribute('placeholder'),
      ariaLabel: el.getAttribute('aria-label'),
      outerHTML: el.outerHTML.slice(0, 320),
    });
  };
  // (a) Marker matches anywhere.
  for (const el of document.querySelectorAll('input,textarea,[contenteditable],h1,h2,h3,[role="heading"],span,div,p')) {
    const txt = (el.value ?? el.textContent ?? '').trim();
    if (txt && txt.includes(marker) && txt.length <= 80) push(el, 'marker');
  }
  // (b) Structural title-widget candidates (language-independent).
  for (const el of document.querySelectorAll('input,textarea,[contenteditable="true"],h1,h2,[role="heading"],[aria-label]')) {
    push(el, 'candidate');
  }
  return { count: out.length, matches: out, docTitle: document.title, url: location.href };
}
"""


async def _probe_title(page: Any, marker: str) -> dict[str, Any]:
    return await page.evaluate(_PROBE_JS, marker)


async def _run(
    *,
    profile_dir: Path,
    headless: bool,
    project_id: str,
    entity_id: str | None,
    marker: str,
    locale: str,
    out_path: Path,
) -> int:
    reads: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "spike": "title-recon",
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "projectId": project_id,
        "marker": marker,
        "locale": locale,
    }

    async with build_client(profile_dir, headless=headless) as client:
        if not entity_id:
            entity_id = await _create_entity(client, project_id)
        else:
            step("0 SKIP", f"using entity={entity_id}", prefix="title")
        result["entityId"] = entity_id

        # PATCH-FIRST: set displayName -> marker BEFORE opening the editor (free Bearer
        # REST). Then a SINGLE editor load tells us whether the editor renders the
        # patched displayName (mechanism A) or ignores it (mechanism B). No reload =
        # no hang.
        body = {
            "entity": {
                "projectId": project_id,
                "entityId": entity_id,
                "entityInfo": {"displayName": marker},
            },
            "updateMask": "entityInfo.displayName",
        }
        step("1", f"PATCH displayName -> {marker!r} (before opening editor)", prefix="title")
        await client._patch_json(routes.FLOW_ENTITIES_URL, body, route_name="spikeTitlePatch")  # noqa: SLF001

        page = await client._checkout_page()  # noqa: SLF001

        def _on_response(resp: Any) -> None:
            url = resp.url
            if any(k in url for k in ("entities", "Entity", "projectInitialData", "entity")):
                reads.append(
                    {"method": resp.request.method, "status": resp.status, "url": url[:200]}
                )

        page.on("response", _on_response)

        outer_html = ""
        try:
            editor_url = routes.character_editor_url(locale, project_id, entity_id)
            step("2", f"opening editor once (post-PATCH): {editor_url}", prefix="title")

            async def _load_and_probe() -> dict[str, Any]:
                await page.goto(editor_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(6_000)  # let the SPA hydrate the title
                return await _probe_title(page, marker)

            try:
                post = await asyncio.wait_for(_load_and_probe(), timeout=75)
            except TimeoutError:
                step(
                    "2 TIMEOUT",
                    "editor load/probe exceeded 75s; capturing whatever loaded",
                    prefix="title",
                )
                post = await asyncio.wait_for(_probe_title(page, marker), timeout=20)

            step(
                "2 OK",
                f"post-PATCH matches={post.get('count')} docTitle={post.get('docTitle')!r}",
                prefix="title",
            )
            try:
                outer_html = await asyncio.wait_for(page.content(), timeout=15)
            except TimeoutError:
                outer_html = "<!-- page.content() timed out -->"

            # Verdict (language-independent: does ANY element show the unique marker?).
            marker_hits = [m for m in post.get("matches", []) if marker in m["text"]]
            shows_marker = bool(marker_hits)
            verdict = (
                "A (displayName-driven: editor shows the patched name on a fresh load -> "
                "gflow opens the editor BEFORE the displayName PATCH lands; fix = PATCH "
                "displayName before/at editor open, or accept that it self-heals on reload)"
                if shows_marker
                else "B (NOT displayName-driven: editor does NOT show the patched name even "
                "though it was PATCHed before load -> the title widget reads a different "
                "field/endpoint; inspect editorReads + candidate widgets for the real source)"
            )
            result.update(
                {
                    "postPatchTitle": post,
                    "editorReads": reads,
                    "markerHits": marker_hits,
                    "verdict": verdict,
                }
            )

            # Write the report INSIDE the client context, before teardown (teardown can
            # be slow/hang on this setup — memory: playwright deadlock).
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            out_path.with_suffix(".html").write_text(outer_html, encoding="utf-8")
        finally:
            client._checkin_page(page)  # noqa: SLF001

    step("==", f"VERDICT: {result.get('verdict', '(none)')}", prefix="title")
    print(f"[title] report   -> {out_path}", flush=True)
    print(
        f"[title] post-PATCH marker hits: {json.dumps([m['text'] for m in result.get('markerHits', [])], ensure_ascii=False)}",
        flush=True,
    )
    print(
        f"[title] candidate widgets: {json.dumps([m['text'] for m in result.get('postPatchTitle', {}).get('matches', []) if m.get('why') == 'candidate'][:12], ensure_ascii=False)}",
        flush=True,
    )
    print(f"[title] editor reads captured: {len(reads)}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Title-widget recon (0 credits).")
    p.add_argument("--profile", default=os.environ.get("GFLOW_CLI_PROFILE", "denon82"))
    p.add_argument("--project", required=True)
    p.add_argument("--entity", default=None)
    p.add_argument("--locale", default="pt")
    p.add_argument("--marker", default="ReconTitleZX9", help="Distinctive displayName to PATCH.")
    p.add_argument("--out", default=None)
    p.add_argument("--headless", action="store_true")
    args = p.parse_args(argv)

    profile_dir = resolve_profile_dir(args.profile)
    out_path = Path(args.out) if args.out else default_out_path("spike_char_editor_title", ".json")
    step(
        "--",
        f"profile={args.profile} project={args.project} entity={args.entity or '(create)'} "
        f"marker={args.marker!r} out={out_path}",
        prefix="title",
    )
    print("[title] NOTE: this run spends 0 credits.", flush=True)
    try:
        return asyncio.run(
            _run(
                profile_dir=profile_dir,
                headless=args.headless,
                project_id=args.project,
                entity_id=args.entity,
                marker=args.marker,
                locale=args.locale,
                out_path=out_path,
            )
        )
    except KeyboardInterrupt:
        print("[title] aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
