"""
perception.py — Axiom Learning OS Session 6
Perception layer: state auditor that generates and updates Goal milestones.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from schemas import Goal, MemoryItem, Observation

logger = logging.getLogger(__name__)

GATEWAY_URL = "http://localhost:8101/v1/chat"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a deterministic, un-hallucinating state auditor for a task-completion agent.

Your job is to manage an ordered list of atomic goal milestones.

Rules you must follow without exception:
1. If the prior goals list is EMPTY, generate an ordered array of independent atomic
   milestones necessary to fulfill the user request. Each milestone must be small,
   concrete, and independently verifiable.
   - CRITICAL: You MUST preserve all specific URLs, file paths, IDs, and exact terms from the user query in the goal text. Never abstract them away (e.g., write "Fetch https://en.wikipedia.org/wiki/..." instead of "Fetch the Wikipedia page").
   - FILE OUTPUT RULE: If the user asks for a reminder, calendar entry, note, schedule,
     or any persistent output that should be saved, decompose it into ONE goal per file.
     Each goal must specify:
       * The exact filename (derive from event name + date)
       * The content to write
     Use the create_file tool for these goals. Never bundle multiple files into one goal.
2. If prior goals list is NON-EMPTY, review the execution history and audit whether
   any tool output or intermediate answer has fulfilled each pending goal.
   - Toggle a goal's "done" field to true ONLY when the history provides clear
     evidence of completion for that specific goal.
   - DECOMPOSITION RULE: Always decompose work into explicit, atomic goals (e.g.,
     "Fetch URL 1", "Fetch URL 2"). Never use vague grouped goals (e.g., "Fetch 3 URLs").
   - DYNAMIC REGENERATION: If the execution history reveals new concrete information
     (like a list of URLs from a search result), you MUST regenerate the goal list to
     include new specific, atomic goals for each individual item, replacing any older
     vague goals. You are free to assign new sequential indices to ensure chronological order.
   - A file-creation goal is DONE only when the history shows a successful create_file
     tool call for that specific filename.
   - Never hallucinate completion. If uncertain, leave "done" as false.
3. Set "all_done" to true ONLY when every goal in the list has done=true.

Output FORMAT — you must output ONLY a valid JSON object matching this schema:
{
  "goals": [
    {"index": <int>, "text": "<string>", "done": <bool>, "attach_artifact_id": <null|string>}
  ],
  "all_done": <bool>
}
No markdown fences. No prose. No comments. Pure JSON only.
"""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def run_perception(
    user_query: str,
    memory_hits: list[MemoryItem],
    prior_goals: list[Goal],
    history: list[str],
) -> Observation:
    """
    Call the perception gateway to generate or update the milestone list.

    Returns an Observation containing the (possibly updated) goals array.
    """
    # Build a rich context string so the LLM has full situational awareness
    context_parts: list[str] = [
        f"## USER QUERY\n{user_query}",
    ]

    if memory_hits:
        mem_block = "\n".join(
            f"- [{item.kind}] {item.descriptor}: {json.dumps(item.value)}"
            for item in memory_hits
        )
        context_parts.append(f"## RELEVANT MEMORY ITEMS\n{mem_block}")
    else:
        context_parts.append("## RELEVANT MEMORY ITEMS\n(none)")

    if prior_goals:
        goals_block = json.dumps(
            [g.model_dump(mode="json") for g in prior_goals], indent=2
        )
        context_parts.append(f"## PRIOR GOALS (DO NOT REORDER OR RENAME)\n{goals_block}")
    else:
        context_parts.append("## PRIOR GOALS\n(empty — generate fresh milestone list)")

    if history:
        history_block = "\n".join(
            f"Turn {i + 1}: {entry}" for i, entry in enumerate(history)
        )
        context_parts.append(f"## EXECUTION HISTORY (chronological)\n{history_block}")
    else:
        context_parts.append("## EXECUTION HISTORY\n(none — first iteration)")

    art_ids = _collect_artifact_ids_from_history(history)
    artifact_injection = ""
    if art_ids:
        blocks = []
        for art_id in art_ids:
            content = _load_artifact_content(art_id)
            if content:
                blocks.append(f"--- BEGIN {art_id} ---\n{content}\n--- END {art_id} ---")
                logger.info("Force-attached artifact %s (%d chars)", art_id, len(content))
        if blocks:
            artifact_injection = "\n\n[ATTACHED ARTIFACT CONTENT]\n" + "\n\n".join(blocks)

    user_content = "\n\n".join(context_parts) + artifact_injection

    payload = {
        "auto_route": "perception",
        "system": _SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "schema": Observation.model_json_schema(),
            "name": "Observation",
            "strict": True,
        },
    }

    if artifact_injection:
        payload["provider"] = "nvidia"

    import asyncio
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(GATEWAY_URL, json=payload)
                response.raise_for_status()
                data = response.json()
                break  # Success
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (502, 503) and attempt < max_attempts - 1:
                logger.warning(f"Gateway returned {exc.response.status_code}, cooling off for 25s (attempt {attempt+1}/{max_attempts})...")
                await asyncio.sleep(25)
                continue
            logger.error("Perception gateway error: %s", exc)
            all_done = all(g.done for g in prior_goals) if prior_goals else False
            return Observation(goals=prior_goals, all_done=all_done)
        except httpx.HTTPError as exc:
            logger.error("Perception gateway error: %s", exc)
            # Return existing goals unchanged to preserve loop integrity
            all_done = all(g.done for g in prior_goals) if prior_goals else False
            return Observation(goals=prior_goals, all_done=all_done)

    content = _extract_content(data)
    content = _strip_fences(content)

    try:
        raw = json.loads(content)
        observation = Observation.model_validate(raw)
        logger.info(
            "Perception: %d goals, all_done=%s",
            len(observation.goals),
            observation.all_done,
        )
        return observation
    except Exception as exc:  # noqa: BLE001
        logger.error("Perception: could not parse response (%s). Raw: %s", exc, content[:400])
        # Fall back to existing state — don't crash the loop
        all_done = all(g.done for g in prior_goals) if prior_goals else False
        return Observation(goals=prior_goals, all_done=all_done)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ARTIFACTS_DIR = Path("state") / "artifacts"

