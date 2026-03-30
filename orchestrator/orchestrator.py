from enum import Enum

import logfire

from agent import BaseAgent
from chat.config import SimulationConfig
from chat.display import (
    StreamingDisplay,
    print_markdown,
    print_role_header,
    print_simulation_complete,
    print_stop_phrase,
    print_tool_call,
    print_max_turns_reached,
    print_turn_cost,
    print_total_cost,
)
from .event_bus import EventBus, Message, Role

# Matches tau2-bench ``DEFAULT_FIRST_AGENT_MESSAGE`` (non-LLM seed before the user simulator replies).
DEFAULT_FIRST_AGENT_MESSAGE = "Hi! How can I help you today?"


async def _stream_chunk(display: StreamingDisplay, event_type: str, data: object) -> None:
    """Callback for agent streaming: update live markdown or show tool call."""
    if event_type == "text":
        display.update_text(str(data))
    elif event_type == "tool_use" and isinstance(data, dict):
        print_tool_call(
            name=data.get("name", ""),
            tool_id=data.get("id", ""),
            input_data=data.get("input"),
        )


class SoloStopMode(Enum):
    """Stopping criteria for single-agent (assistant-only) runs."""

    FIRST_TEXT_ONLY = "first_text_only"
    TASK_COMPLETE_TOOL = "task_complete_tool"


