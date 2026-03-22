"""
Monkeypatch ``google.genai`` ``generate_content`` so Logfire's Model run matches **LiteLLM**:
``request_data`` / ``response_data`` (completion ``message.content`` must be a **string** per
Logfire's LiteLLM tests; list ``content`` is Unrecognised) plus ``all_messages_events`` (LangChain-style tool ``id`` field) for the full thread with Thoughts,
OpenInference ``llm.*_messages.*`` (assistant ``message.content``
= visible text only; ``message.reasoning`` + ``message.contents.*`` using ``type: thinking`` for Model Run),
``tool_call_id`` on tool rows, ``llm.model_name``, token counts, and
``gen_ai.*`` model/usage (no ``gen_ai.input/output.messages`` OTel JSON).

Request/response messages for Logfire are built from **native** ``t_contents`` / candidate
``Part`` objects so ``Part.thought`` is preserved; OpenTelemetry ``to_input_messages`` drops
that flag and was merging thought into plain text.

Call after ``logfire.configure()``::

    from agent.logfire_gemini_integration import instrument_logfire_gemini
    instrument_logfire_gemini()
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
from collections.abc import Sequence
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

# OpenInference semconv (same strings as openinference.semconv.trace).
_LLM_INPUT_MESSAGES = "llm.input_messages"
_LLM_OUTPUT_MESSAGES = "llm.output_messages"
_OI_MSG_ROLE = "message.role"
_OI_MSG_CONTENT = "message.content"
_OI_MSG_REASONING = "message.reasoning"
_OI_MSG_TOOL_CALL_ID = "message.tool_call_id"
_OI_MSG_NAME = "message.name"
_OI_MSG_TOOL_CALLS = "message.tool_calls"
_OI_MSG_CONTENTS = "message.contents"
_OI_MC_TYPE = "message_content.type"
_OI_MC_TEXT = "message_content.text"
_OI_TOOL_CALL_ID = "tool_call.id"
_OI_TOOL_CALL_FN_NAME = "tool_call.function.name"
_OI_TOOL_CALL_FN_ARGS = "tool_call.function.arguments"

_store: dict[str, Any] = {"patched": False, "sync": None, "async": None}


def _system_instruction_joined_text(config: Any) -> str | None:
    """Plain system prompt text from config (injected into ``request_data`` ``messages``)."""
    if config is None:
        return None
    try:
        from google.genai.models import t as transformers
        from google.genai.types import GenerateContentConfig
    except ImportError:
        return None
    try:
        cfg = (
            GenerateContentConfig.model_validate(config)
            if isinstance(config, dict)
            else config
        )
        su = getattr(cfg, "system_instruction", None)
        if not su:
            return None
        content = transformers.t_contents(su)[0]
        if not content.parts:
            return None
        text = " ".join(part.text for part in content.parts if getattr(part, "text", None))
        return text or None
    except Exception:
        return None


def _attach_system_instruction(span: Any, config: Any) -> None:
    if config is None:
        return
    try:
        from google.genai.models import t as transformers
        from google.genai.types import GenerateContentConfig
        from opentelemetry.instrumentation.google_genai.message import to_system_instructions
        from opentelemetry.semconv._incubating.attributes import gen_ai_attributes
        from opentelemetry.util.genai.utils import gen_ai_json_dumps
    except ImportError:
        return
    try:
        cfg = (
            GenerateContentConfig.model_validate(config)
            if isinstance(config, dict)
            else config
        )
        su = getattr(cfg, "system_instruction", None)
        if not su:
            return
        content = transformers.t_contents(su)[0]
        parts = to_system_instructions(content=content)
        if not parts:
            return
        payload: list[Any] = []
        for p in parts:
            payload.append(
                dataclasses.asdict(p) if dataclasses.is_dataclass(p) else str(p)
            )
        span.set_attribute(
            gen_ai_attributes.GEN_AI_SYSTEM_INSTRUCTIONS,
            gen_ai_json_dumps(payload),
        )
    except Exception:
        pass


def _thought_text_from_part_dict(d: dict[str, Any]) -> str | None:
    """Gemini native / OTel: thought body is in ``text`` or ``content``."""
    c = d.get("content")
    if isinstance(c, str) and c.strip():
        return c.strip()
    t = d.get("text")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return None


def _is_thought_part_dict(d: dict[str, Any]) -> bool:
    return d.get("thought") is True or str(d.get("type", "")).lower() == "thought"


def _partition_thought_semconv_parts(parts: list[Any]) -> tuple[list[str], list[Any]]:
    """Split Gemini thought summaries (``Part.thought`` / semconv dict) from other parts."""
    thought_texts: list[str] = []
    rest: list[Any] = []
    for p in parts:
        if isinstance(p, dict) and _is_thought_part_dict(p):
            body = _thought_text_from_part_dict(p)
            if body:
                thought_texts.append(body)
            continue
        # Native ``google.genai.types.Part``: OTel ``to_input_messages`` strips ``thought``;
        # we only see this when parts come from ``t_contents`` (see ``_native_content_list_to_openai_messages``).
        if not isinstance(p, dict) and getattr(p, "thought", None):
            t = getattr(p, "text", None)
            if isinstance(t, str) and t.strip():
                thought_texts.append(t.strip())
            continue
        d = _part_to_dict(p)
        if _is_thought_part_dict(d):
            body = _thought_text_from_part_dict(d)
            if body:
                thought_texts.append(body)
            continue
        rest.append(p)
    return thought_texts, rest


def _function_call_to_openai_fc_dict(fc: Any) -> dict[str, Any]:
    """Normalize ``FunctionCall`` (dict or pydantic/dataclass) for Logfire tool row parsing."""
    if isinstance(fc, dict):
        d = fc
    else:
        d = _part_to_dict(fc)
        if not d and fc is not None:
            name = getattr(fc, "name", None)
            args = getattr(fc, "args", None)
            if args is None:
                args = getattr(fc, "arguments", None)
            if not isinstance(args, dict):
                args = {} if args is None else dict(args)  # type: ignore[arg-type]
            d = {"name": name, "args": args, "id": getattr(fc, "id", None)}
    args = d.get("args")
    if args is None:
        args = d.get("arguments", {})
    return {
        "name": d.get("name") or "",
        "args": args if isinstance(args, dict) else {},
        "id": d.get("id"),
    }


def _parts_for_llm_panel(parts: list[Any]) -> list[Any]:
    """Mirror Logfire's Google GenAI `transform_part`: flatten text / function_call parts.

    ``google.genai.types.Part`` is Pydantic, not a stdlib dataclass — without ``model_dump`` we
    used to pass raw objects through and Logfire showed Python ``repr`` blobs.
    """
    out: list[Any] = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
            continue
        d = _part_to_dict(p)
        if not d:
            fc = getattr(p, "function_call", None)
            if fc is not None:
                fcd = _function_call_to_openai_fc_dict(fc)
                out.append({"function_call": fcd})
                continue
            t = getattr(p, "text", None)
            if isinstance(t, str) and t:
                out.append(t)
                continue
            continue
        if d.get("type") == "text" and "content" in d:
            keys = set(d.keys()) - {"type"}
            if not keys or keys == {"content"}:
                out.append(d["content"])
                continue
        # Gemini ``Part.model_dump()`` uses top-level ``text``, not ``type``/``content``.
        if (
            isinstance(d.get("text"), str)
            and (d.get("text") or "").strip()
            and d.get("function_call") is None
        ):
            out.append(str(d["text"]))
            continue
        fc = d.get("function_call")
        if fc is not None:
            fcd = _function_call_to_openai_fc_dict(fc)
            merged = {**d, "function_call": fcd}
            out.append(merged)
            continue
        out.append(d)
    return out


def _tool_call_arguments_json(args: Any) -> str:
    if args is None:
        return "{}"
    if isinstance(args, str):
        return args
    return json.dumps(args, ensure_ascii=False, default=str)


def _normalize_chat_content_for_logfire(content: Any) -> str | None:
    """OpenAI / LiteLLM use string ``content`` for text; lists (e.g. one string in a list) confuse Logfire."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if not content:
            return ""
        if all(isinstance(x, str) for x in content):
            return content[0] if len(content) == 1 else "\n\n".join(content)
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


