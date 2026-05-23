"""
decision.py — Axiom Learning OS Session 6
Decision layer with Force-Attach safety net for artifact content injection.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from schemas import DecisionOutput, Goal, MemoryItem, ToolCall

logger = logging.getLogger(__name__)

GATEWAY_URL = "http://localhost:8101/v1/chat"
STATE_DIR = Path("state")
ARTIFACTS_DIR = STATE_DIR / "artifacts"


# ---------------------------------------------------------------------------
# Synthesis keywords configuration (dynamic)
# ---------------------------------------------------------------------------

def _load_synthesis_keywords() -> list[str]:
    """
    Load synthesis keywords from configuration.
    
    Tries in order:
    1. From environment variable SYNTHESIS_KEYWORDS (comma-separated)
    2. From config file state/synthesis_keywords.json (JSON array)
    3. Returns built-in defaults
    """
    # Try environment variable first
    env_keywords = os.getenv("SYNTHESIS_KEYWORDS")
    if env_keywords:
        keywords = [kw.strip() for kw in env_keywords.split(",") if kw.strip()]
        if keywords:
            logger.info("Loaded synthesis keywords from environment: %d keywords", len(keywords))
            return keywords
    
    # Try config file
    config_file = STATE_DIR / "synthesis_keywords.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                data = json.load(f)
                if isinstance(data, list):
                    logger.info("Loaded synthesis keywords from config file: %d keywords", len(data))
                    return data
        except Exception as exc:
            logger.warning("Could not load synthesis keywords from config: %s", exc)
    
    # Built-in defaults
    defaults = [
        "based on",
        "select",
        "identify",
        "compare",
        "analyze",
        "extract",
        "recommend",
        "suitable",
        "appropriate",
        "most",
        "best",
        "using",
        "given",
        "synthesize",
        "combine",
        "evaluate",
        "match",
        "filter",
    ]
    logger.info("Using built-in synthesis keywords: %d keywords", len(defaults))
    return defaults


# Load keywords once at module startup
SYNTHESIS_KEYWORDS = _load_synthesis_keywords()


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a precise, deterministic action-planning agent.

Given an active goal, memory context, and execution history, you must decide your next step.

OPTION A — TERMINAL ANSWER:
  If you can resolve the goal entirely from existing context, return your final answer text directly. Do not use any tools.

OPTION B — TOOL CALL:
  If you need to take an action, use exactly one of the provided tools.
  ALL required arguments MUST be populated with real, concrete values extracted
  directly from the goal text or execution history — never leave arguments empty.

ARGUMENT EXTRACTION RULES (mandatory):
- Read the ACTIVE GOAL text carefully. Extract every concrete value mentioned
  (URLs, numbers, currency codes, file names, search terms, etc.).
- Map those values to the correct parameter names in the tool's input schema.
- If a goal says "Fetch https://example.com", set: arguments: {"url": "https://example.com"}
- NEVER submit an empty arguments dict {} for any tool.
- NEVER use placeholder strings. NEVER use artifact handles as argument values.

ARTIFACT SUFFICIENCY RULE (CRITICAL):
If attached artifacts, execution history, or memory contain enough information
to produce a reasonable best-effort answer for the active goal,
you MUST return a TERMINAL ANSWER.

You are FORBIDDEN from calling tools merely to:
- improve answer quality
- obtain a more authoritative source
- obtain a more specific version of existing data
- confirm already available information
- search for recommendations derivable from existing context

Cross-artifact synthesis and reasoning MUST be performed locally.

CONSTRAINTS (these are ABSOLUTE rules — no exceptions):
1. When [ATTACHED ARTIFACT CONTENT] is present, first judge relevance:
   a. RELEVANT — the artifact(s) contain data that addresses the active goal (even partially
      or approximately, e.g. monthly weather when Saturday is asked for):
        → You MUST produce a TERMINAL ANSWER (Option A).
        → Tool calls are FORBIDDEN. Synthesize the best answer you can from the available
          data. Do NOT search for a "more specific" version of data you already have.
   b. NOT RELEVANT — the artifact(s) are entirely unrelated to the active goal
      (e.g. only activity listings are attached, but the goal is to fetch weather):
        → A tool call (Option B) is permitted to fetch the missing data.
        → Do NOT re-fetch data that is already covered by an attached artifact.
2. FILE OUTPUT RULE: If the goal involves saving a reminder, calendar entry, note,
   schedule, or any persistent output to a file:
   → You MUST use the create_file tool (Option B). Do NOT return a terminal answer.
   → Extract the filename directly from the goal text.
   → Write the full, human-readable reminder/note content as the file body.
   → One create_file call per goal — never bundle multiple files into one call.
3. NEVER submit an empty arguments dict {} for any tool.
4. NEVER use artifact handles as argument values.
5. Complete the goals one by one in the order they are presented.

### GUIDELINES FOR TOOL CALLS:
1. SPECIFICITY: Never use single-word search queries. Include locations, dates, and specific subjects.
2. ENTITY RETENTION: If a goal mentions 'Tokyo' and 'Weather', the search query MUST contain both.
3. TOOL SELECTION: Use 'fetch_url' for specific websites and 'web_search' for general discovery.
4. MEMORY FIRST: For personal information (like birthdays), rely on the provided Memory context before using tools.

### EXAMPLES:
Goal: "Find the price of Bitcoin"
Action: web_search(query="current Bitcoin price in USD")

Goal: "What is the weather like?"
Action: web_search(query="current weather forecast and temperature in [User's Location]")

Goal: "Read the article at example.com/news"
Action: fetch_url(url="https://example.com/news")

"""