def _load_artifact_content(artifact_id: str) -> str | None:
    if not artifact_id.startswith("art:"):
        return None
    hash_val = artifact_id[4:]
    bin_path = ARTIFACTS_DIR / f"{hash_val}.bin"
    try:
        return bin_path.read_bytes().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Could not load artifact %s: %s", artifact_id, exc)
        return None

def _collect_artifact_ids_from_history(history: list[str]) -> list[str]:
    pattern = re.compile(r"art:[a-f0-9]{64}")
    seen: set[str] = set()
    for entry in history:
        for match in pattern.finditer(entry):
            seen.add(match.group())
    return list(seen)

# ---------------------------------------------------------------------------

def _extract_content(data: dict[str, Any]) -> str:
    """
    Pull the assistant message content from a llm_gatewayV3 ChatResponse.

    The gateway returns:
      { "text": "...", "parsed": {...}, "provider": "...", ... }

    For structured (response_format) calls the JSON object is also in
    'parsed'.  We prefer 'parsed' serialised back to a string when present
    so callers always get a JSON string they can json.loads().
    """
    # Prefer pre-validated parsed object (structured output path)
    if isinstance(data.get("parsed"), dict):
        import json as _json
        return _json.dumps(data["parsed"])
    # Plain text response
    text = data.get("text", "")
    if text:
        return text
    # Fallback: OpenAI-compatible envelope (shouldn't happen with this gateway)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""


def _strip_fences(text: str) -> str:
    """Remove markdown code fences that the LLM may have erroneously added."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()
