"""Build a Codex model catalog from the loopback gateway's model metadata."""
import argparse
import copy
import json
import os
from pathlib import Path
import tempfile
import urllib.request


def build_catalog(payload):
    models = copy.deepcopy(payload.get('models'))
    if not isinstance(models, list) or not models:
        raise ValueError('gateway returned an empty model catalog')
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get('slug'), str) or not model['slug']:
            raise ValueError('gateway returned invalid model metadata')
        # Enable Codex's native patch tool now that the gateway bridges it.
        model['apply_patch_tool_type'] = 'freeform'
        model['shell_type'] = 'unified_exec'
        model['model_messages'] = {
            'instructions_template': model.get('base_instructions', ''),
            'instructions_variables': None,
        }
    return {'models': models}


def write_catalog(path, payload):
    content = json.dumps(build_catalog(payload), indent=2) + '\n'
    fd, temporary = tempfile.mkstemp(prefix='.gcodex-models-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('port', type=int)
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError('invalid port')
        # Bypass proxy environment variables for this strictly local request.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{args.port}/v1/models', timeout=5) as response:
            payload = json.load(response)
        write_catalog(args.path, payload)
        print('model_catalog_json=' + json.dumps(str(args.path.resolve())))
    except (OSError, ValueError) as error:
        parser.exit(1, f'gcodex: cannot build model catalog: {error}\n')