# ---------------------------------------------------------------------------
# Force-Attach helpers
# ---------------------------------------------------------------------------


def _load_artifact_content(artifact_id: str) -> str | None:
    """
    Load a binary artifact from disk given its handle (e.g., 'art:<hash>').
    Returns the decoded UTF-8 string or None if loading fails.
    """
    if not artifact_id.startswith("art:"):
        return None
    hash_val = artifact_id[4:]
    bin_path = ARTIFACTS_DIR / f"{hash_val}.bin"
    try:
        raw_bytes = bin_path.read_bytes()
        return raw_bytes.decode("utf-8", errors="replace")
    except FileNotFoundError:
        logger.warning("Artifact file not found: %s", bin_path)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load artifact %s: %s", artifact_id, exc)
        return None


def _collect_artifact_ids_from_history(history: list[str]) -> list[str]:
    """Scan history entries for 'art:<hash>' handles and return unique ones."""
    pattern = re.compile(r"art:[a-f0-9]{64}")
    seen: set[str] = set()
    for entry in history:
        for match in pattern.finditer(entry):
            seen.add(match.group())
    return list(seen)


def _goal_needs_synthesis(goal_text: str) -> bool:
    """
    Heuristic to detect if a goal requires synthesis/analysis of prior results.
    
    Synthesis goals contain language from SYNTHESIS_KEYWORDS.
    Action/retrieval goals like "fetch X", "create file Y", "search for Z" don't need artifacts.
    """
    goal_lower = goal_text.lower()
    return any(keyword in goal_lower for keyword in SYNTHESIS_KEYWORDS)


# ---------------------------------------------------------------------------
# Tool schema builder
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


