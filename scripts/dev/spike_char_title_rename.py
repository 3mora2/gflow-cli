#!/usr/bin/env python3
r"""Title-rename capture — what mutation does the editor's name box fire? (0 credits).

Follow-up to spike_char_editor_title.py, which proved the editor title is NOT driven
by ``entityInfo.displayName`` (the field gflow PATCHes on aisandbox-pa). The editor
has a dedicated name input (placeholder "Nome do personagem") that stays empty, and
its only data read is ``flow.projectInitialData`` on labs.google (a different surface).

This spike types into that name input and captures EVERY request fired during the
fill+blur, so we can read off the exact endpoint + field the editor uses to persist
the title. That endpoint/field is the fix target.

Credit cost: 0 (createEntity + a rename type/blur; no generation).

Usage:
    ! .venv\Scripts\python.exe scripts\dev\spike_char_title_rename.py \
        --profile denon82 --project 580a6bbf-d433-4153-80b9-1842b5a560ea
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
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
    result = data.get("result", {}) if isinstance(data, dict) else {}
    inner = result.get("data", {}) if isinstance(result, dict) else {}
    return inner.get("json", inner) if isinstance(inner, dict) else {}


async def _create_entity(client: FlowApiClient, project_id: str) -> str:
    data = await client._post_json(  # noqa: SLF001
        routes.CREATE_ENTITY_URL,
        {"json": {"projectId": project_id}},
        content_type="application/json",
        route_name="createEntity",
    )
    eid = _unwrap_trpc(data).get("entityId")
    if not eid:
        raise ValueError("createEntity returned no entityId")
    step("0 OK", f"minted entityId={eid}", prefix="rename")
    return str(eid)


# Find the character-name input (locale-agnostic: it's the text input whose
# placeholder/aria-label mentions "personagem"/"character"/"name"/"nome", or the
# first short text input in the editor header).
_FIND_INPUT_JS = r"""
() => {
  const cands = [];
  const inputs = document.querySelectorAll('input[type="text"], input:not([type]), textarea, [contenteditable="true"]');
  let idx = 0;
  for (const el of inputs) {
    const ph = (el.getAttribute('placeholder') || '').toLowerCase();
    const al = (el.getAttribute('aria-label') || '').toLowerCase();
    const hay = ph + ' ' + al;
    const isName = /(nome do personagem|character name|\bname\b|\bnome\b)/.test(hay)
      && !/descreva|describe|prompt|search|buscar|pesquis/.test(hay);
    cands.push({ idx, tag: el.tagName.toLowerCase(), placeholder: el.getAttribute('placeholder'),
                 ariaLabel: el.getAttribute('aria-label'), value: el.value ?? el.textContent ?? '',
                 isName });
    if (isName) { el.setAttribute('data-recon-name-input', '1'); }
    idx++;
  }
  const picked = cands.find(c => c.isName) || null;
  return { picked, candidates: cands.slice(0, 25) };
}
"""


async def _run(
    *,
    profile_dir: Path,
    headless: bool,
    project_id: str,
    entity_id: str | None,
    name: str,
    locale: str,
    out_path: Path,
) -> int:
    requests_log: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "spike": "title-rename",
        "name": name,
        "locale": locale,
        "projectId": project_id,
    }

    async with build_client(profile_dir, headless=headless) as client:
        entity_id = entity_id or await _create_entity(client, project_id)
        result["entityId"] = entity_id
        page = await client._checkout_page()  # noqa: SLF001

        def _on_request(req: Any) -> None:
            url = req.url
            if "aisandbox-pa.googleapis.com" in url or "/fx/api/trpc/" in url:
                pd = None
                try:
                    pd = req.post_data
                except Exception:  # noqa: BLE001
                    pd = None
                requests_log.append(
                    {"method": req.method, "url": url[:220], "postData": (pd or "")[:1500]}
                )

        page.on("request", _on_request)

        try:
            editor_url = routes.character_editor_url(locale, project_id, entity_id)
            step("1", f"opening editor: {editor_url}", prefix="rename")

            async def _open_and_find() -> dict[str, Any]:
                await page.goto(editor_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(6_000)
                return await page.evaluate(_FIND_INPUT_JS)

            found = await asyncio.wait_for(_open_and_find(), timeout=75)
            result["nameInputSearch"] = found
            picked = found.get("picked")
            step(
                "2",
                f"name input found={bool(picked)} "
                f"placeholder={picked.get('placeholder') if picked else None!r}",
                prefix="rename",
            )

            mark = len(requests_log)  # requests before the rename
            if picked:
                # Type into the tagged input and blur to trigger the save.
                loc = page.locator('[data-recon-name-input="1"]')
                await loc.click(timeout=10_000)
                await loc.fill("")  # clear
                await loc.type(name, delay=40)
                await page.keyboard.press("Tab")  # blur -> commit
                await page.wait_for_timeout(3_500)  # debounced save window
                step("3 OK", "typed name + blurred; captured save window", prefix="rename")
            else:
                step("3 SKIP", "no name input located; dumping candidates only", prefix="rename")

            # Requests fired during/after the rename (the save call is among these).
            result["requestsDuringRename"] = requests_log[mark:]
            result["renameHits"] = [
                r for r in requests_log[mark:] if name in (r.get("postData") or "")
            ]
            outer_html = await asyncio.wait_for(page.content(), timeout=15)

            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            out_path.with_suffix(".html").write_text(outer_html, encoding="utf-8")
        finally:
            client._checkin_page(page)  # noqa: SLF001

    hits = result.get("renameHits", [])
    step("==", f"rename save requests carrying {name!r}: {len(hits)}", prefix="rename")
    for h in hits:
        print(f"[rename] SAVE -> {h['method']} {h['url']}", flush=True)
        print(f"[rename]      postData: {h['postData'][:600]}", flush=True)
    if not hits:
        print(
            f"[rename] no request carried the name; {len(result.get('requestsDuringRename', []))} "
            "reqs in window (see report).",
            flush=True,
        )
    print(f"[rename] report -> {out_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Capture the editor name-box save mutation (0 credits)."
    )
    p.add_argument("--profile", default=os.environ.get("GFLOW_CLI_PROFILE", "denon82"))
    p.add_argument("--project", required=True)
    p.add_argument("--entity", default=None)
    p.add_argument("--locale", default="pt")
    p.add_argument("--name", default="ReconNameQ7", help="Name to type into the editor name box.")
    p.add_argument("--out", default=None)
    p.add_argument("--headless", action="store_true")
    args = p.parse_args(argv)

    profile_dir = resolve_profile_dir(args.profile)
    out_path = Path(args.out) if args.out else default_out_path("spike_char_title_rename", ".json")
    step(
        "--",
        f"profile={args.profile} project={args.project} name={args.name!r} out={out_path}",
        prefix="rename",
    )
    print("[rename] NOTE: this run spends 0 credits.", flush=True)
    try:
        return asyncio.run(
            _run(
                profile_dir=profile_dir,
                headless=args.headless,
                project_id=args.project,
                entity_id=args.entity,
                name=args.name,
                locale=args.locale,
                out_path=out_path,
            )
        )
    except KeyboardInterrupt:
        print("[rename] aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
