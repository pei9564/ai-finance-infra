"""Small kubectl adapter shared by the homelab administration commands."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess


def add_cluster_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--kubectl', default='kubectl')
    parser.add_argument('--kubeconfig', default='')
    parser.add_argument('--context', default='')
    parser.add_argument('--namespace', default='finance')


class Kubectl:
    def __init__(self, args: argparse.Namespace):
        self.command = ([args.kubectl] if Path(args.kubectl).is_file()
                        else shlex.split(args.kubectl))
        if args.context:
            self.command += ['--context', args.context]
        self.env = dict(os.environ)
        if args.kubeconfig:
            self.env['KUBECONFIG'] = args.kubeconfig
        self.namespace = args.namespace

    def run(self, *args: str, timeout: int = 30, input=None, binary=False):
        try:
            result = subprocess.run(
                [*self.command, '--request-timeout=20s', *args], env=self.env,
                input=input, capture_output=True, text=not binary, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'kubectl timed out after {timeout}s.') from exc
        if result.returncode:
            error = result.stderr.decode(errors='replace') if binary else result.stderr
            raise RuntimeError(error.strip() or f'kubectl exited {result.returncode}')
        return result.stdout

    def get(self, resource: str, name: str = '', namespaced: bool = True):
        args = ['get', resource]
        if name:
            args.append(name)
        if namespaced:
            args += ['-n', self.namespace]
        return json.loads(self.run(*args, '-o', 'json'))

    def target(self) -> None:
        text = self.run('config', 'view', '--minify', '-o',
                        'jsonpath=Context: {.contexts[0].name}{"\\n"}API server: {.clusters[0].cluster.server}{"\\n"}')
        if not text.strip():
            raise RuntimeError('No Kubernetes context selected.')
        print(text.strip())
        print(f'Namespace: {self.namespace}')
