"""Validate application version pins in rendered Kustomize manifests, offline."""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

APPS = {'ghcr.io/pei9564/finance-scraper-service', 'ghcr.io/pei9564/stt-ai-worker'}


def validate_images(rendered: str):
    found = set()
    for line in rendered.splitlines():
        match = re.match(r'^\s*image:\s*[\"\']?([^\s\"\']+)', line)
        if not match:
            continue
        image = match[1]
        for app in APPS:
            if image.startswith(app + ':') or image.startswith(app + '@') or image == app:
                found.add(app)
                if not re.fullmatch(re.escape(app) + r'(?::sha-[0-9a-f]{7,40}|@sha256:[0-9a-f]{64})', image):
                    raise ValueError(f'Unpinned application image: {image}; use sha-<commit> or a digest.')
    if found != APPS:
        raise ValueError('Missing application images: ' + ', '.join(sorted(APPS - found)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubectl', default='kubectl')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    command = [args.kubectl] if Path(args.kubectl).is_file() else shlex.split(args.kubectl)
    for overlay in ('homelab', 'cpu'):
        rendered = subprocess.check_output([*command, 'kustomize', str(args.root / 'k8s/overlays' / overlay)],
                                          env={**os.environ, 'KUBECONFIG': '/dev/null'}, text=True)
        validate_images(rendered)
        print(f'{overlay}: application image pins verified.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
