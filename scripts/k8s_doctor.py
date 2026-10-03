"""Read-only preflight; never prints Secret values or kubeconfig credentials."""
import argparse
import sys
from k8s_common import Kubectl, add_cluster_args


def ready(node):
    return any(c['type'] == 'Ready' and c['status'] == 'True'
               for c in node.get('status', {}).get('conditions', []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_cluster_args(parser)
    parser.add_argument('--overlay', choices=['homelab', 'cpu'], default='homelab')
    args = parser.parse_args()
    kube = Kubectl(args)
    failures = []

    def check(label, action):
        try:
            detail = action()
            print(f'OK   {label}: {detail}')
        except (RuntimeError, ValueError, KeyError, OSError) as exc:
            failures.append(label)
            print(f'FAIL {label}: {exc}')

    check('Target', lambda: kube.target() or args.overlay)
    nodes = []

    def check_nodes():
        nodes.extend(kube.get('nodes', namespaced=False)['items'])
        available = [n for n in nodes if ready(n) and not n.get('spec', {}).get('unschedulable')]
        if not available:
            raise ValueError('No Ready, schedulable node; check K3s and Tailscale.')
        if args.overlay == 'homelab':
            for role in ('data', 'gpu'):
                matches = [n for n in available if n['metadata'].get('labels', {}).get('finance.node/role') == role]
                if not matches:
                    raise ValueError(f'No Ready node with finance.node/role={role}.')
                if role == 'gpu' and not any(int(n.get('status', {}).get('allocatable', {}).get('nvidia.com/gpu', 0)) >= 1 for n in matches):
                    raise ValueError('GPU node has no allocatable NVIDIA GPU; install runtime/device plugin.')
        return ', '.join(n['metadata']['name'] for n in available)

    check('Nodes / GPU', check_nodes)
    if args.overlay == 'homelab':
        check('NVIDIA runtime', lambda: kube.get('runtimeclass', 'nvidia', False)['handler'])
    check('StorageClass', lambda: kube.get('storageclass', 'local-path', False)['provisioner'])
    check('Namespace', lambda: kube.get('namespace', args.namespace, False)['metadata']['name'])

    def app_secret():
        # Fetch only type and key names, never Secret data.
        template = '{{.type}}{{"\\n"}}{{range $key, $value := .data}}{{$key}}{{"\\n"}}{{end}}'
        lines = kube.run('get', 'secret', 'app-secrets', '-n', args.namespace,
                         '-o', f'go-template={template}').splitlines()
        required = {'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB', 'GITHUB_TOKEN', 'VLLM_API_KEY'}
        missing = required - set(lines[1:])
        if missing:
            raise ValueError('Missing keys: ' + ', '.join(sorted(missing)))
        if not lines or lines[0] != 'Opaque':
            raise ValueError('app-secrets must have type Opaque.')
        return 'Required keys present (values not inspected).'

    check('Application Secret', app_secret)

    def pull_secret():
        secret_type = kube.run('get', 'secret', 'ghcr-cred', '-n', args.namespace,
                               '-o', 'jsonpath={.type}')
        if secret_type != 'kubernetes.io/dockerconfigjson':
            raise ValueError('ghcr-cred must have type kubernetes.io/dockerconfigjson.')
        return 'Present; registry login/image availability still require deployment verification.'

    check('GHCR pull Secret', pull_secret)

    def volumes():
        pvcs = kube.get('pvc')['items']
        expected = {'pg-data-postgres-0', 'stt-uploads'}
        found = {p['metadata']['name']: p for p in pvcs if p['metadata']['name'] in expected}
        for name, pvc in found.items():
            if pvc['status'].get('phase') != 'Bound':
                print(f'WARN PVC {name}: {pvc["status"].get("phase", "unknown")} (may wait for its first Pod).')
        missing = expected - set(found)
        return ('Not created yet: ' + ', '.join(sorted(missing)) if missing
                else ', '.join(f'{name}={pvc["status"].get("phase", "unknown")}' for name, pvc in found.items()))

    check('Persistent volumes', volumes)
    print(f'\nPreflight: {len(failures)} failed checks. Read-only; cross-node networking and application behavior are not tested.')
    return bool(failures)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'FAIL {exc}', file=sys.stderr)
        sys.exit(1)
