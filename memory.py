"""
memory.py — Axiom Learning OS Session 6
Persistent memory layer with token-intersection keyword matching.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from schemas import MemoryItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GATEWAY_URL = "http://localhost:8101/v1/chat"
STATE_DIR = Path("state")
MEMORY_FILE = STATE_DIR / "memory.json"

# Standard English stopwords for token-intersection filtering
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "in", "on", "at", "to",
        "for", "of", "with", "by", "from", "is", "was", "are", "were", "be",
        "been", "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can", "not",
        "no", "nor", "so", "yet", "both", "either", "neither", "also", "as",
        "that", "this", "these", "those", "it", "its", "i", "me", "my",
        "we", "our", "you", "your", "he", "she", "they", "them", "his",
        "her", "their", "what", "which", "who", "whom", "when", "where",
        "why", "how", "all", "each", "every", "any", "some", "such",
        "into", "than", "then", "just", "about", "up", "out", "over",
        "after", "before", "through", "during", "without", "within",
        "s", "t", "re", "ve", "ll", "d", "m",
    }
)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def initialize_state() -> None:
    """Create state directory topology and memory file if they do not exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "artifacts").mkdir(parents=True, exist_ok=True)

    if not MEMORY_FILE.exists():
        _write_memory([])
        logger.info("Initialized empty memory file at %s", MEMORY_FILE)


# ---------------------------------------------------------------------------
# Low-level I/O helpers (atomic)
# ---------------------------------------------------------------------------

def _read_memory() -> list[dict[str, Any]]:
    """Return raw memory list from disk; safe against concurrent reads."""
    try:
        text = MEMORY_FILE.read_text(encoding="utf-8")
        if not text.strip():
            # File exists but is empty — reinitialize silently
            _write_memory([])
            return []
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Memory file is malformed (%s); reinitializing.", exc)
        _write_memory([])
        return []
    except FileNotFoundError:
        return []


def _write_memory(records: list[dict[str, Any]]) -> None:
    """Atomically write the full memory list to disk."""
    tmp = MEMORY_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(records, indent=2, default=_json_serial),
        encoding="utf-8",
    )
    tmp.replace(MEMORY_FILE)


def _json_serial(obj: Any) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


# ---------------------------------------------------------------------------
# Public write helpers
# ---------------------------------------------------------------------------

def append_item(item: MemoryItem) -> None:
    """Append a single MemoryItem to persistent storage."""
    records = _read_memory()
    records.append(item.model_dump(mode="json"))
    _write_memory(records)
    logger.debug("Appended memory item id=%s kind=%s", item.id, item.kind)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    """Lowercase, strip non-alphanumeric chars, remove stopwords."""
    raw_tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in raw_tokens if t not in _STOPWORDS}


# ---------------------------------------------------------------------------
# Boot-time extraction
# ---------------------------------------------------------------------------

async def remember_query_intent(user_query: str) -> None:
    """
    Call the gateway with auto_route='memory' to extract any explicitly
    declared facts or preferences from the user query and persist them.
    """
    system_prompt = (
        "You are a fact extractor. Scan the user's message for any explicitly "
        "declared facts, preferences, or personal information "
        "(e.g., 'My mom's birthday is May 25', 'I prefer metric units', "
        "'My name is Alice'). "
        "For each such item, output a JSON array of objects with these fields: "
        '{"kind": "fact"|"preference", "keywords": [list of lowercase keywords], '
        '"descriptor": "short label", "value": {"raw": "the extracted text"}}. '
        "If you find nothing relevant, output an empty JSON array []. "
        "Output ONLY valid JSON — no markdown fences, no prose."
    )

    payload = {
        "auto_route": "memory",
        # 'system' field routes to Gemini's systemInstruction and OpenAI's system role.
        # Do NOT use {"role":"system"} inside messages — Gemini drops those silently.
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_query},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(GATEWAY_URL, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        logger.warning("remember_query_intent gateway error: %s", exc)
        return

    # Extract text from the gateway's ChatResponse: prefer 'parsed', then 'text'
    try:
        if isinstance(data.get("parsed"), dict):
            content = json.dumps(data["parsed"])
        elif data.get("text"):
            content = data["text"]
        else:
            content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = data.get("text", "")

    # Strip markdown code fences if the model wrapped its output
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)

    try:
        items: list[dict[str, Any]] = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("remember_query_intent: could not parse response JSON.")
        return

    if not isinstance(items, list):
        logger.warning("remember_query_intent: expected list, got %s", type(items))
        return

    now = datetime.utcnow()
    for raw in items:
        try:
            kind = raw.get("kind", "fact")
            if kind not in ("fact", "preference", "tool_outcome", "scratchpad"):
                kind = "fact"
            item = MemoryItem(
                id=str(uuid.uuid4()),
                kind=kind,
                keywords=raw.get("keywords", []),
                descriptor=raw.get("descriptor", "extracted item"),
                value=raw.get("value", {}),
                created_at=now,
            )
            append_item(item)
            logger.info("[MEMORY WRITE] Parsed from query → saved: %s", item.descriptor)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create MemoryItem from %s: %s", raw, exc)


# ---------------------------------------------------------------------------
# Read phase — Token-Intersection Matcher
# ---------------------------------------------------------------------------

def read_keyword_match(query: str, history: list[str] | None = None) -> list[MemoryItem]:
    """
    Lightweight token-intersection match over all memory records.

    Tokenizes (query + trailing history), removes stopwords, then intersects
    against each record's keywords list. Returns records with intersection
    length >= 1, sorted descending by match count.
    """
    combined_text = query
    if history:
        combined_text += " " + " ".join(history[-5:])  # use last 5 history entries

    query_tokens = _tokenize(combined_text)

    if not query_tokens:
        return []

    records = _read_memory()
    scored: list[tuple[int, MemoryItem]] = []

    for raw in records:
        try:
            item = MemoryItem.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping invalid memory record: %s", exc)
            continue

        # Tokenize each keyword entry so phrase-style keywords like
        # "birth date" or "key contributions to information theory" are
        # split into individual tokens before intersection — otherwise a
        # single-string phrase never matches individual query tokens.
        item_tokens: set[str] = set()
        for kw in item.keywords:
            if isinstance(kw, str):
                item_tokens.update(_tokenize(kw))
        overlap = len(query_tokens & item_tokens)
        if overlap >= 1:
            scored.append((overlap, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored]
