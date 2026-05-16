"""Convert pipeline output (TrainingExample) into Gemma 4 chat-template format.

Critical Gemma 4 specifics:
- Native system role is supported; use it.
- Thinking content goes inside <start_of_turn>model ... <end_of_turn> on the CURRENT turn only.
- Thinking content must be STRIPPED from prior assistant turns in history.
- Function calls have native support; format per Gemma 4 chat template.

We let the HuggingFace tokenizer's apply_chat_template handle the wrapping —
that's the most robust path and follows whatever Google ships. We just produce
the messages list in the right shape, including the optional thinking block.
"""
from __future__ import annotations
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.schemas import TrainingExample, Continuation, StructuredTrace
from utils.templating import render_trace_block


def example_to_prompt_completion(ex: TrainingExample, include_thinking: bool = True) -> dict:
    """Convert a TrainingExample to TRL's conversational prompt-completion format.

    Returns {"prompt": [..messages..], "completion": [{"role": "assistant", ...}]}

    - `prompt` is all messages up to (but not including) the target turn.
    - `completion` is a single-element list containing the target assistant message.

    TRL's SFTTrainer, given this shape, applies the chat template internally
    and computes loss only on the completion tokens — exactly what we want.
    """
    prompt_messages = [{"role": "system", "content": ex.system_prompt}]

    # Add prior conversation history. Strip thinking from any historical assistant
    # content. Gemma 4 expects tool calls nested under "function" (OpenAI-style),
    # and crucially expects the tool result to be folded into the SAME assistant
    # message under "tool_responses" (not a separate tool-role message).
    #
    # See:
    #   https://ai.google.dev/gemma/docs/capabilities/text/function-calling-gemma4
    #
    # We walk the history forwards, look-ahead one step to merge tool_result
    # turns into the preceding assistant tool_call turn.
    history = list(ex.conversation_history)
    i = 0
    while i < len(history):
        h = history[i]
        role = h["role"]
        content = h["content"]

        if role == "assistant" and isinstance(content, dict) and "tool_call" in content:
            # Build the Gemma 4-shaped assistant message.
            tc = content["tool_call"]
            assistant_msg = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": tc.get("name"),
                        "arguments": tc.get("arguments", {}),
                    }
                }],
            }
            # Look-ahead: if the next turn is a tool result for this call,
            # fold it in as tool_responses.
            if i + 1 < len(history) and history[i + 1].get("role") == "tool":
                tr = history[i + 1]
                tr_content = tr.get("content")
                assistant_msg["tool_responses"] = [{
                    "name": tr.get("name", tc.get("name")),
                    "response": tr_content if isinstance(tr_content, dict) else {"value": tr_content},
                }]
                i += 2  # consume both
            else:
                i += 1
            prompt_messages.append(assistant_msg)
            continue

        if role == "tool":
            # Orphan tool result (no preceding assistant tool_call turn);
            # this shouldn't normally happen but skip it gracefully.
            i += 1
            continue

        if role == "assistant":
            prompt_messages.append({"role": "assistant", "content": content})
        else:
            prompt_messages.append({"role": role, "content": content})
        i += 1

    # Build the TARGET assistant turn (the completion)
    thinking_body = ""
    if include_thinking:
        thinking_body = render_trace_block(
            ex.structured_trace, continuation_text=ex.continuation.text or ""
        )

    if ex.tool_call:
        target_content = ""
        if include_thinking:
            target_content = f"<think>\n{thinking_body}\n</think>\n"
        tc = ex.tool_call.model_dump()
        completion_msg = {
            "role": "assistant",
            "content": target_content,
            "tool_calls": [{
                "function": {
                    "name": tc.get("name"),
                    "arguments": tc.get("arguments", {}),
                }
            }],
        }
    else:
        body = ex.response_to_user or ""
        if include_thinking:
            target_content = f"<think>\n{thinking_body}\n</think>\n{body}"
        else:
            target_content = body
        completion_msg = {"role": "assistant", "content": target_content}

    return {"prompt": prompt_messages, "completion": [completion_msg]}


def format_dataset(input_path: str, output_path: str):
    """Convert TrainingExample JSONL to TRL prompt-completion conversational JSONL.

    Output shape per line: {"prompt": [...], "completion": [...], "example_id": "..."}.
    TRL handles this shape natively: applies the chat template, computes loss on
    completion tokens only. No `formatting_func` needed at train time.
    """
    n = 0
    with open(input_path) as fin, open(output_path, "w") as fout:
        for line in fin:
            if not line.strip(): continue
            ex = TrainingExample(**json.loads(line))
            pc = example_to_prompt_completion(ex, include_thinking=True)
            pc["example_id"] = ex.example_id
            fout.write(json.dumps(pc) + "\n")
            n += 1
    print(f"[format] wrote {n} formatted examples to {output_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    format_dataset(args.input, args.output)