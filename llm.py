"""Two small LLM calls: extract listing details, and parse a free-text viewing time.

Both fail soft: on any error (or no API key) they return None and the bot keeps working.
"""
from __future__ import annotations

import json
import logging
import os
import re

from core import HE_WEEKDAYS, now

log = logging.getLogger(__name__)

MODEL = os.environ.get("LLM_MODEL", "claude-haiku-5-5")
_client = None


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()
    return _client


async def _ask_json(system: str, user: str, max_tokens: int = 400) -> dict | None:
    if not enabled():
        return None
    try:
        resp = await _get_client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        log.exception("LLM call failed")
        return None


# ---------------------------------------------------------------- listing extraction

EXTRACT_SYSTEM = """You extract details from Israeli apartment rental ads (usually Hebrew).
Return ONLY a JSON object, no prose:
{
  "is_listing": true/false,     // is this text an apartment ad / listing details?
  "address": string|null,       // street + number if present, in Hebrew as written
  "neighborhood": string|null,  // neighborhood only, without the city
  "city": string|null,          // city / town, in Hebrew
  "price": integer|null,        // monthly rent in ILS
  "rooms": number|null,         // e.g. 3 or 3.5
  "contact_name": string|null,
  "phone": string|null          // as written
}
Use null for anything not stated. Do not guess."""


def _clean_listing(d: dict) -> dict:
    out = {"is_listing": bool(d.get("is_listing"))}
    for k in ("address", "neighborhood", "city", "contact_name", "phone"):
        v = d.get(k)
        out[k] = str(v).strip() if v not in (None, "", "null") else None
    try:
        p = d.get("price")
        p = int(re.sub(r"[^\d]", "", str(p))) if p not in (None, "") else None
        out["price"] = p if p and 300 <= p <= 100_000 else None
    except ValueError:
        out["price"] = None
    try:
        r = d.get("rooms")
        r = float(r) if r not in (None, "") else None
        out["rooms"] = r if r and 0.5 <= r <= 15 else None
    except (TypeError, ValueError):
        out["rooms"] = None
    return out


async def extract_listing(text: str) -> dict | None:
    d = await _ask_json(EXTRACT_SYSTEM, f"<ad>\n{text[:4000]}\n</ad>")
    return _clean_listing(d) if d else None


# ---------------------------------------------------------------- corrections

FIX_SYSTEM = """You apply a user's correction to an apartment record (Israeli rental, Hebrew).
You get the current fields as JSON and the user's correction in free Hebrew text.
Return ONLY a JSON object with the fields the user wants to change, nothing else:
keys from: address, neighborhood, city, price, rooms, contact_name, phone.
- address: street + number. neighborhood: neighborhood only. city: city / town only.
- To clear a field, set it to null.
- If the text does not ask to change any field, return {}.
Do not change fields the user did not mention."""


async def extract_fix(current: dict, text: str) -> dict | None:
    """Returns {field: raw_value} to change, {} if nothing, or None if the LLM is unavailable."""
    keys = ("address", "neighborhood", "city", "price", "rooms", "contact_name", "phone")
    cur = {k: current.get(k) for k in keys}
    d = await _ask_json(FIX_SYSTEM, f"<current>{json.dumps(cur, ensure_ascii=False)}</current>\n"
                                    f"<correction>{text[:1000]}</correction>", max_tokens=200)
    if d is None:
        return None
    return {k: d[k] for k in keys if k in d}


# ---------------------------------------------------------------- viewing time

WHEN_SYSTEM = """You convert a Hebrew free-text appointment time into a date and time.
Return ONLY JSON: {"date": "YYYY-MM-DD" or null, "time": "HH:MM" or null}
Rules: always the nearest FUTURE date matching the text. Bare hours 1-7 mean PM.
If no date can be inferred, return {"date": null, "time": null}."""


async def parse_when(text: str) -> str | None:
    n = now()
    ctx = f"Now: {n.strftime('%Y-%m-%d %H:%M')} (יום {HE_WEEKDAYS[n.weekday()]}), Israel time.\nText: {text}"
    d = await _ask_json(WHEN_SYSTEM, ctx, max_tokens=100)
    if not d or not d.get("date"):
        return None
    try:
        from datetime import date as _date
        _date.fromisoformat(d["date"])
        if d.get("time") and re.fullmatch(r"\d{2}:\d{2}", d["time"]):
            return f"{d['date']}T{d['time']}"
        return d["date"]
    except ValueError:
        return None