class Orchestrator:
    def __init__(
        self,
        assistant: BaseAgent,
        user: BaseAgent,
        bus: EventBus,
        config: SimulationConfig,
    ) -> None:
        self.assistant = assistant
        self.user = user
        self.bus = bus
        self.config = config
        self.transcript: list[Message] = []

    def _check_stop(self, text: str) -> bool:
        return any(phrase in text for phrase in self.config.stop_phrases)

    def _check_user_stop(self, text: str) -> bool:
        """``stop_phrases`` (e.g. ``###STOP###``, ``###TRANSFER###``), optional literal ``stop``."""
        if self._check_stop(text):
            return True
        if getattr(self.config, "stop_on_user_stop_word", False):
            if (text or "").strip().lower() == "stop":
                return True
        return False

    async def _user_respond_stream(
        self,
        incoming: str,
        *,
        print_header: bool,
        user_turn: int | None,
    ) -> tuple[str, dict]:
        """Run the user simulator on ``incoming`` (assistant text or greeting)."""
        await self.bus.send_to_user(incoming)
        if print_header:
            print_role_header("User", turn=user_turn)
        if self.user.use_streaming_display:
            with logfire.span("user"):
                with StreamingDisplay() as display:

                    async def on_chunk_user(ev: str, data: object) -> None:
                        await _stream_chunk(display, ev, data)

                    user_reply, usage_info_user = await self.user.respond_stream(
                        incoming, on_chunk=on_chunk_user
                    )
                    display.finish()
        else:
            with logfire.span("user"):

                async def on_chunk_user(ev: str, data: object) -> None:
                    if ev == "tool_use" and isinstance(data, dict):
                        print_tool_call(
                            name=data.get("name", ""),
                            tool_id=data.get("id", ""),
                            input_data=data.get("input"),
                        )

                user_reply, usage_info_user = await self.user.respond_stream(
                    incoming, on_chunk=on_chunk_user
                )
        return user_reply, usage_info_user

    async def run(self) -> list[Message]:
        initial = self.config.initial_message
        has_initial = bool(initial and initial.strip())

        assistant_total_cost = 0.0
        user_total_cost = 0.0

        if has_initial:
            # Optional: user speaks first (fixed YAML line).
            current_message = initial.strip()
            self.transcript.append(Message(role=Role.USER, content=current_message))
            print_role_header("User", seed=True)
            print_markdown(current_message)
        else:
            # tau2-bench: fixed assistant greeting (no LLM), then user simulator replies.
            greeting = (
                (getattr(self.config, "first_agent_message", None) or "").strip()
                or DEFAULT_FIRST_AGENT_MESSAGE
            )
            self.transcript.append(Message(role=Role.ASSISTANT, content=greeting))
            print_role_header("Assistant", seed=True)
            print_markdown(greeting)
            # So the first real assistant LLM call sees the same greeting in ``history`` (tau2-bench parity).
            a_hist = getattr(self.assistant, "history", None)
            if isinstance(a_hist, list):
                a_hist.append({"role": "assistant", "content": greeting})
            user_opening, usage_info_open = await self._user_respond_stream(
                greeting,
                print_header=True,
                user_turn=1,
            )
            u0 = usage_info_open.get("usage")
            c0 = usage_info_open.get("cost")
            if c0 is not None:
                user_total_cost += c0
            print_turn_cost("User", u0, c0)
            self.transcript.append(Message(role=Role.USER, content=user_opening))
            if self._check_user_stop(user_opening):
                print_stop_phrase("User stop (phrase or 'stop'). Goal achieved!")
                print_total_cost(assistant_total_cost, user_total_cost)
                print_simulation_complete(len(self.transcript))
                return self.transcript
            current_message = user_opening

        for turn in range(self.config.max_turns):
            # Assistant responds (streaming); nest LLM/tool spans under ``agent`` for traces.
            await self.bus.send_to_assistant(current_message)
            print_role_header("Assistant", turn=turn + 1)
            with logfire.span("agent"):
                with StreamingDisplay() as display:

                    async def on_chunk(ev: str, data: object) -> None:
                        await _stream_chunk(display, ev, data)

                    assistant_reply, usage_info = await self.assistant.respond_stream(
                        current_message, on_chunk=on_chunk
                    )
                    display.finish()

            u = usage_info.get("usage")
            c = usage_info.get("cost")
            if c is not None:
                assistant_total_cost += c
            print_turn_cost("Assistant", u, c)

            self.transcript.append(Message(role=Role.ASSISTANT, content=assistant_reply))

            if self._check_stop(assistant_reply):
                print_stop_phrase("Stop phrase detected in assistant reply.")
                break

            user_reply, usage_info_user = await self._user_respond_stream(
                assistant_reply,
                print_header=True,
                user_turn=turn + 1,
            )

            u_user = usage_info_user.get("usage")
            c_user = usage_info_user.get("cost")
            if c_user is not None:
                user_total_cost += c_user
            print_turn_cost("User", u_user, c_user)

            self.transcript.append(Message(role=Role.USER, content=user_reply))

            if self._check_user_stop(user_reply):
                print_stop_phrase("User stop (phrase or 'stop'). Goal achieved!")
                break

            current_message = user_reply
        else:
            print_max_turns_reached(self.config.max_turns)

        print_total_cost(assistant_total_cost, user_total_cost)
        print_simulation_complete(len(self.transcript))
        return self.transcript

    async def run_solo(
        self,
        prompt: str,
        *,
        stop_mode: SoloStopMode = SoloStopMode.FIRST_TEXT_ONLY,
        task_complete_tools: list[str] | None = None,
    ) -> list[Message]:
        """
        Run a "solo" simulation: only the assistant agent is called.

        Stopping criteria:
        - FIRST_TEXT_ONLY: stop after the first assistant turn that does not
          emit any tool_use chunks (i.e., a pure text reply).
        - TASK_COMPLETE_TOOL: stop once a tool whose name matches one of
          `task_complete_tools` is invoked.
        """
        current_message = (prompt or "").strip()
        if not current_message:
            current_message = "Begin the task."

        # Seed transcript with a synthetic "user" message containing the prompt
        self.transcript.append(Message(role=Role.USER, content=current_message))
        print_role_header("User", seed=True)
        print_markdown(current_message)

        assistant_total_cost = 0.0

        normalized_task_tools = [t.lower() for t in (task_complete_tools or [])]

        for turn in range(self.config.max_turns):
            await self.bus.send_to_assistant(current_message)
            print_role_header("Assistant", turn=turn + 1)

            saw_tool_use = False
            saw_task_complete_tool = False

            with StreamingDisplay() as display:

                async def on_chunk(ev: str, data: object) -> None:
                    nonlocal saw_tool_use, saw_task_complete_tool
                    if ev == "tool_use" and isinstance(data, dict):
                        saw_tool_use = True
                        tool_name = str(data.get("name", "")).lower()
                        if normalized_task_tools and any(
                            tool_name == t or tool_name.endswith(t)
                            for t in normalized_task_tools
                        ):
                            saw_task_complete_tool = True
                    await _stream_chunk(display, ev, data)

                assistant_reply, usage_info = await self.assistant.respond_stream(
                    current_message, on_chunk=on_chunk
                )
                display.finish()

            usage = usage_info.get("usage")
            cost = usage_info.get("cost")
            if cost is not None:
                assistant_total_cost += cost
            print_turn_cost("Assistant", usage, cost)

            self.transcript.append(
                Message(role=Role.ASSISTANT, content=assistant_reply)
            )

            # Global text-based stop phrases still apply
            if self._check_stop(assistant_reply):
                print_stop_phrase("Stop phrase detected in assistant reply.")
                break

            # Solo-mode specific stopping criteria
            if stop_mode is SoloStopMode.FIRST_TEXT_ONLY:
                # Stop immediately after the first assistant turn, regardless
                # of whether tools were used. The ticket + policy already live
                # in the system prompt; the assistant's first reply is the
                # final answer for this task.
                print_stop_phrase("Solo mode: first assistant reply received.")
                break

            if stop_mode is SoloStopMode.TASK_COMPLETE_TOOL and saw_task_complete_tool:
                print_stop_phrase("Solo mode: task-complete tool invocation detected.")
                break

            current_message = assistant_reply
        else:
            print_max_turns_reached(self.config.max_turns)

        # In solo mode, only assistant cost is relevant
        print_total_cost(assistant_total_cost, 0.0)
        print_simulation_complete(len(self.transcript))
        return self.transcript
