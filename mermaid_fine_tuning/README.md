# SOP Agent — Gemma 4 Fine-tuning Pipeline

End-to-end pipeline for training a Gemma 4 model to follow Mermaid-based SOP graphs with structured two-stage thinking.

## Structure

```
sop_agent/
├── config/                    # Schemas, constants, prompts
│   ├── schemas.py             # Pydantic models for graphs, plans, traces
│   ├── prompts.py             # Gemini generation prompts for each stage
│   └── retail_sop.py          # The anchor retail SOP as a Python constant
├── data_gen/                  # Dataset generation pipeline
│   ├── stage1_graphs.py       # Generate SOP graphs across domains
│   ├── stage2_plans.py        # Generate trajectory plans per graph
│   ├── stage3_conversations.py# Realize plans as full conversations
│   ├── stage4_traces.py       # Template structured traces (deterministic)
│   ├── stage5_continuations.py# Generate optional free-form continuations
│   └── run_pipeline.py        # Orchestrator: runs all stages
├── utils/
│   ├── mermaid_parser.py      # Parse Mermaid flowcharts → graph structure
│   ├── validators.py          # Graph, plan, turn validators
│   ├── gemini_client.py       # Wrapper around Gemini API with retries
│   └── templating.py          # Structured-trace templating
├── training/
│   ├── format_dataset.py      # Convert pipeline output → Gemma chat format
│   ├── train_sft.py           # QLoRA SFT training script
│   └── train_config.yaml      # Training hyperparameters
├── evaluation/
│   ├── eval_harness.py        # Run model against eval set
│   ├── metrics.py             # Per-skill metrics (node accuracy, etc.)
│   └── gold_trajectories.py   # Hand-written eval trajectories
└── README.md
```

## Usage

```bash
# 1. Generate dataset (run in batches; review between)
python -m data_gen.run_pipeline --batch_size 20 --output data/batch1.jsonl

# 2. Format for training
python -m training.format_dataset --input data/batch3.jsonl --output data/train.jsonl

# 3. Baseline eval (before fine-tuning)
python -m evaluation.eval_harness --model google/gemma-4-26B-a4b-it --output results/baseline.json

# 4. Fine-tune
python -m training.train_sft --config training/train_config.yaml

# 5. Eval fine-tuned model
python -m evaluation.eval_harness --model ./checkpoints/final --output results/finetuned.json
```

## Environment

```bash
pip install google-genai pydantic transformers peft trl bitsandbytes accelerate datasets
```

Set `GEMINI_API_KEY` env var before running generation.
