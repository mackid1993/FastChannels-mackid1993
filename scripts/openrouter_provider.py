#!/usr/bin/env python3
"""The OpenRouter provider routing every AI call in CI uses (the drift repair, the conflict
repair and the held-fix reviewer).

Tested 2026-10-09 with the AI repair's exact request (~130k tokens, a renamed upstream
function): OpenInference answered as if no files were in the chat, on every try, while
DeepInfra read everything and wrote the correct fix (Z.AI, Fireworks, Together and Novita
also read the files). So OpenInference is never used, DeepInfra is preferred, and other
providers are only a fallback when DeepInfra is down.

Override with the AI_PROVIDER_ORDER / AI_PROVIDER_IGNORE repo variables (comma-separated).

    openrouter_provider.py json             -> {"order": [...], "ignore": [...], ...}
    openrouter_provider.py aider <model>    -> an Aider --model-settings-file for <model>
"""
import json
import os
import sys


def provider() -> dict:
    def names(var, default):
        return [n.strip() for n in (os.environ.get(var) or default).split(',') if n.strip()]
    return {'order': names('AI_PROVIDER_ORDER', 'DeepInfra'),
            'ignore': names('AI_PROVIDER_IGNORE', 'OpenInference'),
            'allow_fallbacks': True}


if __name__ == '__main__':
    if sys.argv[1:2] == ['aider']:
        # Aider passes extra_body straight into the OpenRouter request.
        print(json.dumps([{'name': sys.argv[2], 'extra_params': {'extra_body': {'provider': provider()}}}]))
    else:
        print(json.dumps(provider()))
