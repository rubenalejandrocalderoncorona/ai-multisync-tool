"""The judge model for the draft evaluator (and, through generate_classification, any Phoenix classification evaluator): the pipeline's own client on its cheap tier (DeepSeek deepseek-v4-pro by default, keys from the environment),
shaped like the `phoenix.evals.LLM` the classification evaluators call. No second model SDK is installed for it."""
from __future__ import annotations

from ..llm import LLM


def _messages(prompt) -> list[dict]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    return [{"role": str(getattr(m["role"], "value", m["role"])), "content": m["content"] if isinstance(m["content"], str) else str(m["content"])} for m in prompt]


class PipelineJudge:
    def __init__(self, llm: LLM, tier: str = "cheap"):
        self.llm, self.tier = llm, tier
        self.model = llm.tiers[tier]["model"]

    def complete(self, prompt) -> str:
        """The raw text answer of the judge tier (temperature 0). The faithfulness prompt asks for strict JSON; draft_evals.parse_verdict reads it."""
        return self.llm.chat(_messages(prompt), tier=self.tier, temperature=0)

    def generate_classification(self, prompt, labels, include_explanation: bool = True, description: str | None = None, **_ignored) -> dict:
        """{"label": one of labels, "explanation": ...}. `labels` is a list or a {label: description} dict, as the evaluator passes it."""
        names = list(labels)
        shape = '{"label": "<one of: ' + " | ".join(names) + '>"' + (', "explanation": "<one or two sentences>"' if include_explanation else "") + "}"
        messages = _messages(prompt)
        messages[-1] = {**messages[-1], "content": f"{messages[-1]['content']}\n\nAnswer with one JSON object only, no other text: {shape}"}
        out = self.llm.chat_json(messages, tier=self.tier, temperature=0)
        label = str(out.get("label", "")).strip().lower() if isinstance(out, dict) else ""
        if label not in names:
            raise ValueError(f"judge answered {label!r}, expected one of {names}")
        return {"label": label, **({"explanation": out.get("explanation")} if include_explanation else {})}
