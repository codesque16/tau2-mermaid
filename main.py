import argparse
import asyncio
import os
import sys
from pathlib import Path

import logfire
from dotenv import load_dotenv

from agent import create_agent
from agent.config import AgentConfig as AgentAgentConfig
from chat.config import AgentConfig as ChatAgentConfig, load_simulation_config
from orchestrator.event_bus import EventBus
from orchestrator.orchestrator import Orchestrator


def _agent_type_from_model(model: str) -> str:
    """Infer agent type from model name."""
    m = model.lower()
    if "gemini" in m:
        return "gemini"
    if m.startswith("gpt-"):
        return "openai"
    return "anthropic"


def _to_agent_config(loaded: ChatAgentConfig) -> AgentAgentConfig:
    """Convert chat AgentConfig to agent package AgentConfig."""
    return AgentAgentConfig(
        system_prompt=loaded.system_prompt,
        max_tokens=loaded.max_tokens,
        temperature=loaded.temperature,
    )


async def run(config_path: Path) -> None:
    config = load_simulation_config(config_path)

    # Assistant agent: resolve type and model; when type is litellm, require assistant_model or model
    assistant_model = config.assistant_model or config.model
    assistant_agent_type = config.assistant_agent_type or _agent_type_from_model(
        assistant_model
    )
    if assistant_agent_type == "litellm" and not (
        assistant_model and str(assistant_model).strip()
    ):
        raise ValueError(
            "When assistant_agent_type is 'litellm', either assistant_model or model must be set in config."
        )

    # User agent: resolve type and model; when type is litellm, require user_model or model
    user_model = config.user_model or config.model
    user_agent_type = config.user_agent_type or _agent_type_from_model(user_model)
    if user_agent_type == "litellm" and not (user_model and str(user_model).strip()):
        raise ValueError(
            "When user_agent_type is 'litellm', either user_model or model must be set in config."
        )

    bus = EventBus()
    assistant = create_agent(
        assistant_agent_type,
        "assistant",
        _to_agent_config(config.assistant),
        assistant_model,
    )
    user = create_agent(
        user_agent_type,
        "user",
        _to_agent_config(config.user),
        user_model,
    )

    orchestrator = Orchestrator(assistant, user, bus, config)
    transcript = await orchestrator.run()

    print(f"\n{'='*60}")
    print(f"Simulation complete. Total messages: {len(transcript)}")


def main() -> None:
    load_dotenv()
    logfire.configure()

    # Initialize Logfire LLM instrumentation (see logfire.pydantic.dev/docs/integrations/llms/)
    # Each call instruments the corresponding SDK so traces show provider, model, tokens, and cost.
    #logfire.instrument_openai()      # OpenAI (gpt-4o, gpt-4o-mini, etc.)
    #logfire.instrument_anthropic()   # Anthropic (Claude)
    os.environ.setdefault(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true"
    )  # capture prompts/completions for Gemini in Logfire
    #logfire.instrument_google_genai() # Google Gen AI (Gemini)
    logfire.instrument_litellm()      # LiteLLM (unified client; can use any of the above via config)

    parser = argparse.ArgumentParser(description="Run a tau2-mermaid simulation.")
    parser.add_argument(
        "config",
        nargs="?",
        default="configs/simulation.yaml",
        help="Path to simulation YAML config",
    )
    parser.add_argument(
        "-n",
        "--name",
        default=None,
        help="Name for this simulation (used as the top-level Logfire span name)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"Error: config file '{config_path}' not found.")
        sys.exit(1)

    simulation_name = args.name or config_path.stem

    with logfire.span(simulation_name):
        asyncio.run(run(config_path))


if __name__ == "__main__":
    main()
