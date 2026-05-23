"""
agent6.py — Axiom Learning OS Session 6
Core orchestration loop: 10-turn deterministic agent with memory, perception,
decision, and action layers wired together.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
sys.stdout.reconfigure(encoding='utf-8')
import traceback
from pathlib import Path

from action import create_mcp_session, dispatch_tool_call, list_mcp_tools
from decision import run_decision
from memory import (
    initialize_state,
    read_keyword_match,
    remember_query_intent,
)
from perception import run_perception
from schemas import Goal, MemoryItem, Observation

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)


def _attach_file_handler() -> None:
    """Add a FileHandler to the root logger after state/ is guaranteed to exist."""
    log_path = Path("state") / "agent6.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(fh)


def _reset_state() -> None:
    """Delete the state/ directory from any previous run before starting fresh.

    Called before initialize_state() so there are no open file handles yet.
    initialize_state() will recreate the directory tree immediately after.
    """
    state_dir = Path("state")
    if not state_dir.exists():
        return

    # Only wipe session-specific data — preserve memory.json so that
    # cross-session persistent memory survives across runs.
    artifacts_dir = state_dir / "artifacts"
    if artifacts_dir.exists():
        shutil.rmtree(artifacts_dir)

    log_file = state_dir / "agent6.log"
    if log_file.exists():
        log_file.unlink()

    logging.getLogger("agent6").info(
        "[BOOT] Cleared session artifacts and log. Memory preserved."
    )

logger = logging.getLogger("agent6")


MAX_TURNS = 10


# ---------------------------------------------------------------------------
# Final synthesis
# ---------------------------------------------------------------------------

async def _final_synthesis(
    user_query: str,
    history: list[str],
    goals: list[Goal],
    exit_reason: str,
) -> str:
    """
    Produce a clean, final human-readable answer from the loop's history.

    Artifacts are intentionally NOT re-attached here — perception has already
    read them and marked goals done. History + goal status are sufficient context
    for synthesis, and avoids sending huge payloads that exceed provider limits.

    Falls back to an informative failure traceback if history is empty.
    """
    import httpx, json, re

    GATEWAY_URL = "http://localhost:8101/v1/chat"

    all_done = all(g.done for g in goals)
    status_line = "All goals completed successfully." if all_done else f"Loop exited: {exit_reason}"

    history_block = (
        "\n".join(f"Turn {i + 1}: {entry}" for i, entry in enumerate(history))
        if history
        else "(no history recorded)"
    )

    goals_block = (
        "\n".join(
            f"[{'✓' if g.done else '✗'}] Goal {g.index}: {g.text}" for g in goals
        )
        if goals
        else "(no goals generated)"
    )

    system_prompt = (
        "You are a final answer synthesizer. Given the user's original request, "
        "the goal completion status, and the full execution history of an agent loop, "
        "produce a single, clean, well-formatted final answer that directly addresses "
        "the user's request. Do not repeat the history verbatim. Synthesize the key "
        "findings into a coherent response."
    )

    user_content = (
        f"## ORIGINAL REQUEST\n{user_query}\n\n"
        f"## STATUS\n{status_line}\n\n"
        f"## GOAL COMPLETION\n{goals_block}\n\n"
        f"## EXECUTION HISTORY\n{history_block}"
    )

    payload = {
        "auto_route": "decision",
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_content},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(GATEWAY_URL, json=payload)
            response.raise_for_status()
            data = response.json()
        # Gateway ChatResponse uses 'text' (not OpenAI choices envelope)
        content = data.get("text", "") or ""
        if not content:
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                content = ""
        # Strip code fences if present
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-z]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        return content.strip()
    except Exception as exc:  # noqa: BLE001
        # If synthesis itself fails, build a plain traceback summary
        return (
            f"[Final Synthesis Failed: {exc}]\n\n"
            f"Status: {status_line}\n\n"
            f"Goals:\n{goals_block}\n\n"
            f"Last history entries:\n"
            + "\n".join(history[-3:] if history else ["(none)"])
        )


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

async def run_agent(user_query: str) -> None:
    """
    Execute the Axiom Learning OS Session 6 agent loop.

    Sequential operations per the spec:
    1. Boot & state initialization
    2. Pre-flight memory extraction
    3. MCP session creation
    4. Deterministic for-loop (max 10 turns):
       a. Top-of-loop memory read
       b. Perception pass
       c. Break if all_done
       d. Goal selection (first undone)
       e. Decision pass (with Force-Attach check)
       f. Action dispatch or answer recording
    5. Final synthesis & print
    """
    logger.info("=" * 60)
    logger.info("AGENT SESSION START")
    logger.info("Query: %s", user_query)
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 1. Boot
    # ------------------------------------------------------------------
    _reset_state()              # wipe any leftover state/ from previous run
    initialize_state()          # recreate state/ and state/artifacts/
    _attach_file_handler()      # safe to open log file now


    # ------------------------------------------------------------------
    # 2. Pre-flight memory extraction
    # ------------------------------------------------------------------
    logger.info("[BOOT] Running pre-flight memory extraction...")
    await remember_query_intent(user_query)

    # ------------------------------------------------------------------
    # 3. MCP session
    # ------------------------------------------------------------------
    logger.info("[BOOT] Initializing MCP client session...")
    mcp_session, mcp_exit_stack = await create_mcp_session()
    mcp_tools = await list_mcp_tools(mcp_session)
    logger.info("[BOOT] MCP session ready. Tools available: %s", [t["name"] for t in mcp_tools])

    # ------------------------------------------------------------------
    # Shared loop state
    # ------------------------------------------------------------------
    history: list[str] = []
    prior_goals: list[Goal] = []
    exit_reason = "max iterations reached"

    try:
        # ---------------------------------------------------------------
        # 4. Main deterministic loop
        # ---------------------------------------------------------------
        for iteration in range(1, MAX_TURNS + 1):
            logger.info("-" * 50)
            logger.info("[ITERATION %d of %d (Max Turns)]", iteration, MAX_TURNS)

            # -----------------------------------------------------------
            # 4a. Top-of-loop memory read (every turn)
            # -----------------------------------------------------------
            memory_hits: list[MemoryItem] = read_keyword_match(user_query, history)
            logger.info("[MEMORY] %d relevant item(s) loaded.", len(memory_hits))

            # -----------------------------------------------------------
            # 4b. Perception pass
            # -----------------------------------------------------------
            logger.info("[PERCEPTION] Running perception model...")
            observation: Observation = await run_perception(
                user_query=user_query,
                memory_hits=memory_hits,
                prior_goals=prior_goals,
                history=history,
            )

            # Merge perception output into prior_goals, preserving index integrity
            prior_goals = observation.goals

            logger.info(
                "[PERCEPTION] %d goal(s), all_done=%s",
                len(prior_goals),
                observation.all_done,
            )
            for g in prior_goals:
                status = "DONE" if g.done else "PENDING"
                logger.info("[GOALS] [%s] index=%d: %s", status, g.index, g.text)

            # -----------------------------------------------------------
            # 4c. Break if all goals complete
            # -----------------------------------------------------------
            if observation.all_done:
                exit_reason = "all goals completed"
                logger.info("[LOOP] All goals marked done — exiting loop.")
                break

            # -----------------------------------------------------------
            # 4d. Goal selection: first undone goal
            # -----------------------------------------------------------
            active_goal: Goal | None = next(
                (g for g in prior_goals if not g.done), None
            )

            if active_goal is None:
                # Defensive: perception said not all_done but we have no undone goal
                logger.warning(
                    "[LOOP] No undone goal found but all_done=False — forcing exit."
                )
                exit_reason = "inconsistent perception state (no undone goals)"
                break

            logger.info(
                "[GOAL] Active: index=%d  text=%s", active_goal.index, active_goal.text
            )

            # -----------------------------------------------------------
            # 4e. Decision pass
            # -----------------------------------------------------------
            logger.info("[DECISION] Running decision model...")
            decision = await run_decision(
                active_goal=active_goal,
                memory_hits=memory_hits,
                history=history,
                mcp_tools=mcp_tools,
            )

            # -----------------------------------------------------------
            # 4f. Act on decision
            # -----------------------------------------------------------
            if decision.answer is not None:
                # Model provided a terminal answer for this goal
                entry = (
                    f"[ANSWER for goal {active_goal.index}] {decision.answer}"
                )
                history.append(entry)
                logger.info("[ANSWER] %s", decision.answer[:200])
                # Continue to next turn so perception can mark this done
                continue

            if decision.tool_call is not None:
                tool_call = decision.tool_call
                logger.info(
                    "[TOOL] Calling %s with args: %s",
                    tool_call.name,
                    tool_call.arguments,
                )
                descriptor = await dispatch_tool_call(
                    session=mcp_session,
                    tool_call=tool_call,
                    tool_schemas=mcp_tools,
                )
                entry = (
                    f"[TOOL_RESULT for goal {active_goal.index}] "
                    f"Tool={tool_call.name} | {descriptor}"
                )
                history.append(entry)
                logger.info("[TOOL_RESULT] %s", descriptor[:300])
                continue

            # Should never reach here due to decision.py sanity checks,
            # but handle gracefully
            logger.error("[LOOP] Decision returned no answer and no tool_call.")
            history.append(
                f"[ERROR turn {iteration}] Decision model returned empty output."
            )

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.critical("[LOOP CRASH] Unhandled exception:\n%s", tb)
        history.append(f"[CRASH] {exc}")
        exit_reason = f"unhandled exception: {exc}"

    finally:
        # ------------------------------------------------------------------
        # Always close MCP session
        # ------------------------------------------------------------------
        try:
            await mcp_exit_stack.aclose()
            logger.info("[BOOT] MCP session closed.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error closing MCP session: %s", exc)

    # ------------------------------------------------------------------
    # 5. Final synthesis
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("[SYNTHESIS] Generating final answer... (exit: %s)", exit_reason)
    final_answer = await _final_synthesis(
        user_query=user_query,
        history=history,
        goals=prior_goals,
        exit_reason=exit_reason,
    )

    print("\n" + "=" * 60)
    print("FINAL ANSWER")
    print("=" * 60)
    print(final_answer)
    print("=" * 60 + "\n")

    logger.info("[SESSION END] Done.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python agent6.py \"<your query here>\"")
        sys.exit(1)

    user_query = " ".join(sys.argv[1:])
    asyncio.run(run_agent(user_query))


if __name__ == "__main__":
    main()
