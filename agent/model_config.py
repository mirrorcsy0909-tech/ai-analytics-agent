"""One model configuration shared by the reference Agent and the workflow."""

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


def build_model(*, timeout=30, max_retries=0, max_tokens=3000):
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    model = os.getenv("OPENAI_MODEL", "openai:gpt-5.5").removeprefix("openai:")
    return ChatOpenAI(
        model=model,
        use_responses_api=True,
        reasoning={"effort": os.getenv("OPENAI_REASONING_EFFORT", "medium")},
        # Carry reasoning items through tool turns without server-side storage.
        include=["reasoning.encrypted_content"],
        store=False,
        timeout=timeout,
        max_retries=max_retries,
        max_tokens=max_tokens,
    )
