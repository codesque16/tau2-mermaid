"""Gemini-backed agent; same interface as BaseAgent (respond_stream returns (text, usage_info))."""

import asyncio
import base64
import json
import os
from typing import Any, Callable, Awaitable

import logfire

from .base import BaseAgent
from .config import AgentConfig
from .gemini_log import log_gemini_generate_io, log_openai_chat_raw_io, to_jsonable
from .logfire_gemini_integration import (
    reset_current_gemini_tool_round,
    set_current_gemini_tool_round,
)
from .utils.cost import compute_cost, usage_from_gemini_response, usage_from_openai_response

# Gemini API roles for `contents` (see https://ai.google.dev/gemini-api/docs/function-calling ).
GEMINI_ROLE_USER = "user"
GEMINI_ROLE_MODEL = "model"
GEMINI_ROLE_FUNCTION = "function"  # tool / function_call results (not OpenAI's "tool" role)


def _supports_gemini_thinking_level(model: str) -> bool:
    """Return whether `thinking_level` should be sent for this model."""
    m = (model or "").strip().lower()
    # Gemini 2.5 Flash Lite rejects `thinking_level` today (400 INVALID_ARGUMENT).
    if "gemini-2.5-flash-lite" in m or "gemini-2.5-flash" in m:
        return False
    return True


def _gemini_thinking_budget_from_effort(effort: str | None) -> int | None:
    if effort is None or not str(effort).strip():
        return None
    lvl = str(effort).strip().lower()
    # Conservative defaults for 2.5 flash-lite budget-based thinking.
    budget_map = {
        "minimal": 0,
        "low": 1024,
        "medium": 4096,
        "high": 8192,
    }
    return budget_map.get(lvl)


def _gemini_api_seed_i32(raw: int) -> int:
    """``GenerateContentConfig.seed`` must be signed INT32; large YAML ints (e.g. ``evaluation_seed``) must fold."""
    x = int(raw) % (2**32)
    if x >= 2**31:
        x -= 2**32
    return x


def _content_to_text(content: Any) -> str:
    """Extract visible assistant text; skip thought parts (``part.thought`` is true)."""
    if content is None:
        return ""
    parts = getattr(content, "parts", None) or []
    out: list[str] = []
    for p in parts:
        if getattr(p, "thought", None):
            continue
        t = getattr(p, "text", None)
        if isinstance(t, str) and t:
            out.append(t)
    return "".join(out)


def _thought_text_from_content(content: Any) -> str:
    """Concatenate thought-summary text from parts where ``thought`` is true (needs ``include_thoughts``)."""
    if content is None:
        return ""
    parts = getattr(content, "parts", None) or []
    chunks: list[str] = []
    for p in parts:
        if not getattr(p, "thought", None):
            continue
        t = getattr(p, "text", None)
        if isinstance(t, str) and t.strip():
            chunks.append(t.strip())
    return "\n\n".join(chunks) if chunks else ""


def _thought_text_from_response(response: Any) -> str:
    cands = getattr(response, "candidates", None) or []
    if not cands:
        return ""
    return _thought_text_from_content(getattr(cands[0], "content", None))


def _thought_signature_for_history(sig: Any) -> str | None:
    """Serialize Part.thought_signature (opaque bytes) for JSON-safe tool_call records."""
    if sig is None:
        return None
    if isinstance(sig, bytes):
        return base64.standard_b64encode(sig).decode("ascii")
    if isinstance(sig, str):
        return sig
    return None


def _thought_signature_from_history(stored: Any) -> bytes | None:
    """Restore bytes for types.Part(thought_signature=...) when replaying tool turns."""
    if stored is None:
        return None
    if isinstance(stored, bytes):
        return stored
    if isinstance(stored, str) and stored.strip():
        return base64.standard_b64decode(stored.encode("ascii"))
    return None


