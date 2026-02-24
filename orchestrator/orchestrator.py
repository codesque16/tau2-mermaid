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

    async def run(self) -> list[Message]:
        initial = self.config.initial_message
        has_initial = bool(initial and initial.strip())

        if has_initial:
            current_message = initial.strip()
            self.transcript.append(Message(role=Role.USER, content=current_message))
            print_role_header("User", seed=True)
            print_markdown(current_message)
        else:
            current_message = "Begin the conversation."

        assistant_total_cost = 0.0
        user_total_cost = 0.0

        for turn in range(self.config.max_turns):
            # Assistant responds (streaming)
            await self.bus.send_to_assistant(current_message)
            print_role_header("Assistant", turn=turn + 1)
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

            # User agent responds (streaming); on first turn with no initial message, this is the first user message
            await self.bus.send_to_user(assistant_reply)
            print_role_header("User", turn=turn + 1)
            if self.user.use_streaming_display:
                with StreamingDisplay() as display:

                    async def on_chunk_user(ev: str, data: object) -> None:
                        await _stream_chunk(display, ev, data)

                    user_reply, usage_info_user = await self.user.respond_stream(
                        assistant_reply, on_chunk=on_chunk_user
                    )
                    display.finish()
            else:
                # Human agent: don't print "text" chunk — user already saw their input when typing
                async def on_chunk_user(ev: str, data: object) -> None:
                    if ev == "tool_use" and isinstance(data, dict):
                        print_tool_call(
                            name=data.get("name", ""),
                            tool_id=data.get("id", ""),
                            input_data=data.get("input"),
                        )

                user_reply, usage_info_user = await self.user.respond_stream(
                    assistant_reply, on_chunk=on_chunk_user
                )

            u_user = usage_info_user.get("usage")
            c_user = usage_info_user.get("cost")
            if c_user is not None:
                user_total_cost += c_user
            print_turn_cost("User", u_user, c_user)

            self.transcript.append(Message(role=Role.USER, content=user_reply))

            if self._check_stop(user_reply):
                print_stop_phrase("Stop phrase detected in user reply. Goal achieved!")
                break

            current_message = user_reply
        else:
            print_max_turns_reached(self.config.max_turns)

        print_total_cost(assistant_total_cost, user_total_cost)
        print_simulation_complete(len(self.transcript))
        return self.transcript