async def run_decision(
    active_goal: Goal,
    memory_hits: list[MemoryItem],
    history: list[str],
    mcp_tools: list[dict[str, Any]],
) -> DecisionOutput:
    """
    Call the decision gateway for the active (unfinished) goal.

    Applies the Force-Attach safety net before dispatch:
    If the goal implies synthesis/extraction AND an artifact handle is present
    in the history, loads the artifact content and injects it as a dedicated
    message block so the model can work on it directly.
    """
    # System prompt lives outside messages so Gemini routes it to systemInstruction.
    # Do NOT use {"role":"system"} inside messages — Gemini drops those silently.
    messages: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # Force-Attach: conditionally inject artifacts based on goal type.
    # Only attach if:
    #   1. The goal explicitly requests artifacts (attach_artifact_id is set), OR
    #   2. The goal text suggests synthesis/analysis (contains keywords like
    #      "based on", "select", "identify", "compare", etc.)
    #
    # Action/retrieval goals like "Fetch X from URL" or "Search for Y" don't
    # need artifacts, so we skip the overhead of loading and injecting them.
    # ------------------------------------------------------------------
    artifact_injection: str = ""
    goal_needs_artifacts = _goal_needs_synthesis(active_goal.text) or active_goal.attach_artifact_id is not None

    if goal_needs_artifacts:
        art_ids: list[str] = _collect_artifact_ids_from_history(history)

        # Also honour any artifact the perception model explicitly pinned
        if active_goal.attach_artifact_id:
            art_ids.append(active_goal.attach_artifact_id)

        # Deduplicate
        art_ids = list(dict.fromkeys(art_ids))

        if art_ids:
            blocks: list[str] = []
            for art_id in art_ids:
                content = _load_artifact_content(art_id)
                if content is not None:
                    blocks.append(
                        f"--- BEGIN {art_id} ---\n{content}\n--- END {art_id} ---"
                    )
                    logger.info(
                        "Force-attached artifact %s (%d chars)", art_id, len(content)
                    )
            if blocks:
                artifact_injection = "\n\n[ATTACHED ARTIFACT CONTENT]\n" + "\n\n".join(
                    blocks
                )

    # ------------------------------------------------------------------
    # Build user message
    # ------------------------------------------------------------------
    mem_block = (
        "\n".join(
            f"- [{item.kind}] {item.descriptor}: {json.dumps(item.value)}"
            for item in memory_hits
        )
        if memory_hits
        else "(none)"
    )

    history_block = (
        "\n".join(f"Turn {i + 1}: {entry}" for i, entry in enumerate(history))
        if history
        else "(none)"
    )

    # Build an explicit extraction reminder so the model sees the goal values
    # immediately before the reminder to use them — prevents empty-arguments calls.
    extraction_reminder = (
        f"\n\n## ARGUMENT EXTRACTION REMINDER\n"
        f'The active goal text is: "{active_goal.text}"\n'
        f"Extract every concrete value from that text (URLs, search terms, file names, "
        f"amounts, currency codes, etc.) and populate the required tool arguments with them. "
        f"Do NOT submit an empty arguments dict."
    )

    user_content = (
        f"## ACTIVE GOAL (index={active_goal.index})\n{active_goal.text}\n\n"
        f"## RELEVANT MEMORY\n{mem_block}\n\n"
        f"## EXECUTION HISTORY\n{history_block}\n\n"
        + extraction_reminder
        + artifact_injection
    )

    messages.append({"role": "user", "content": user_content})

    gateway_tools = []
    for tool in mcp_tools:
        gateway_tools.append(
            {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", tool.get("parameters", {})),
            }
        )

    payload = {
        "auto_route": "decision",
        # 'system' field routes to Gemini's systemInstruction and OpenAI's system role.
        "system": _SYSTEM_PROMPT,
        "messages": messages,
        "tools": gateway_tools,
        "tool_choice": "auto",
    }

    # Force-attach payloads (large artifact content) can exceed 8000 estimated
    # tokens, triggering the gateway's HUGE-tier 503.  Bypass auto_route tier
    # classification by pinning to Gemini directly (1M context window).
    # The HUGE check only fires when auto_route is set AND provider is NOT set.
    if artifact_injection:
        payload["provider"] = "nvidia"

    import asyncio

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(GATEWAY_URL, json=payload)
                response.raise_for_status()
                data = response.json()
                break  # Success
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (502, 503) and attempt < max_attempts - 1:
                logger.warning(
                    f"Gateway returned {exc.response.status_code}, cooling off for 60s (attempt {attempt + 1}/{max_attempts})..."
                )
                await asyncio.sleep(60)
                continue
            logger.error("Decision gateway error: %s", exc)
            return DecisionOutput(
                answer=f"[Decision gateway error: {exc}]",
                tool_call=None,
            )
        except httpx.HTTPError as exc:
            logger.error("Decision gateway error: %s", exc)
            return DecisionOutput(
                answer=f"[Decision gateway error: {exc}]",
                tool_call=None,
            )

    tool_calls = data.get("tool_calls", [])
    text_content = data.get("text", "")

    # ------------------------------------------------------------------
    # SYNTHESIS GOAL CONSTRAINT ENFORCEMENT
    # If this is a synthesis goal with artifacts already attached, reject
    # any tool calls and force the model to synthesize from existing data.
    # This enforces the "ARTIFACT SUFFICIENCY RULE" from the system prompt.
    # ------------------------------------------------------------------
    is_synthesis_goal = _goal_needs_synthesis(active_goal.text)
    has_artifacts = bool(artifact_injection)

    if is_synthesis_goal and has_artifacts and tool_calls:
        logger.warning(
            "CONSTRAINT VIOLATION: Synthesis goal attempted tool call despite "
            "artifacts being available. Forcing terminal answer from model response."
        )
        logger.info(
            "Goal: %s | Attempted tool: %s | Forcing synthesis from artifacts instead.",
            active_goal.text,
            tool_calls[0].get("name", "unknown"),
        )
        # Reject the tool call and use text content as forced answer
        tool_calls = []
        if not text_content:
            text_content = (
                "[Synthesis constraint enforced: model attempted tool call despite "
                "having artifacts. Synthesizing answer from existing data instead.]"
            )

    if tool_calls:
        tc_data = tool_calls[0]
        try:
            tool_call_obj = ToolCall(
                name=tc_data.get("name", ""), arguments=tc_data.get("arguments", {})
            )
            decision = DecisionOutput(answer=None, tool_call=tool_call_obj)
        except Exception as exc:
            logger.error("Decision: could not parse tool_call (%s)", exc)
            return DecisionOutput(
                answer=f"[Decision parse error: invalid tool call structure. {exc}]",
                tool_call=None,
            )
    else:
        decision = DecisionOutput(answer=text_content, tool_call=None)

    # Sanity check
    if not decision.answer and not decision.tool_call:
        logger.warning(
            "Decision returned neither answer nor tool_call — injecting error answer."
        )
        decision = DecisionOutput(
            answer="[Agent error: decision model returned empty output. Aborting this step.]",
            tool_call=None,
        )
    logger.info(f"Decision: {decision}")
    return decision