def _part_to_dict(p: Any) -> dict[str, Any]:
    if isinstance(p, dict):
        return {k: v for k, v in p.items() if v is not None}
    if dataclasses.is_dataclass(p) and not isinstance(p, type):
        return dataclasses.asdict(p)
    model_dump = getattr(p, "model_dump", None)
    if callable(model_dump):
        for kwargs in ({"mode": "python"}, {}):
            try:
                raw = model_dump(**kwargs)
            except TypeError:
                continue
            if isinstance(raw, dict):
                return {k: v for k, v in raw.items() if v is not None}
    return {}


def _assistant_openai_from_parts(parts: list[Any]) -> dict[str, Any]:
    """Shape assistant messages for Logfire Model run.

    Thought summaries go in ``reasoning_content`` (and flat ``message.reasoning``); ``content`` is
    **visible** assistant text only (never merged with thoughts) so the Details panel and Model run
    can split them. ``message.contents.*`` is filled from the same split in
    ``_openinference_flat_message_attributes``.
    """
    thought_texts, non_thought_parts = _partition_thought_semconv_parts(parts)
    panel_parts = _parts_for_llm_panel(non_thought_parts)
    tool_calls: list[dict[str, Any]] = []
    rest: list[Any] = []
    for item in panel_parts:
        if isinstance(item, dict) and item.get("type") == "tool_call":
            tool_calls.append(
                {
                    "id": str(item.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": _tool_call_arguments_json(item.get("arguments")),
                    },
                }
            )
        elif isinstance(item, dict) and item.get("function_call") is not None:
            fc = _function_call_to_openai_fc_dict(item["function_call"])
            args = fc.get("args") or {}
            tool_calls.append(
                {
                    "id": str(fc.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(fc.get("name") or ""),
                        "arguments": _tool_call_arguments_json(args),
                    },
                }
            )
        else:
            rest.append(item)

    reasoning_joined = "\n\n".join(thought_texts) if thought_texts else ""

    visible_str: str | None = None
    if rest:
        v = _normalize_chat_content_for_logfire(rest)
        if v is not None and str(v).strip():
            visible_str = str(v).strip()
    elif not tool_calls:
        v = _normalize_chat_content_for_logfire(panel_parts)
        if v is not None and str(v).strip():
            visible_str = str(v).strip()

    msg: dict[str, Any] = {"role": "assistant"}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if visible_str is not None and str(visible_str).strip():
        msg["content"] = visible_str
    elif tool_calls:
        msg["content"] = None
    else:
        msg["content"] = ""
    if reasoning_joined:
        msg["reasoning_content"] = reasoning_joined
    return msg


