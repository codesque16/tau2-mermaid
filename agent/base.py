"""Abstract base agent; all concrete agents implement this interface."""

from abc import ABC, abstractmethod
import copy
import logging
from typing import Any, Callable, Awaitable, Dict, List, Optional

import json
import shlex
import logfire

from .config import AgentConfig

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Base agent interface. Subclasses must implement _do_respond_stream();
    respond() and respond_stream() are provided in terms of it with Logfire tracing.
    """

    # When False, orchestrator will not use a Live streaming display for this agent
    # (e.g. human agent so terminal input is visible while typing).
    use_streaming_display: bool = True

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        self.name = name
        self.config = config
        self.model = model

        # MCP integration (lazy-initialized)
        self._mcp_initialized: bool = False
        self._mcp_tools: List[dict[str, Any]] = []
        # Map tool_name -> server config used to start a stdio client (or mermaid HTTP config)
        self._mcp_tool_servers: Dict[str, Dict[str, Any]] = {}
        # Map tool_name -> live MCP session for mermaid HTTP (so we reuse session after load_graph)
        self._mcp_tool_sessions: Dict[str, Any] = {}
        # Keep mermaid HTTP context managers alive (list of (http_context, session_context))
        self._mermaid_connections: List[Any] = []
        # System prompt from load_graph (role, rules, SOP); when set, use this + ticket only.
        self._mermaid_system_prompt: Optional[str] = None
        self._tool_docs_by_name: Dict[str, Dict[str, Any]] = {}

    async def respond(self, incoming: str) -> tuple[str, dict]:
        """Non-streaming response. Returns (full_text, usage_info)."""
        return await self.respond_stream(incoming, on_chunk=None)

    async def aclose_mcp(self) -> None:
        """Close mermaid HTTP connections. Call this when done with the agent (e.g. after a run)
        so shutdown does not hit 'exit cancel scope in different task' errors.
        """
        connections = getattr(self, "_mermaid_connections", []) or []
        self._mermaid_connections = []
        self._mcp_tool_sessions = {}
        for http_ctx, session_ctx in connections:
            try:
                if hasattr(session_ctx, "__aexit__"):
                    await session_ctx.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("Ignoring error closing mermaid session context: %s", e)
            try:
                if hasattr(http_ctx, "__aexit__"):
                    await http_ctx.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("Ignoring error closing mermaid http context: %s", e)

    async def respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """
        Stream the response with Logfire tracing; delegates to _do_respond_stream.
        Returns (full_text, usage_info). usage_info: {"usage": {...}, "cost": float}.
        """
        # with logfire.span(
        #     "agent.respond_stream",
        #     agent=self.name,
        #     model=self.model,
        #     _span_name="agent.respond_stream",
        # ) as span:
        #     full_text, usage_info = await self._do_respond_stream(
        #         incoming, on_chunk=on_chunk
        #     )
        #     u = usage_info.get("usage") or {}
        #     span.set_attribute("input_tokens", u.get("input_tokens"))
        #     span.set_attribute("output_tokens", u.get("output_tokens"))
        #     if usage_info.get("cost") is not None:
        #         span.set_attribute("cost_usd", usage_info["cost"])
        #     return full_text, usage_info
        full_text, usage_info = await self._do_respond_stream(
            incoming, on_chunk=on_chunk
        )
        return full_text, usage_info

    @abstractmethod
    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """
        Implement streaming response. Returns (full_text, usage_info).
        usage_info: {"usage": {...}, "cost": float}.
        """
        ...

    # ── MCP helpers (shared across agents) ──────────────────────────────

    def _parse_load_graph_result(self, result: Any) -> Optional[Dict[str, Any]]:
        """Parse load_graph MCP result to a dict; extract from structuredContent or content[].text JSON."""
        if hasattr(result, "structuredContent") and result.structuredContent is not None:
            if isinstance(result.structuredContent, dict):
                return result.structuredContent
            return None
        # Some transports wrap the result
        if hasattr(result, "result") and isinstance(getattr(result, "result"), dict):
            return result.result
        content_list = getattr(result, "content", []) or []
        texts: List[str] = []
        for c in content_list:
            if hasattr(c, "text") and c.text:
                texts.append(c.text)
            elif isinstance(c, dict) and c.get("text"):
                texts.append(c["text"])
        # Try each content block, then joined (in case response is split)
        for text in texts:
            if isinstance(text, str) and text.strip().startswith("{"):
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    pass
        joined = "".join(t for t in texts if isinstance(t, str))
        if joined.strip().startswith("{"):
            try:
                return json.loads(joined)
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    def get_effective_system_prompt(self) -> str:
        """System prompt for the LLM: when mermaid load_graph provided one, use it + config.system_prompt (ticket); else config.system_prompt only."""
        base = getattr(self, "_mermaid_system_prompt", None)
        ticket_part = self.config.system_prompt or ""
        if base:
            return f"{base}\n\n{ticket_part}" if ticket_part.strip() else base
        return ticket_part

    async def _ensure_mcp_initialized(self) -> None:
        """Connect to configured MCP servers (stdio mcps + mermaid HTTP), list tools, and cache schemas.
        For mermaid entries: connect via HTTP, call load_graph(graph), then expose listed tools (excluding load_graph).
        """
        if self._mcp_initialized:
            return

        mermaid_raw = getattr(self.config, "mermaid", None) or []
        # Support both mermaid: [ {...} ] and mermaid: { ... } (single object)
        mermaid_list = (
            mermaid_raw
            if isinstance(mermaid_raw, list)
            else ([mermaid_raw] if isinstance(mermaid_raw, dict) else [])
        )
        mcps = getattr(self.config, "mcps", None) or []
        if not mermaid_list and not mcps:
            self._mcp_initialized = True
            return

        logger.info(
            "Initializing MCP: mermaid=%d, stdio=%d",
            len(mermaid_list),
            len(mcps),
        )
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            self._mcp_initialized = True
            return

        tools: List[dict[str, Any]] = []
        tool_docs_by_name: Dict[str, Dict[str, Any]] = {}
        tools_md_path = (getattr(self.config, "mcp_tools_markdown_path", None) or "").strip()
        if tools_md_path:
            try:
                from .mcp_tools_markdown import parse_tools_markdown

                tool_docs_by_name = parse_tools_markdown(tools_md_path)
            except Exception as e:
                logger.warning("Failed to parse mcp_tools_markdown_path=%r: %s", tools_md_path, e)

        # ── Mermaid MCP(s): HTTP, load_graph once, then expose tools ──
        if mermaid_list:
            try:
                from mcp.client.streamable_http import streamable_http_client
            except ImportError:
                logger.warning("mcp.client.streamable_http not available; skipping mermaid MCP")
            else:
                for entry in mermaid_list:
                    if entry.get("type") != "http":
                        continue
                    url = (entry.get("url") or "").strip()
                    graph = (entry.get("graph") or "").strip()
                    if not url or not graph:
                        logger.warning("mermaid entry missing url or graph, skipping: %s", entry)
                        continue
                    url = url.rstrip("/") + "/mcp" if "/mcp" not in url else url
                    logger.info("Connecting to mermaid MCP at %s, graph=%s", url, graph)
                    allow = set(entry.get("tools") or [])
                    mermaid_cfg = dict(entry)
                    mermaid_cfg["type"] = "http"

                    try:
                        http_ctx = streamable_http_client(url)
                        read_stream, write_stream, _ = await http_ctx.__aenter__()
                        session_ctx = ClientSession(read_stream, write_stream)
                        session = await session_ctx.__aenter__()
                        await session.initialize()
                    except Exception as conn_err:  # ConnectError, ConnectionError, OSError, etc.
                        err_name = type(conn_err).__name__
                        if "Connect" in err_name or "Connection" in err_name or "connect" in str(conn_err).lower():
                            raise RuntimeError(
                                f"MCP connection failed at {url!r}. "
                                "Is the mermaid MCP server running? (e.g. start the server that serves /mcp.) "
                                f"Original: {conn_err}"
                            ) from conn_err
                        raise

                    load_result = await session.call_tool(
                        "load_graph",
                        arguments={"sop_file": graph},
                    )
                    if getattr(load_result, "isError", False):
                        content = getattr(load_result, "content", []) or []
                        err_text = content[0].text if content else str(load_result)
                        raise RuntimeError(f"mermaid load_graph failed: {err_text}")
                    logger.info("mermaid MCP load_graph(%s) succeeded", graph)

                    # Use system_prompt from load_graph (instructions/policy are inside it); ticket is appended by caller.
                    load_data = self._parse_load_graph_result(load_result)
                    system_prompt_val = (
                        load_data.get("system_prompt") if isinstance(load_data, dict) else None
                    ) or (
                        load_data.get("systemPrompt") if isinstance(load_data, dict) else None
                    )
                    if system_prompt_val:
                        self._mermaid_system_prompt = system_prompt_val
                        logger.info("Using system prompt from load_graph (%d chars)", len(self._mermaid_system_prompt))
                    else:
                        # Debug: log what we received so we can fix parsing
                        content_list = getattr(load_result, "content", []) or []
                        first_text = ""
                        for c in content_list:
                            t = getattr(c, "text", None) or (c.get("text") if isinstance(c, dict) else None)
                            if t:
                                first_text = t[:500] if len(str(t)) > 500 else str(t)
                                break
                        logger.warning(
                            "load_graph result missing system_prompt: parsed=%s keys=%s first_text_preview=%s",
                            load_data is not None,
                            list(load_data.keys()) if isinstance(load_data, dict) else None,
                            first_text[:200] if first_text else "(no text)",
                        )

                    tools_result = await session.list_tools()
                    self._mermaid_connections.append((http_ctx, session_ctx))
                    mermaid_tool_names: List[str] = []

                    for t in getattr(tools_result, "tools", []) or []:
                        name = getattr(t, "name", "") or ""
                        if name == "load_graph":
                            continue
                        if allow and name not in allow:
                            continue
                        tool_def = self._mcp_tool_to_openai_format(t, tool_docs_by_name=tool_docs_by_name)
                        tools.append(tool_def)
                        self._mcp_tool_servers[name] = mermaid_cfg
                        self._mcp_tool_sessions[name] = session
                        mermaid_tool_names.append(name)
                    logger.info("mermaid MCP tools registered: %s", mermaid_tool_names)

        # ── Stdio MCP(s): reconnect per call ──
        for server_cfg in mcps:
            cmd_str = (
                server_cfg.get("command")
                or server_cfg.get("commad")  # tolerate typo
                or ""
            ).strip()
            if not cmd_str:
                continue

            parts = shlex.split(cmd_str)
            if not parts:
                continue
            cmd, *args = parts

            params = StdioServerParameters(command=cmd, args=args)

            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()

            allow = set(server_cfg.get("tools") or [])

            for t in getattr(tools_result, "tools", []) or []:
                name = getattr(t, "name", "") or ""
                if allow and name not in allow:
                    continue
                tool_def = self._mcp_tool_to_openai_format(t, tool_docs_by_name=tool_docs_by_name)
                tools.append(tool_def)
                self._mcp_tool_servers[name] = server_cfg

        self._mcp_tools = tools
        self._tool_docs_by_name = tool_docs_by_name
        self._mcp_initialized = True

    def _mcp_tool_to_openai_format(
        self,
        tool_obj: Any,
        *,
        tool_docs_by_name: Dict[str, Dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Convert a single MCP tool descriptor to OpenAI/litellm `tools` format.

        Function/argument descriptions can be enriched from ``mcp_tools_markdown_path``.
        """
        _HIDDEN_PARAMS = {"session_id", "ctx"}

        name = getattr(tool_obj, "name", "") or ""
        input_schema = getattr(tool_obj, "inputSchema", None) or {
            "type": "object",
            "properties": {},
        }

        if isinstance(input_schema, dict):
            props = input_schema.get("properties") or {}
            required = input_schema.get("required") or []
            input_schema = {
                **input_schema,
                "properties": {
                    k: v for k, v in props.items() if k not in _HIDDEN_PARAMS
                },
                "required": [r for r in required if r not in _HIDDEN_PARAMS],
            }

        tool_docs_by_name = tool_docs_by_name or {}
        doc = tool_docs_by_name.get(name) or {}
        description = str(doc.get("description") or "").strip()
        arg_docs = doc.get("args") if isinstance(doc.get("args"), dict) else {}
        if isinstance(input_schema, dict) and isinstance(input_schema.get("properties"), dict):
            for p_name, p_schema in input_schema["properties"].items():
                if not isinstance(p_schema, dict):
                    continue
                arg_desc = str(arg_docs.get(p_name) or "").strip()
                if not arg_desc:
                    continue
                updated = dict(p_schema)
                updated["description"] = arg_desc
                input_schema["properties"][p_name] = updated

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": input_schema,
            },
        }

    def _format_mcp_result(self, result: Any) -> str:
        """Format MCP call_tool result as string."""
        if getattr(result, "isError", False):
            parts: List[str] = []
            for c in getattr(result, "content", []) or []:
                if hasattr(c, "text") and c.text:
                    parts.append(c.text)
            return json.dumps({"error": " ".join(parts) or "MCP tool error"})
        if hasattr(result, "structuredContent") and result.structuredContent:
            return json.dumps(result.structuredContent)
        texts: List[str] = []
        for c in getattr(result, "content", []) or []:
            if hasattr(c, "text") and c.text:
                texts.append(c.text)
        return "\n".join(texts) if texts else "{}"

    async def _call_mcp_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Execute one MCP tool call and return a text/JSON string."""
        if not self._mcp_initialized:
            await self._ensure_mcp_initialized()

        server_cfg = self._mcp_tool_servers.get(name)
        if server_cfg is None:
            return json.dumps({"error": f"Unknown MCP tool: {name}"})

        # Mermaid HTTP: use the existing session (load_graph already called at init)
        if server_cfg.get("type") == "http":
            session = self._mcp_tool_sessions.get(name)
            if session is None:
                return json.dumps({"error": f"No session for mermaid tool: {name}"})
            result = await session.call_tool(name, arguments=arguments or {})
            return self._format_mcp_result(result)

        # Stdio MCP: reconnect per call
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            return json.dumps({"error": "MCP SDK not installed"})

        cmd_str = (
            server_cfg.get("command")
            or server_cfg.get("commad")
            or ""
        ).strip()
        if not cmd_str:
            return json.dumps({"error": "No command configured for MCP server"})

        parts = shlex.split(cmd_str)
        if not parts:
            return json.dumps({"error": "Invalid MCP command configuration"})
        cmd, *args = parts

        params = StdioServerParameters(command=cmd, args=args)

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments=arguments or {})

        return self._format_mcp_result(result)

    def _get_mcp_tools_for_llm(self) -> List[dict[str, Any]]:
        """Return the OpenAI-format tools list for the current agent."""
        return [copy.deepcopy(t) for t in (self._mcp_tools or [])]

    def log_llm_tools_in_request(
        self,
        tools: List[dict[str, Any]],
        *,
        provider: str,
        model: str,
    ) -> None:
        """Emit the exact tool definitions sent on the next LLM request (Logfire)."""
        if not tools:
            return
        names = [
            (t.get("function") or {}).get("name")
            for t in tools
            if isinstance(t, dict) and t.get("type") == "function"
        ]
        logfire.info(
            "llm_request_tools",
            agent_name=self.name,
            provider=provider,
            model=model,
            tool_count=len(tools),
            tool_names=names,
            tools_json=json.dumps(tools, default=str, ensure_ascii=False),
        )
