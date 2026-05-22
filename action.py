"""
action.py — Axiom Learning OS Session 6
Action layer: MCP stdio transport, defensive argument validation,
and 4 KB size-threshold artifact triage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from schemas import ToolCall

logger = logging.getLogger(__name__)

STATE_DIR = Path("state")
ARTIFACTS_DIR = STATE_DIR / "artifacts"

# Threshold in bytes for artifact triage
_ARTIFACT_THRESHOLD_BYTES = 4096

# Regex for artifact handle strings produced by a previous triage
_HANDLE_PATTERN = re.compile(r"^art:[a-f0-9]{64}$")


# ---------------------------------------------------------------------------
# MCP session lifecycle
# ---------------------------------------------------------------------------

async def create_mcp_session() -> tuple[ClientSession, Any]:
    """
    Start an MCP stdio client connected to mcp_server.py.

    Returns the (session, exit_stack) tuple.  The caller is responsible for
    keeping the exit_stack alive while the session is in use and calling
    aclose() when done.
    """
    from contextlib import AsyncExitStack

    import os
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    server_params = StdioServerParameters(
        command="python",
        args=["mcp_server.py"],
        env=env,
    )

    exit_stack = AsyncExitStack()
    read_stream, write_stream = await exit_stack.enter_async_context(
        stdio_client(server_params)
    )
    session: ClientSession = await exit_stack.enter_async_context(
        ClientSession(read_stream, write_stream)
    )
    await session.initialize()
    logger.info("MCP session initialized successfully.")
    return session, exit_stack


async def list_mcp_tools(session: ClientSession) -> list[dict[str, Any]]:
    """Return the list of tool definitions exposed by the MCP server."""
    result = await session.list_tools()
    tools = []
    for tool in result.tools:
        tools.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema if tool.inputSchema else {},
            }
        )
    logger.info("Listed %d MCP tools.", len(tools))
    return tools


# ---------------------------------------------------------------------------
# Defensive argument checker
# ---------------------------------------------------------------------------

def _arguments_contain_handle(arguments: dict[str, Any]) -> bool:
    """
    Return True if any argument value is a raw artifact handle string
    (e.g., "art:<64-char-hex>") instead of actual content.
    """
    for value in arguments.values():
        if isinstance(value, str) and _HANDLE_PATTERN.match(value.strip()):
            return True
    return False


def _validate_required_args(
    tool_name: str,
    arguments: dict[str, Any],
    tool_schemas: list[dict[str, Any]] | None,
) -> str | None:
    """
    Check that all required arguments declared in the tool's inputSchema are
    present and non-empty in the supplied arguments dict.

    Returns an error instruction string if validation fails, or None if OK.
    """
    if not tool_schemas:
        return None

    schema: dict[str, Any] | None = None
    for entry in tool_schemas:
        if entry.get("name") == tool_name:
            schema = entry.get("inputSchema", {})
            break

    if not schema:
        return None  # Unknown tool — let MCP handle it

    required_fields: list[str] = schema.get("required", [])
    missing: list[str] = []
    for field in required_fields:
        value = arguments.get(field)
        # Treat None, empty string, and missing key as absent
        if value is None or value == "" or field not in arguments:
            missing.append(field)

    if missing:
        props = schema.get("properties", {})
        hints = ", ".join(
            f'"{f}" ({props[f].get("description", props[f].get("type", "value"))})'
            if f in props else f'"{f}"'
            for f in missing
        )
        instruction = (
            f"[ACTION INTERCEPTED] Tool call '{tool_name}' was rejected: "
            f"the following required argument(s) are missing or empty: {hints}. "
            f"Re-read the ACTIVE GOAL text, extract the concrete values, "
            f"and populate all required arguments before calling the tool again."
        )
        logger.warning(
            "Required args missing for tool '%s': %s", tool_name, missing
        )
        return instruction

    return None


# ---------------------------------------------------------------------------
# Artifact triage helpers
# ---------------------------------------------------------------------------

def _compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_artifact(payload: str, origin_tool: str) -> str:
    """
    Persist a large payload to disk and return a lightweight reference descriptor.

    Artifacts are intentionally NOT registered in memory.json — they are
    session-scoped (wiped on next boot) and are already discoverable via the
    'art:<hash>' handle that gets embedded in the agent history string.
    """
    raw_bytes = payload.encode("utf-8")
    hash_val = _compute_sha256(raw_bytes)
    artifact_id = f"art:{hash_val}"

    bin_path = ARTIFACTS_DIR / f"{hash_val}.bin"
    meta_path = ARTIFACTS_DIR / f"{hash_val}.json"

    # Write binary payload
    bin_path.write_bytes(raw_bytes)

    # Write metadata sidecar
    meta = {"size_bytes": len(raw_bytes), "origin_tool": origin_tool}
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    preview = payload[:200]
    descriptor = (
        f"Executed tool successfully. Large payload cached to handle: {artifact_id}. "
        f"Preview: [{preview}...]"
    )
    logger.info("Artifact saved: %s (%d bytes)", artifact_id, len(raw_bytes))
    return descriptor



# ---------------------------------------------------------------------------
# Public dispatch interface
# ---------------------------------------------------------------------------

async def dispatch_tool_call(
    session: ClientSession,
    tool_call: ToolCall,
    tool_schemas: list[dict[str, Any]] | None = None,  # for required-arg validation
    memory_append_fn: Any = None,
) -> str:
    """
    Validate, dispatch, and triage a tool call through the MCP session.

    Returns a string descriptor to be appended to the agent's history.
    """
    tool_name = tool_call.name
    arguments = tool_call.arguments

    logger.info("Dispatching tool: %s with args: %s", tool_name, arguments)

    # ------------------------------------------------------------------
    # Defensive check 1: reject calls that pass artifact handle strings
    # ------------------------------------------------------------------
    if _arguments_contain_handle(arguments):
        instruction = (
            f"[ACTION INTERCEPTED] Tool call '{tool_name}' was rejected because one "
            "or more arguments contained a raw artifact handle string instead of the "
            "actual content. Please read the [ATTACHED ARTIFACT CONTENT] block that "
            "was mounted in the previous decision step and use its text directly as "
            "the argument value."
        )
        logger.warning("Intercepted handle-string argument in tool call '%s'.", tool_name)
        return instruction

    # ------------------------------------------------------------------
    # Defensive check 2: validate required arguments against tool schema
    # ------------------------------------------------------------------
    validation_error = _validate_required_args(tool_name, arguments, tool_schemas)
    if validation_error is not None:
        return validation_error

    # ------------------------------------------------------------------
    # Execute via MCP
    # ------------------------------------------------------------------
    try:
        result = await session.call_tool(tool_name, arguments=arguments)
    except Exception as exc:  # noqa: BLE001
        error_descriptor = f"[TOOL ERROR] {tool_name} raised an exception: {exc}"
        logger.error("MCP tool call failed: %s", exc)
        return error_descriptor

    # Extract text payload from MCP result
    payload = _extract_mcp_payload(result)

    # ------------------------------------------------------------------
    # 4 KB size-threshold triage
    # ------------------------------------------------------------------
    payload_bytes = payload.encode("utf-8")
    if len(payload_bytes) <= _ARTIFACT_THRESHOLD_BYTES:
        descriptor = f"Tool '{tool_name}' returned: {payload}"
        logger.debug("Small payload (%d bytes) returned inline.", len(payload_bytes))
        return descriptor
    else:
        # Persist to artifacts and register in memory
        descriptor = _write_artifact(payload, origin_tool=tool_name)
        return descriptor


# ---------------------------------------------------------------------------
# MCP result extraction
# ---------------------------------------------------------------------------

def _extract_mcp_payload(result: Any) -> str:
    """
    Extract the text payload from an MCP CallToolResult.

    Handles both text content blocks and raw response shapes.
    """
    # MCP SDK result has a .content list of content blocks
    if hasattr(result, "content"):
        parts: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                # Binary blob — convert to hex preview
                parts.append(f"[binary data: {len(block.data)} bytes]")
            else:
                parts.append(str(block))
        return "\n".join(parts)

    # Fallback: stringify whatever we have
    return str(result)