def _tool_message_content_for_panel(payload: Any) -> str:
    """LiteLLM / OpenAI chat completions log tool results as JSON **strings**; match that for Logfire."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, default=str)
    except TypeError:
        return str(payload)


def _tool_parts_to_openai_messages(parts: list[Any]) -> list[dict[str, Any]]:
    """One OpenAI ``role: tool`` row per ``tool_call_response`` part."""
    out: list[dict[str, Any]] = []
    for p in parts:
        d = _part_to_dict(p)
        if d.get("type") != "tool_call_response":
            continue
        tid = str(d.get("id") or "")
        payload = d.get("response")
        if payload is None and "result" in d:
            payload = d["result"]
        name = d.get("name")
        if not isinstance(name, str):
            name = ""
        out.append(
            {
                "role": "tool",
                "tool_call_id": tid,
                "name": name,
                "content": _tool_message_content_for_panel(payload),
            }
        )
    return out


def _user_content_openai(parts: list[Any]) -> str | None:
    return _normalize_chat_content_for_logfire(_parts_for_llm_panel(parts))


def _content_role_str(role: Any) -> str:
    if role is None:
        return ""
    if isinstance(role, str):
        return role.lower()
    name = getattr(role, "name", None)
    if isinstance(name, str):
        return name.lower()
    s = str(role)
    return s.rsplit(".", 1)[-1].lower()


def _native_user_parts_to_openai_content(parts: list[Any]) -> str | None:
    texts: list[str] = []
    for p in parts:
        if getattr(p, "thought", None):
            continue
        t = getattr(p, "text", None)
        if isinstance(t, str) and t:
            texts.append(t)
    if not texts:
        return None
    return texts[0] if len(texts) == 1 else "\n\n".join(texts)


def _native_tool_parts_to_openai(parts: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in parts:
        fr = getattr(p, "function_response", None)
        if fr is None:
            continue
        tid = str(getattr(fr, "id", None) or "")
        payload = getattr(fr, "response", None)
        fn = getattr(fr, "name", None)
        if isinstance(fn, str):
            name = fn.strip()
        elif fn is not None:
            name = str(fn).strip()
        else:
            name = ""
        out.append(
            {
                "role": "tool",
                "tool_call_id": tid,
                "name": name,
                "content": _tool_message_content_for_panel(payload),
            }
        )
    return out


def _native_content_list_to_openai_messages(
    content_list: list[Any], *, config: Any
) -> list[dict[str, Any]]:
    """Map native Gemini ``Content`` list to OpenAI chat messages.

    Uses ``Part.thought`` on the wire. OpenTelemetry ``to_input_messages`` drops that flag when
    converting to semconv parts, which caused thought and visible text to merge in Logfire.
    """
    messages: list[dict[str, Any]] = []
    sys_text = _system_instruction_joined_text(config)
    first_role = getattr(content_list[0], "role", None) if content_list else None
    if sys_text and _content_role_str(first_role) != "system":
        messages.append({"role": "system", "content": sys_text})

    for c in content_list:
        parts = list(getattr(c, "parts", None) or [])
        role_s = _content_role_str(getattr(c, "role", None))
        if not role_s and parts and all(
            getattr(p, "function_response", None) is not None for p in parts
        ):
            role_s = "function"

        if role_s == "user":
            messages.append(
                {"role": "user", "content": _native_user_parts_to_openai_content(parts) or ""}
            )
        elif role_s in ("model", "assistant"):
            messages.append(_assistant_openai_from_parts(parts))
        elif role_s == "function":
            tool_msgs = _native_tool_parts_to_openai(parts)
            if tool_msgs:
                messages.extend(tool_msgs)
            else:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": "",
                        "name": "",
                        "content": json.dumps(
                            [_part_to_dict(p) for p in parts], default=str
                        ),
                    }
                )
        else:
            uc = _native_user_parts_to_openai_content(parts)
            messages.append(
                {
                    "role": role_s or "user",
                    "content": uc
                    if uc is not None
                    else json.dumps([_part_to_dict(p) for p in parts], default=str),
                }
            )

    return messages


def _native_response_to_openai_message(response: Any) -> dict[str, Any]:
    """First candidate from a native ``GenerateContentResponse`` (preserves ``Part.thought``)."""
    cands = getattr(response, "candidates", None) or []
    if not cands:
        return {"role": "assistant", "content": ""}
    content = getattr(cands[0], "content", None)
    parts = list(getattr(content, "parts", None) or [])
    return _assistant_openai_from_parts(parts)


def _input_msgs_to_openai_messages(input_msgs: list[Any], *, config: Any) -> list[dict[str, Any]]:
    """Build LiteLLM-shaped ``messages`` list (OpenAI chat completions)."""
    messages: list[dict[str, Any]] = []
    sys_text = _system_instruction_joined_text(config)
    if sys_text and (
        not input_msgs or getattr(input_msgs[0], "role", None) != "system"
    ):
        messages.append({"role": "system", "content": sys_text})

    for im in input_msgs:
        parts = list(im.parts)
        role = im.role
        if role == "assistant":
            messages.append(_assistant_openai_from_parts(parts))
        elif role == "tool":
            tool_msgs = _tool_parts_to_openai_messages(parts)
            if tool_msgs:
                messages.extend(tool_msgs)
            else:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": "",
                        "name": "",
                        "content": json.dumps(_parts_for_llm_panel(parts), default=str),
                    }
                )
        else:
            messages.append({"role": role, "content": _user_content_openai(parts)})
    return messages


def _outputs_to_openai_response_message(outputs: list[Any]) -> dict[str, Any]:
    """First model candidate as an OpenAI-shaped assistant message."""
    if not outputs:
        return {"role": "assistant", "content": ""}
    return _assistant_openai_from_parts(list(outputs[0].parts))


def _assistant_reasoning_visible_for_flat(
    message: dict[str, Any],
) -> tuple[str, str | None]:
    """Split assistant ``reasoning_content`` / list ``content`` into (reasoning, visible).

    ``visible`` is ``None`` when there is no string content (e.g. tool-call-only turn with
    ``content: null``). Legacy list-shaped ``content`` with ``type: reasoning`` / ``text`` is
    still supported when ``reasoning_content`` is absent.
    """
    top_rc = message.get("reasoning_content") or message.get("reasoning")
    has_top_reasoning = isinstance(top_rc, str) and bool(top_rc.strip())
    reasoning_chunks: list[str] = []
    if has_top_reasoning:
        reasoning_chunks.append(top_rc.strip())

    raw = message.get("content")
    visible_chunks: list[str] = []
    if isinstance(raw, list):
        for b in raw:
            if not isinstance(b, dict):
                continue
            bt = str(b.get("type") or "").lower()
            if bt in ("reasoning", "thinking"):
                if not has_top_reasoning:
                    body = b.get("content") or b.get("text") or b.get("thinking")
                    if isinstance(body, str) and body.strip():
                        reasoning_chunks.append(body.strip())
            elif bt == "text":
                body = b.get("text") or b.get("content")
                if isinstance(body, str) and body.strip():
                    visible_chunks.append(body.strip())
    elif isinstance(raw, str):
        visible_chunks.append(raw)
    elif raw is not None:
        visible_chunks.append(str(raw))

    reasoning_full = "\n\n".join(reasoning_chunks) if reasoning_chunks else ""
    if visible_chunks:
        visible_val = "\n\n".join(visible_chunks)
    elif isinstance(raw, str):
        visible_val = raw
    else:
        visible_val = None
    return reasoning_full, visible_val


def _openinference_emit_content_blocks(
    attrs: dict[str, Any], prefix: str, blocks: Sequence[Any]
) -> None:
    """``message.contents.N`` — body from ``text``, ``content``, or ``thinking`` on each block."""
    for bi, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        bprefix = f"{prefix}.{_OI_MSG_CONTENTS}.{bi}"
        if (bt := block.get("type")) is not None:
            attrs[f"{bprefix}.{_OI_MC_TYPE}"] = bt
        body = block.get("text")
        if not isinstance(body, str):
            body = block.get("content")
        if not isinstance(body, str):
            body = block.get("thinking")
        if isinstance(body, str):
            attrs[f"{bprefix}.{_OI_MC_TEXT}"] = body


def _openinference_flat_message_attributes(
    messages: list[dict[str, Any]],
    *,
    message_type: Literal["input", "output"],
) -> dict[str, Any]:
    """Flatten chat messages to ``llm.input_messages`` / ``llm.output_messages`` (OpenInference)."""
    attrs: dict[str, Any] = {}
    base = _LLM_INPUT_MESSAGES if message_type == "input" else _LLM_OUTPUT_MESSAGES
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        prefix = f"{base}.{i}"
        if (role := message.get("role")) is not None:
            attrs[f"{prefix}.{_OI_MSG_ROLE}"] = role

        if role == "assistant":
            reasoning_full, visible_val = _assistant_reasoning_visible_for_flat(message)
            if reasoning_full:
                attrs[f"{prefix}.{_OI_MSG_REASONING}"] = reasoning_full
            blocks: list[dict[str, str]] = []
            # Logfire Model Run matches pydantic-ai / LiteLLM: ``type: thinking`` + ``text``, not ``reasoning``.
            if reasoning_full:
                blocks.append({"type": "thinking", "text": reasoning_full})
            if isinstance(visible_val, str) and visible_val.strip():
                blocks.append({"type": "text", "text": visible_val.strip()})
            if blocks:
                _openinference_emit_content_blocks(attrs, prefix, blocks)
            if visible_val is not None:
                attrs[f"{prefix}.{_OI_MSG_CONTENT}"] = visible_val
            contents_seq = message.get("contents")
            if isinstance(contents_seq, Sequence) and contents_seq and not blocks:
                _openinference_emit_content_blocks(attrs, prefix, contents_seq)
        else:
            raw_content = message.get("content")
            if isinstance(raw_content, list) and raw_content:
                _openinference_emit_content_blocks(attrs, prefix, raw_content)
            elif raw_content is not None:
                attrs[f"{prefix}.{_OI_MSG_CONTENT}"] = raw_content
            contents_seq = message.get("contents")
            if isinstance(contents_seq, Sequence) and not (
                isinstance(raw_content, list) and raw_content
            ):
                _openinference_emit_content_blocks(attrs, prefix, contents_seq)
        if isinstance(tcid := message.get("tool_call_id"), str) and tcid:
            attrs[f"{prefix}.{_OI_MSG_TOOL_CALL_ID}"] = tcid
        if role == "tool" and isinstance(n := message.get("name"), str):
            attrs[f"{prefix}.{_OI_MSG_NAME}"] = n
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, Sequence):
            for j, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    continue
                tcp = f"{prefix}.{_OI_MSG_TOOL_CALLS}.{j}"
                if (call_id := tc.get("id")) is not None:
                    attrs[f"{tcp}.{_OI_TOOL_CALL_ID}"] = call_id
                fn = tc.get("function")
                if isinstance(fn, dict):
                    if isinstance(fn_name := fn.get("name"), str):
                        attrs[f"{tcp}.{_OI_TOOL_CALL_FN_NAME}"] = fn_name
                    fn_args = fn.get("arguments")
                    if isinstance(fn_args, str):
                        attrs[f"{tcp}.{_OI_TOOL_CALL_FN_ARGS}"] = fn_args
                    elif isinstance(fn_args, dict):
                        attrs[f"{tcp}.{_OI_TOOL_CALL_FN_ARGS}"] = json.dumps(
                            fn_args, ensure_ascii=False, default=str
                        )
    return attrs


def _to_logfire_llm_ui_message(message: dict[str, Any]) -> dict[str, Any]:
    """Shape assistant rows in ``request_data.messages`` for Logfire Model Run.

    History rows use pydantic-ai style blocks: ``{\"type\": \"thinking\", \"text\": \"...\"}`` plus
    optional ``{\"type\": \"text\", \"text\": \"...\"}`` (Logfire #2131).

    Tool rows keep OpenAI shape (``tool_call_id``) here; see ``_to_logfire_all_messages_event``.
    """
    if message.get("role") == "tool":
        out = dict(message)
        out.setdefault("name", "")
        return out
    if message.get("role") != "assistant":
        return dict(message)
    reasoning = (message.get("reasoning_content") or message.get("reasoning") or "").strip()
    out = {k: v for k, v in message.items() if k not in ("reasoning_content", "reasoning")}
    visible = out.get("content")
    if reasoning:
        blocks: list[dict[str, str]] = [{"type": "thinking", "text": reasoning}]
        if isinstance(visible, str) and visible.strip():
            blocks.append({"type": "text", "text": visible.strip()})
        out["content"] = blocks
        return out
    if isinstance(visible, list):
        return out
    return out


def _to_logfire_all_messages_event(message: dict[str, Any]) -> dict[str, Any]:
    """Shape one message for ``all_messages_events`` (LangChain-style tool rows).

    Logfire's Model Run uses this array for the full thread (including final Thoughts). Tool
    messages must match ``processor_wrapper._transform_langchain_message``: ``tool_call_id`` is
    renamed to ``id``; OpenAI-shaped ``tool_call_id`` alone is shown as **Unrecognised**.
    """
    if message.get("role") != "tool":
        return _to_logfire_llm_ui_message(message)
    out = dict(message)
    out.setdefault("name", "")
    tcid = out.pop("tool_call_id", None)
    if tcid is not None:
        out["id"] = str(tcid)
    return out


def _lite_llm_response_message(message: dict[str, Any]) -> dict[str, Any]:
    """Shape ``response_data.message`` like Logfire's LiteLLM snapshots (``test_litellm.py``).

    The Model Run **completion** slot only accepts ``content`` as a **string** or ``null``; list
    ``content`` (thinking blocks) is always shown as **Unrecognised**. Optional ``reasoning_content``
    is included for APIs that expose it next to plain ``content``.
    """
    if message.get("role") != "assistant":
        return dict(message)
    rc = (message.get("reasoning_content") or message.get("reasoning") or "").strip()
    out = {k: v for k, v in message.items() if k not in ("reasoning_content", "reasoning")}
    visible = out.get("content")
    if isinstance(visible, str) and visible.strip():
        out["content"] = visible.strip()
    elif visible is None:
        out["content"] = None
    else:
        out["content"] = _normalize_chat_content_for_logfire(visible)
    if rc:
        out["reasoning_content"] = rc
    return out


def _attach_litellm_style_request_response(
    span: Any,
    *,
    model: str,
    internal_messages: list[dict[str, Any]],
    response_message: dict[str, Any],
) -> None:
    """Mirror Logfire's LiteLLM / LangChain payloads for the Model run tab."""
    try:
        from logfire._internal.constants import ATTRIBUTES_JSON_SCHEMA_KEY, ATTRIBUTES_TAGS_KEY
        from logfire._internal.json_schema import JsonSchemaProperties, attributes_json_schema
    except ImportError:
        return

    ui_messages = [_to_logfire_llm_ui_message(m) for m in internal_messages]
    request_data: dict[str, Any] = {"model": model, "messages": ui_messages}
    response_data: dict[str, Any] = {
        "message": _lite_llm_response_message(response_message),
    }
    # LangChain-style transcript: tool rows use ``id`` not ``tool_call_id`` (see
    # ``_to_logfire_all_messages_event``).
    all_events = [_to_logfire_all_messages_event(m) for m in internal_messages] + [
        _to_logfire_all_messages_event(response_message)
    ]
    all_messages_events = json.dumps(all_events, default=str)

    span.set_attribute("request_data", request_data)
    span.set_attribute("response_data", response_data)
    span.set_attribute("all_messages_events", all_messages_events)
    span.set_attribute(ATTRIBUTES_TAGS_KEY, ["LLM"])
    span.set_attribute(
        ATTRIBUTES_JSON_SCHEMA_KEY,
        attributes_json_schema(
            JsonSchemaProperties(
                {
                    "request_data": {"type": "object"},
                    "response_data": {"type": "object"},
                    "all_messages_events": {"type": "array"},
                }
            )
        ),
    )


def _apply_gen_ai_span_attributes(
    span: Any, *, model: str, contents: Any, config: Any, response: Any
) -> None:
    try:
        from google.genai.models import t as transformers
        from opentelemetry.semconv._incubating.attributes import gen_ai_attributes
    except ImportError as e:
        logger.warning(
            "logfire_gemini_integration: missing dependency (%s); "
            "install google-genai and opentelemetry.semconv",
            e,
        )
        return

    content_list = transformers.t_contents(contents)
    span.set_attribute(gen_ai_attributes.GEN_AI_SYSTEM, "google")
    span.set_attribute(gen_ai_attributes.GEN_AI_OPERATION_NAME, "generate_content")
    span.set_attribute(gen_ai_attributes.GEN_AI_REQUEST_MODEL, model)
    _attach_system_instruction(span, config)

    openai_messages = _native_content_list_to_openai_messages(
        content_list, config=config
    )
    response_message = _native_response_to_openai_message(response)

    oi_flat = {
        **_openinference_flat_message_attributes(openai_messages, message_type="input"),
        **_openinference_flat_message_attributes(
            [response_message], message_type="output"
        ),
    }
    for k, v in oi_flat.items():
        span.set_attribute(k, v)
    span.set_attribute("llm.model_name", model)
    span.set_attribute("llm.system", "google")

    mv = getattr(response, "model_version", None)
    if mv:
        span.set_attribute(gen_ai_attributes.GEN_AI_RESPONSE_MODEL, str(mv))

    um = getattr(response, "usage_metadata", None)
    if um is not None:
        pt = getattr(um, "prompt_token_count", None)
        ct = getattr(um, "candidates_token_count", None)
        tt = getattr(um, "total_token_count", None)
        if pt is not None:
            span.set_attribute("gen_ai.usage.input_tokens", pt)
            span.set_attribute("llm.token_count.prompt", pt)
        if ct is not None:
            span.set_attribute("gen_ai.usage.output_tokens", ct)
            span.set_attribute("llm.token_count.completion", ct)
        if tt is not None:
            span.set_attribute("gen_ai.usage.total_tokens", tt)
            span.set_attribute("llm.token_count.total", tt)

    _attach_litellm_style_request_response(
        span,
        model=model,
        internal_messages=openai_messages,
        response_message=response_message,
    )

    _attach_raw_generate_content_io(span, model=model, contents=contents, config=config, response=response)


# OTLP exporters often cap string attributes (~128KiB); stay under to avoid drops.
_GEMINI_IO_JSON_MAX_ATTR_CHARS = 120_000


def _attach_raw_generate_content_io(
    span: Any,
    *,
    model: str,
    contents: Any,
    config: Any,
    response: Any,
) -> None:
    """Same payload shape as ``gemini_log.log_gemini_generate_io`` (minus ``tool_round``) on this span."""
    try:
        from .gemini_log import _dumps, logfire_raw_io_enabled, to_jsonable
    except ImportError:
        return
    if not logfire_raw_io_enabled():
        return
    try:
        payload = {
            "request": {
                "model": model,
                "contents": to_jsonable(contents),
                "config": to_jsonable(config),
            },
            "response": to_jsonable(response),
        }
        s = _dumps(payload)
        if len(s) <= _GEMINI_IO_JSON_MAX_ATTR_CHARS:
            span.set_attribute("gemini_io_json", s)
    except Exception:
        pass


def _generate_content_span_kwargs(model: str) -> dict[str, Any]:
    return {
        "_span_name": "generate_content",
        "gen_ai_system": "google",
        "gen_ai_operation_name": "generate_content",
        "gen_ai_request_model": model,
    }


def _wrap_sync(orig: Callable[..., Any]) -> Callable[..., Any]:
    import logfire

    @functools.wraps(orig)
    def wrapped(self: Any, *, model: str, contents: Any, config: Any = None, **kwargs: Any):
        with logfire.span(
            "google.genai generate_content",
            **_generate_content_span_kwargs(model),
        ) as span:
            try:
                response = orig(self, model=model, contents=contents, config=config, **kwargs)
            except BaseException:
                span.set_attribute("error", True)
                raise
            _apply_gen_ai_span_attributes(
                span,
                model=model,
                contents=contents,
                config=config,
                response=response,
            )
            return response

    return wrapped


def _wrap_async(orig: Callable[..., Any]) -> Callable[..., Any]:
    import logfire

    @functools.wraps(orig)
    async def wrapped(
        self: Any, *, model: str, contents: Any, config: Any = None, **kwargs: Any
    ):
        with logfire.span(
            "google.genai generate_content",
            **_generate_content_span_kwargs(model),
        ) as span:
            try:
                response = await orig(
                    self, model=model, contents=contents, config=config, **kwargs
                )
            except BaseException:
                span.set_attribute("error", True)
                raise
            _apply_gen_ai_span_attributes(
                span,
                model=model,
                contents=contents,
                config=config,
                response=response,
            )
            return response

    return wrapped


def instrument_logfire_gemini() -> None:
    """Monkeypatch ``Models.generate_content`` / ``AsyncModels.generate_content`` once."""
    if _store["patched"]:
        return
    try:
        from google.genai import models as genai_models
    except ImportError as e:
        logger.warning("logfire_gemini_integration: google.genai not installed (%s)", e)
        return

    _store["sync"] = genai_models.Models.generate_content
    _store["async"] = genai_models.AsyncModels.generate_content
    genai_models.Models.generate_content = _wrap_sync(genai_models.Models.generate_content)  # type: ignore[method-assign]
    genai_models.AsyncModels.generate_content = _wrap_async(  # type: ignore[method-assign]
        genai_models.AsyncModels.generate_content
    )
    _store["patched"] = True


def uninstrument_logfire_gemini() -> None:
    """Restore original ``generate_content`` methods (e.g. for tests)."""
    if not _store["patched"]:
        return
    from google.genai import models as genai_models

    if _store["sync"] is not None:
        genai_models.Models.generate_content = _store["sync"]  # type: ignore[method-assign]
    if _store["async"] is not None:
        genai_models.AsyncModels.generate_content = _store["async"]  # type: ignore[method-assign]
    _store["sync"] = None
    _store["async"] = None
    _store["patched"] = False