async def _with_retry_gemini(
    agent: "GeminiAgent",
    generate_fn: Callable[[], tuple[str, Any]],
    max_attempts: int = 6,
) -> tuple[str, Any]:
    """Run generate in a thread; retry on transient errors and API key rotation (429 / backup fail)."""
    from agent.api_key_rotation import maybe_rotate_after_provider_error

    last_err: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await asyncio.to_thread(generate_fn)
        except Exception as e:
            last_err = e
            inv = lambda: setattr(agent, "_client", None)
            rotated = maybe_rotate_after_provider_error(
                "gemini", e, invalidate_client=inv
            )
            if rotated or attempt < max_attempts - 1:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
            raise
    raise last_err  # type: ignore[misc]


class GeminiAgent(BaseAgent):
    """Gemini-backed agent (chat). Same interface as BaseAgent."""

    def __init__(self, name: str, config: AgentConfig, model: str) -> None:
        super().__init__(name=name, config=config, model=model)
        self._client: Any = None
        self.history: list[dict[str, Any]] = []

    def _get_client(self):
        from google import genai

        from agent.api_key_rotation import get_gemini_api_key

        use_vertex_ai = bool(getattr(self.config, "vertex_ai", False))
        scope = (self.name or "assistant").strip().lower() or "assistant"
        api_key = get_gemini_api_key(scope)
        if not api_key.strip():
            raise ValueError("Set GOOGLE_API_KEY or GEMINI_API_KEY for Gemini models.")
        client_cache_key = f"vertexai:{int(use_vertex_ai)}|name:{scope}|api_key:{api_key.strip()}"
        if self._client is None or getattr(self, "_gemini_client_key", None) != client_cache_key:
            self._client = genai.Client(
                vertexai=use_vertex_ai,
                api_key=api_key.strip(),
            )
            self._gemini_client_key = client_cache_key
        return self._client

    def _is_vertex_endpoint_mode(self) -> bool:
        return bool((getattr(self.config, "vertex_endpoint_id", None) or "").strip())

    def _vertex_endpoint_predict(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        phase: str,
    ) -> dict[str, Any]:
        import google.auth
        from google.auth.transport.requests import Request

        endpoint_id = (getattr(self.config, "vertex_endpoint_id", None) or "").strip()
        if not endpoint_id:
            raise ValueError("assistant.vertex_endpoint_id is required for dedicated Vertex endpoint mode.")
        project = (
            (getattr(self.config, "vertex_project", None) or "").strip()
            or (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
        )
        if not project:
            raise ValueError("Set assistant.vertex_project or GOOGLE_CLOUD_PROJECT for dedicated Vertex endpoint mode.")
        location = (getattr(self.config, "vertex_location", None) or "").strip() or "us-central1"

        base = (getattr(self.config, "vertex_http_predict_base", None) or "").strip().rstrip("/")
        if not base:
            dedicated_domain = (os.getenv("DEDICATED_ENDPOINT_DOMAIN") or "").strip()
            if dedicated_domain:
                base = f"https://{dedicated_domain}"
        if not base:
            # Console "sample request" style host (see scripts/test_vertex_openai.py).
            base = f"https://{endpoint_id}.{location}-{project}.prediction.vertexai.goog"

        params = getattr(self.config, "vertex_endpoint_parameters", None) or {}
        if not isinstance(params, dict):
            raise ValueError("assistant.vertex_endpoint_parameters must be a dict/object when provided.")

        api_ver = (getattr(self.config, "vertex_http_predict_api_version", None) or "v1").strip().strip(
            "/"
        )
        if not api_ver:
            api_ver = "v1"

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())
        token = getattr(creds, "token", None) or ""
        if not token:
            raise RuntimeError("Failed to obtain Google ADC token for dedicated Vertex endpoint mode.")

        instance: dict[str, Any] = {
            "@requestFormat": "chatCompletions",
            "messages": messages,
            "temperature": float(getattr(self.config, "temperature", 0.0) or 0.0),
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if self.config.max_tokens is not None:
            instance["max_tokens"] = int(self.config.max_tokens)
        for k, v in dict(params).items():
            if k not in ("messages", "@requestFormat"):
                instance[k] = v
        if tools:
            instance["tools"] = tools
            # tool_choice semantics (vLLM / OpenAI-compatible servers on Vertex):
            # - **Omitting** the `tool_choice` field is NOT the same as sending `"tool_choice":"auto"`.
            #   Many stacks only enforce --enable-auto-tool-choice when the **string** "auto" appears.
            # - scripts/test_vertex_openai.py call_chat(..., tool_choice=None) **drops the key** from JSON.
            # - If the agent ever merged `"auto"` here (or via vertex_endpoint_parameters), the same
            #   URL returns predictions with object=error and message about --enable-auto-tool-choice.
            # - To match the test script: do not set tool_choice unless YAML sets it explicitly.

        url = (
            f"{base}/{api_ver}/projects/{project}/locations/{location}/endpoints/"
            f"{endpoint_id}:predict"
        )
        body = {"instances": [instance]}

        from agent.vertex_dedicated_http import vertex_predict_post
        from agent.logfire_native_llm import _finalize_openai_span

        _tc_in_body = "tool_choice" in instance
        _tc_val = instance.get("tool_choice") if _tc_in_body else None

        request_extras: dict[str, Any] = {
            "url": url,
            # Keep tools visible in Logfire "Model Run" request_data.
        }
        if tools is not None:
            request_extras["tools"] = tools
        # Exact POST instance keys / tool_choice (compare to a working test_vertex_openai call).
        request_extras["vertex_instance_keys"] = sorted(instance.keys())
        request_extras["vertex_body_has_tool_choice"] = _tc_in_body
        request_extras["vertex_body_tool_choice"] = _tc_val

        model_for_log = f"vertex-endpoint:{endpoint_id}"
        # Create the same Logfire "Model Run" structure as OpenAI chat.completions,
        # so vertex :predict calls appear with request_data/response_data consistently.
        payload = vertex_predict_post(url, token, body, timeout_s=120)

        # Shape payload.predictions as an OpenAI-compatible "completion" object for Logfire UI
        # (match _vertex_prediction_obj behavior, but without raising).
        completion_for_log: Any = payload.get("predictions")
        if isinstance(completion_for_log, str):
            try:
                completion_for_log = json.loads(completion_for_log)
            except json.JSONDecodeError:
                pass
        if isinstance(completion_for_log, list) and completion_for_log:
            first = completion_for_log[0]
            if isinstance(first, str):
                try:
                    first = json.loads(first)
                except json.JSONDecodeError:
                    pass
            completion_for_log = first
        if completion_for_log is None:
            completion_for_log = payload

        with logfire.span(
            "vertex chat.completions",
            agent=self.name,
            model=model_for_log,
            **{
                "gen_ai.system": "vertex",
                "gen_ai.request.model": model_for_log,
                "gen_ai.operation.name": "chat",
            },
        ) as span:
            # Best-effort: even if the endpoint returns {"object":"error"}, we still want
            # request/response structure in Logfire UI.
            _finalize_openai_span(
                span,
                model=model_for_log,
                request_messages=messages,
                request_extras=request_extras,
                completion=completion_for_log,
                api_key_masked=None,
                io_phase=phase,
            )

        return payload

    @staticmethod
    def _vertex_format_vertex_prediction_error(msg: str | None) -> str:
        """Append deployment hint when vLLM complains about auto tool choice."""
        base = str(msg or "")
        if "enable-auto-tool-choice" in base or "tool-call-parser" in base:
            return (
                base
                + " — The client is not sending tool_choice (see Logfire vertex_body_has_tool_choice). "
                "Many OpenAI-compatible servers still use internal tool_choice=auto whenever "
                "`tools` is non-empty, which requires --enable-auto-tool-choice and "
                "--tool-call-parser on the **model worker**. Fix: add those flags to the serving "
                "deployment (Vertex custom container / vLLM args), or use an endpoint where "
                "they are already enabled (your standalone test may hit a different revision)."
            )
        return base

    @staticmethod
    def _vertex_prediction_obj(payload: dict[str, Any]) -> dict[str, Any]:
        preds = payload.get("predictions")
        if preds is None:
            return {}
        if isinstance(preds, str):
            try:
                preds = json.loads(preds)
            except json.JSONDecodeError:
                return {}
        if isinstance(preds, dict):
            if preds.get("object") == "error":
                m = preds.get("message")
                raise RuntimeError(
                    "Vertex endpoint returned error payload: "
                    f"code={preds.get('code')} type={preds.get('type')} "
                    f"message={GeminiAgent._vertex_format_vertex_prediction_error(m if isinstance(m, str) else None)}"
                )
            return preds
        if isinstance(preds, list) and preds:
            first = preds[0]
            if isinstance(first, str):
                try:
                    first = json.loads(first)
                except json.JSONDecodeError:
                    return {}
            if isinstance(first, dict):
                if first.get("object") == "error":
                    m = first.get("message")
                    raise RuntimeError(
                        "Vertex endpoint returned error payload: "
                        f"code={first.get('code')} type={first.get('type')} "
                        f"message={GeminiAgent._vertex_format_vertex_prediction_error(m if isinstance(m, str) else None)}"
                    )
                return first
        return {}

    @staticmethod
    def _vertex_chat_text(pred: dict[str, Any]) -> str:
        choices = pred.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        msg = c0.get("message") if isinstance(c0, dict) else {}
        if not isinstance(msg, dict):
            return ""
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict):
                    t = p.get("text")
                    if isinstance(t, str) and t:
                        parts.append(t)
            return "".join(parts)
        return ""

    @staticmethod
    def _vertex_chat_tool_calls(pred: dict[str, Any]) -> list[dict[str, Any]]:
        choices = pred.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        msg = c0.get("message") if isinstance(c0, dict) else {}
        if not isinstance(msg, dict):
            return []
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            return []
        out: list[dict[str, Any]] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = fn.get("name") if isinstance(fn.get("name"), str) else ""
            args_raw = fn.get("arguments")
            args: dict[str, Any] = {}
            if isinstance(args_raw, str):
                try:
                    parsed = json.loads(args_raw)
                    if isinstance(parsed, dict):
                        args = parsed
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(args_raw, dict):
                args = args_raw
            if not name:
                continue
            out.append(
                {
                    "id": str(tc.get("id") or ""),
                    "name": name,
                    "arguments": args,
                }
            )
        return out

    def _vertex_messages_from_history(self) -> list[dict[str, Any]]:
        """OpenAI chat messages for @requestFormat chatCompletions (see scripts/test_vertex_openai.py)."""
        messages: list[dict[str, Any]] = []
        system = (self.get_effective_system_prompt() or "").strip()
        if system:
            messages.append({"role": "system", "content": system})
        for m in self.history:
            role = m.get("role")
            if role == "user":
                messages.append({"role": "user", "content": str(m.get("content") or "")})
                continue
            if role == "assistant":
                raw_content = m.get("content")
                if raw_content is None:
                    text = ""
                else:
                    text = raw_content if isinstance(raw_content, str) else str(raw_content)
                msg: dict[str, Any] = {"role": "assistant", "content": text}
                api_calls: list[dict[str, Any]] = []
                tcs = m.get("tool_calls") or []
                if isinstance(tcs, list) and tcs:
                    for tc in tcs:
                        if not isinstance(tc, dict):
                            continue
                        name = str(tc.get("name") or "").strip()
                        if not name:
                            continue
                        args = tc.get("arguments")
                        if isinstance(args, dict):
                            args_s = json.dumps(args)
                        else:
                            args_s = str(args or "{}")
                        api_calls.append(
                            {
                                "id": str(tc.get("id") or ""),
                                "type": "function",
                                "function": {"name": name, "arguments": args_s},
                            }
                        )
                    if api_calls:
                        msg["tool_calls"] = api_calls
                messages.append(msg)
                continue
            if role == "tool":
                tc_id = str(m.get("tool_call_id") or "")
                raw = m.get("content")
                if raw is None:
                    body = ""
                elif isinstance(raw, (dict, list)):
                    body = json.dumps(raw)
                else:
                    body = str(raw)
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": body})
        return messages

    async def _do_respond_stream(
        self,
        incoming: str,
        *,
        on_chunk: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> tuple[str, dict]:
        """Generate reply via Gemini. Returns (full_text, usage_info). Calls on_chunk with full text when done."""
        if self._is_vertex_endpoint_mode():
            await self._ensure_mcp_initialized()
            self.history.append({"role": "user", "content": incoming})

            tools = self._get_mcp_tools_for_llm()
            if tools:
                self.log_llm_tools_in_request(tools, provider="vertex", model=self.model)
            total_usage: dict[str, int] = {}
            total_cost = 0.0
            max_tool_rounds = 20
            final_text = ""

            for _round in range(max_tool_rounds):
                api_messages = self._vertex_messages_from_history()
                payload = await asyncio.to_thread(
                    self._vertex_endpoint_predict,
                    messages=api_messages,
                    tools=tools or None,
                    phase=f"vertex_endpoint_chat_round_{_round}",
                )
                pred = self._vertex_prediction_obj(payload)
                usage = usage_from_openai_response(pred.get("usage") if isinstance(pred, dict) else None)
                if usage:
                    total_usage = {
                        k: int(total_usage.get(k, 0)) + int(usage.get(k, 0))
                        for k in set(total_usage) | set(usage)
                    }
                    total_cost += compute_cost(self.model, usage)

                content = self._vertex_chat_text(pred).strip()
                tool_calls = self._vertex_chat_tool_calls(pred)
                assistant_record: dict[str, Any] = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_record["tool_calls"] = tool_calls
                self.history.append(assistant_record)

                if not tool_calls:
                    final_text = content
                    if on_chunk is not None:
                        await on_chunk("text", final_text)
                    return final_text, {"usage": total_usage, "cost": total_cost}

                for tc in tool_calls:
                    tc_id = tc.get("id") or ""
                    fn_name = tc.get("name") or ""
                    args = tc.get("arguments") or {}
                    if on_chunk is not None:
                        await on_chunk("tool_use", {"name": fn_name, "id": tc_id, "input": args})
                    result = await self._call_mcp_tool(fn_name, args)
                    self.history.append(
                        {
                            "role": "tool",
                            "name": fn_name,
                            "content": result,
                            "tool_call_id": tc_id,
                        }
                    )

            if on_chunk is not None and final_text:
                await on_chunk("text", final_text)
            return final_text, {"usage": total_usage, "cost": total_cost}

        # In solo mode (retail), tools are required. We mimic LiteLLM's approach:
        # 1) initialize MCP tool schemas via BaseAgent
        # 2) pass tool declarations to Gemini
        # 3) parse model-emitted function_call parts
        # 4) execute MCP tool calls and feed tool results back until final text

        await self._ensure_mcp_initialized()

        def _openai_tools_to_gemini_tool(openai_tools: list[dict[str, Any]]):
            from google.genai import types

            fn_decls: list[Any] = []
            for t in openai_tools:
                if t.get("type") != "function":
                    continue
                fn = t.get("function") or {}
                name = fn.get("name") or ""
                if not name:
                    continue
                params = fn.get("parameters") or {"type": "object", "properties": {}}
                json_schema = types.JSONSchema.model_validate(params)
                schema = types.Schema.from_json_schema(json_schema=json_schema)
                # Reuse the same tool description populated in BaseAgent/OpenAI path.
                fn_decls.append(
                    types.FunctionDeclaration(
                        name=name,
                        description=str(fn.get("description") or ""),
                        parameters=schema,
                    )
                )
            if not fn_decls:
                return None
            return [types.Tool(function_declarations=fn_decls)]

        def _tool_output_to_response_dict(raw: Any) -> dict[str, Any]:
            """Build FunctionResponse.response dict from MCP JSON/text string."""
            if raw is None:
                return {"result": ""}
            s = str(raw).strip()
            if not s:
                return {"result": ""}
            if s.startswith("{") or s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, dict):
                        return parsed
                    return {"result": parsed}
                except json.JSONDecodeError:
                    pass
            return {"result": s}

        def _tool_name_for_history_index(hist: list[dict[str, Any]], idx: int) -> str:
            """Resolve function name for a tool message (prefers `name` field)."""
            t = hist[idx]
            n = (t.get("name") or "").strip()
            if n:
                return n
            tc_id = t.get("tool_call_id")
            for j in range(idx - 1, -1, -1):
                if hist[j].get("role") != "assistant":
                    continue
                for tc in hist[j].get("tool_calls") or []:
                    if tc.get("id") == tc_id:
                        return str(tc.get("name") or "")
            return ""

        def _history_to_gemini_contents():
            """Map OpenAI-style history to Gemini contents: model=function_call, function=function_response."""
            from google.genai import types

            contents: list[Any] = []
            hist = self.history
            i = 0
            while i < len(hist):
                m = hist[i]
                role = m.get("role")

                if role == "user":
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[types.Part(text=m.get("content") or "")],
                        )
                    )
                    i += 1
                    continue

                if role == "assistant":
                    tool_calls = m.get("tool_calls") or []
                    text = (m.get("content") or "").strip()
                    parts: list[Any] = []
                    reasoning = (
                        m.get("reasoning_content") or m.get("thought") or ""
                    )
                    if isinstance(reasoning, str) and reasoning.strip():
                        parts.append(
                            types.Part(text=reasoning.strip(), thought=True)
                        )
                    if text:
                        parts.append(types.Part(text=text))
                    for tc in tool_calls:
                        fn = str(tc.get("name") or "").strip()
                        args = tc.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args) if args.strip() else {}
                            except json.JSONDecodeError:
                                args = {}
                        if not isinstance(args, dict):
                            args = {}
                        tid = tc.get("id")
                        tid_s = str(tid).strip() if tid is not None else None
                        fc = types.FunctionCall(
                            name=fn,
                            args=dict(args),
                            id=tid_s or None,
                        )
                        sig = _thought_signature_from_history(
                            tc.get("thought_signature")
                        )
                        part_kw: dict[str, Any] = {"function_call": fc}
                        if sig is not None:
                            part_kw["thought_signature"] = sig
                        parts.append(types.Part(**part_kw))
                    if not parts:
                        parts.append(types.Part(text=""))
                    contents.append(types.Content(role="model", parts=parts))
                    i += 1
                    continue

                if role == "tool":
                    fr_parts: list[Any] = []
                    while i < len(hist) and hist[i].get("role") == "tool":
                        t = hist[i]
                        fname = _tool_name_for_history_index(hist, i)
                        if not fname:
                            fname = "unknown_tool"
                        tid = t.get("tool_call_id")
                        tid_s = str(tid).strip() if tid is not None else None
                        fr_parts.append(
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=fname,
                                    id=tid_s or None,
                                    response=_tool_output_to_response_dict(
                                        t.get("content")
                                    ),
                                )
                            )
                        )
                        i += 1
                    contents.append(
                        types.Content(role=GEMINI_ROLE_FUNCTION, parts=fr_parts)
                    )
                    continue

                i += 1

            return contents

        def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
            tool_calls: list[dict[str, Any]] = []
            candidates = getattr(response, "candidates", None) or []
            if not candidates:
                return tool_calls
            content = candidates[0].content
            parts = getattr(content, "parts", None) or []
            for p in parts:
                fc = getattr(p, "function_call", None)
                if not fc:
                    continue
                tool_calls.append(
                    {
                        "id": getattr(fc, "id", None) or "",
                        "name": getattr(fc, "name", None) or "",
                        "arguments": getattr(fc, "args", None) or {},
                        # Required when using thinking + tools; must be echoed on replay.
                        "thought_signature": getattr(p, "thought_signature", None),
                    }
                )
            return tool_calls

        def _extract_text(response: Any) -> str:
            candidates = getattr(response, "candidates", None) or []
            if not candidates:
                return ""
            return _content_to_text(candidates[0].content)

        openai_tools = self._get_mcp_tools_for_llm()
        if openai_tools:
            self.log_llm_tools_in_request(
                openai_tools, provider="gemini", model=self.model
            )
        gemini_tools = _openai_tools_to_gemini_tool(openai_tools) if openai_tools else None

        # Store user message in our OpenAI-compatible history structure so evaluators can replay tool calls.
        self.history.append({"role": "user", "content": incoming})

        max_tool_rounds = 20
        final_text = ""
        total_usage: dict[str, int] = {}
        total_cost = 0.0

        for _round in range(max_tool_rounds):
            def _generate() -> tuple[str, str, Any, list[dict[str, Any]], dict[str, Any]]:
                from google.genai import types

                client = self._get_client()
                from agent.api_key_rotation import mask_secret

                raw_key = getattr(self, "_gemini_client_key", "") or ""
                contents = _history_to_gemini_contents()

                gen_config_kw: dict = {
                    "system_instruction": self.get_effective_system_prompt(),
                    "temperature": self.config.temperature,
                }
                reff = getattr(self.config, "reasoning_effort", None)
                if reff is not None and str(reff).strip():
                    if _supports_gemini_thinking_level(self.model):
                        lvl = str(reff).strip().lower()
                        level_map = {
                            "low": types.ThinkingLevel.LOW,
                            "medium": types.ThinkingLevel.MEDIUM,
                            "high": types.ThinkingLevel.HIGH,
                            "minimal": types.ThinkingLevel.MINIMAL,
                        }
                        tl = level_map.get(lvl)
                        if tl is not None:
                            # ``include_thoughts`` returns summaries in ``Part(text=..., thought=True)``.
                            # ``thought_signature`` on tool-call parts is still required for replay.
                            try:
                                gen_config_kw["thinking_config"] = types.ThinkingConfig(
                                    thinking_level=tl,
                                    include_thoughts=True,
                                )
                            except Exception:
                                gen_config_kw["thinking_config"] = types.ThinkingConfig(
                                    thinking_level=tl
                                )
                    else:
                        budget = _gemini_thinking_budget_from_effort(reff)
                        if budget is not None:
                            try:
                                gen_config_kw["thinking_config"] = types.ThinkingConfig(
                                    thinking_budget=budget,
                                    include_thoughts=True,
                                )
                            except Exception:
                                gen_config_kw["thinking_config"] = types.ThinkingConfig(
                                    thinking_budget=budget
                                )
                if self.config.max_tokens is not None:
                    gen_config_kw["max_output_tokens"] = self.config.max_tokens
                _llm_seed = getattr(self.config, "seed", None)
                if _llm_seed is not None:
                    gen_config_kw["seed"] = _gemini_api_seed_i32(_llm_seed)
                if gemini_tools:
                    gen_config_kw["tools"] = gemini_tools
                    # We manage tool execution ourselves; this prevents the SDK from trying to "help".
                    gen_config_kw["automaticFunctionCalling"] = types.AutomaticFunctionCallingConfig(
                        disable=True
                    )

                gen_config = types.GenerateContentConfig(**gen_config_kw)
                # Raw I/O: ``gemini_io_json`` on the generate_content span (if small enough) +
                # ``log_gemini_generate_io`` below (includes ``tool_round`` in the log payload).
                _tool_round_token = set_current_gemini_tool_round(_round)
                try:
                    response = client.models.generate_content(
                        model=self.model,
                        contents=contents,
                        config=gen_config,
                    )
                finally:
                    reset_current_gemini_tool_round(_tool_round_token)
                log_bundle = {
                    "gemini_contents": to_jsonable(contents),
                    "gemini_config": to_jsonable(gen_config),
                    "api_key_masked": mask_secret(raw_key),
                }
                thought_text = _thought_text_from_response(response)
                return (
                    _extract_text(response),
                    thought_text,
                    response,
                    _extract_tool_calls(response),
                    log_bundle,
                )

            text, thought_text, response, tool_calls, log_bundle = await _with_retry_gemini(
                self, _generate
            )
            log_gemini_generate_io(
                model=self.model,
                tool_round=_round,
                contents=log_bundle["gemini_contents"],
                config=log_bundle["gemini_config"],
                response=response,
                api_key_masked=log_bundle.get("api_key_masked") or None,
                # Wrapper span already emits nested raw IO event; keep dump-file logging only.
                emit_logfire_event=False,
            )
            usage = usage_from_gemini_response(response)
            if usage:
                total_usage = {
                    k: int(total_usage.get(k, 0)) + int(usage.get(k, 0))
                    for k in set(total_usage) | set(usage)
                }
            total_cost += compute_cost(self.model, usage) if usage else 0.0

            if tool_calls:
                # Record assistant tool calls in LiteLLM-compatible history format.
                # This is required for evaluation (`_extract_predicted_actions`).
                tool_call_records = [
                    {
                        "id": tc["id"],
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                        "thought_signature": _thought_signature_for_history(
                            tc.get("thought_signature")
                        ),
                    }
                    for tc in tool_calls
                ]
                asst_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": text or "",
                    "tool_calls": [
                        {
                            "id": tr["id"],
                            "name": tr["name"],
                            "arguments": tr["arguments"],
                            "thought_signature": tr.get("thought_signature"),
                        }
                        for tr in tool_call_records
                    ],
                }
                if thought_text.strip():
                    asst_msg["reasoning_content"] = thought_text.strip()
                self.history.append(asst_msg)
                if on_chunk is not None and thought_text.strip():
                    await on_chunk("reasoning", thought_text.strip())

                for tr in tool_call_records:
                    if on_chunk is not None:
                        await on_chunk(
                            "tool_use",
                            {"name": tr["name"], "id": tr["id"], "input": tr["arguments"]},
                        )
                    result = await self._call_mcp_tool(tr["name"], tr["arguments"])
                    self.history.append(
                        {
                            "role": "tool",
                            "name": tr["name"],
                            "content": result,
                            "tool_call_id": tr["id"],
                        }
                    )
                # Next round: Gemini should incorporate tool results.
                continue

            # No tool calls: final assistant text.
            final_text = text.strip()
            final_msg: dict[str, Any] = {"role": "assistant", "content": final_text}
            if thought_text.strip():
                final_msg["reasoning_content"] = thought_text.strip()
            self.history.append(final_msg)
            if on_chunk is not None:
                if thought_text.strip():
                    await on_chunk("reasoning", thought_text.strip())
                await on_chunk("text", final_text)
            return final_text, {"usage": total_usage, "cost": total_cost}

        # Safety fallback: if the model keeps requesting tools, return whatever text we last got.
        final_text = final_text.strip()
        if on_chunk is not None and final_text:
            await on_chunk("text", final_text)
        return final_text, {"usage": total_usage, "cost": total_cost}
