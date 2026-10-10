#!/usr/bin/env python3
"""Model settings for every AI call in CI (the drift repair, the conflict repair and the
held-fix reviewer). The model is the AI_MODEL repo variable, as Aider names it:

- "anthropic/<id>" (the default, Claude Haiku 5.5): Anthropic's API with ANTHROPIC_API_KEY.
  Paid from the Claude Max plan's monthly API credit. Gets an explicit output budget, since
  Anthropic requires max_tokens and a model newer than Aider's model list would otherwise get
  a small default (and its adaptive thinking spends from the same budget), and no
  temperature (Haiku 5.5 rejects it).
- "openrouter/<id>": OpenRouter with OPENROUTER_API_KEY, plus provider routing. Tested
  2026-10-09 with the AI repair's exact request (~130k tokens): OpenInference answered as if no
  files were in the chat, so it is never used, and DeepInfra is preferred. Override with the
  AI_PROVIDER_ORDER / AI_PROVIDER_IGNORE repo variables (comma-separated).

    openrouter_provider.py json             -> OpenRouter routing {"order": [...], ...}
    openrouter_provider.py aider <model>    -> an Aider --model-settings-file for <model>
    openrouter_provider.py key-var <model>  -> the env var holding <model>'s API key
"""
import json
import os
import sys

ANTHROPIC_MAX_TOKENS = 32000


def provider() -> dict:
    def names(var, default):
        return [n.strip() for n in (os.environ.get(var) or default).split(',') if n.strip()]
    return {'order': names('AI_PROVIDER_ORDER', 'DeepInfra'),
            'ignore': names('AI_PROVIDER_IGNORE', 'OpenInference'),
            'allow_fallbacks': True}


def is_anthropic(model: str) -> bool:
    return model.startswith('anthropic/')


def key_var(model: str) -> str:
    return 'ANTHROPIC_API_KEY' if is_anthropic(model) else 'OPENROUTER_API_KEY'


def aider_settings(model: str) -> list:
    if is_anthropic(model):
        # use_temperature False: Claude Haiku 5.5 (and later models) reject `temperature`
        # ("deprecated for this model"), and Aider sends it unless told not to.
        return [{'name': model, 'edit_format': 'diff', 'use_temperature': False,
                 'extra_params': {'max_tokens': ANTHROPIC_MAX_TOKENS}}]
    # Aider passes extra_body straight into the OpenRouter request.
    return [{'name': model, 'extra_params': {'extra_body': {'provider': provider()}}}]


if __name__ == '__main__':
    cmd = sys.argv[1:2]
    if cmd == ['aider']:
        print(json.dumps(aider_settings(sys.argv[2])))
    elif cmd == ['key-var']:
        print(key_var(sys.argv[2]))
    else:
        print(json.dumps(provider()))
