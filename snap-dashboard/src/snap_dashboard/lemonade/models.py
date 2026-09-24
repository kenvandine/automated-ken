"""Opinionated default models for each Lemonade task type.

Tuned for the reference target hardware — an AMD Strix Halo machine with
128GB of unified RAM — where large mixture-of-experts GGUF models fit
comfortably and run fast thanks to their small active-parameter count,
even without a discrete GPU. Every model listed here is present in
Lemonade's supported llamacpp model catalog, and is pulled automatically
(see ``lemonade.embedded.EmbeddedLemonadeManager``) the first time it's
needed so users never have to manually run ``lemonade-server pull``.

These are only *defaults*: a user can still override the model for all
tasks via ``UserConfig.lemonade_model`` (Settings -> Agents & AI).
"""

from __future__ import annotations

#: Task identifiers used throughout the codebase to select a model.
TASK_VISION = "vision"
TASK_TEXT = "text"
TASK_CODING = "coding"

# Vision: screenshot/regression review (screenshot_reviewer.py,
# test_run_auto_promoter.py) needs a capable multimodal model. Qwen3-VL-8B
# is a strong, modern vision-language model that's still small enough to
# warm up quickly.
_VISION_MODEL = "Qwen3-VL-8B-Instruct-GGUF"

# Text: general reasoning / drafting (PR descriptions, summaries). A
# mixture-of-experts model gives strong quality with only ~3B active
# parameters per token, so it stays fast on Strix Halo's iGPU/NPU despite
# its large total size fitting easily in 128GB of unified RAM.
_TEXT_MODEL = "Qwen3.6-35B-A3B-GGUF"

# Coding: reserved for the "local_lemonade" coding-task backend (see
# agents/coding_backend.py) once local models are capable enough to take
# over from the GitHub Copilot cloud agent. Qwen3-Coder is purpose-built
# for code generation/editing and, as a 30B-A3B MoE model, is likewise
# fast on unified memory.
_CODING_MODEL = "Qwen3-Coder-30B-A3B-Instruct-GGUF"

#: Map of task identifier -> opinionated default model name.
TASK_MODELS: dict[str, str] = {
    TASK_VISION: _VISION_MODEL,
    TASK_TEXT: _TEXT_MODEL,
    TASK_CODING: _CODING_MODEL,
}

# Context window (in tokens) to load each opinionated model with. Set
# explicitly via the ``/v1/load`` ``ctx_size`` parameter (persisted with
# ``save_options: true``) rather than relying on each model's often-small
# built-in default, since 128GB of unified RAM comfortably affords much
# larger contexts than a typical consumer GPU box would:
#  - vision: screenshot pairs + prompt fit well within a moderate window.
#  - text: PR descriptions can quote lengthy upstream release notes.
#  - coding: full file diffs/multi-file context need the most headroom.
TASK_CONTEXT_SIZES: dict[str, int] = {
    TASK_VISION: 8192,
    TASK_TEXT: 32768,
    TASK_CODING: 65536,
}

#: Default task when none is specified.
DEFAULT_TASK = TASK_TEXT


def default_model_for(task: str) -> str:
    """Return the opinionated default model for ``task``, falling back to text."""
    return TASK_MODELS.get(task, TASK_MODELS[DEFAULT_TASK])


def default_context_for(task: str) -> int:
    """Return the opinionated context size (tokens) for ``task``, falling back to text."""
    return TASK_CONTEXT_SIZES.get(task, TASK_CONTEXT_SIZES[DEFAULT_TASK])
