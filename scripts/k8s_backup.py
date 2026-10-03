"""Quiesced PostgreSQL + uploads backup/restore. Configuration and referenced Secrets are saved in a private bundle."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import uuid

from k8s_common import Kubectl, add_cluster_args

APPS = ('finance-scraper-service', 'stt-ai-worker')
PVC = 'stt-uploads'
HELPER_IMAGE = 'busybox:1.37.0'
LOCK = 'finance-maintenance-lock'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate_bundle(directory):
    meta = json.loads((directory / 'metadata.json').read_text())
    if meta.get('format_version') != 1:
        raise ValueError('Unsupported backup format.')
    for name in ('postgres.dump', 'uploads.tar.gz'):
        path = directory / name
        if not path.is_file() or sha256(path) != meta['sha256'][name]:
            raise ValueError(f'Invalid backup checksum: {name}')
    if 'configuration.json' in meta['sha256']:
        if sha256(directory / 'configuration.json') != meta['sha256']['configuration.json']:
            raise ValueError('Invalid configuration checksum')
    with (directory / 'postgres.dump').open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise ValueError('Expected a PostgreSQL custom-format dump.')
    with tarfile.open(directory / 'uploads.tar.gz', 'r:gz') as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError(f'Unsafe upload archive entry: {member.name}')
    replicas = meta.get('replicas', {})
    if set(replicas) != set(APPS) or any(type(v) is not int or v < 0 or v > 1 for v in replicas.values()):
        raise ValueError('Backup must describe the two single-replica applications.')
    return meta


def stream_command(kube, args, *, output=None, source=None):
    # Stream large audio/dump files instead of keeping them in RAM.
    with (output.open('xb') if output else open(os.devnull, 'wb')) as sink:
        with (source.open('rb') if source else open(os.devnull, 'rb')) as stdin:
            result = subprocess.run([*kube.command, '--request-timeout=0', *args],
                                    env=kube.env, stdin=stdin, stdout=sink,
                                    stderr=subprocess.PIPE, timeout=1800)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace').strip() or 'Transfer failed.')


def app_state(kube):
    return {name: kube.get('deployment', name) for name in APPS}


def replicas_of(state):
    return {name: dep['spec'].get('replicas', 1) for name, dep in state.items()}


def pause(kube, state):
    print('Stopping app writers; PostgreSQL remains running.')
    for name in APPS:
        kube.run('scale', f'deployment/{name}', '-n', kube.namespace, '--replicas=0')
    for dep in state.values():
        selector = ','.join(f'{key}={value}' for key, value in dep['spec']['selector']['matchLabels'].items())
        pods = kube.run('get', 'pods', '-n', kube.namespace, '-l', selector, '-o', 'name').split()
        if pods:
            kube.run('wait', '--for=delete', *pods, '-n', kube.namespace,
                     '--timeout=180s', timeout=240)


def resume(kube, replicas):
    for name, count in replicas.items():
        kube.run('scale', f'deployment/{name}', '-n', kube.namespace, f'--replicas={count}')
    print('App replica counts restored. Check rollout status and readiness.')


@contextmanager
def files_pod(kube, readonly):
    name = 'finance-files-' + uuid.uuid4().hex[:12]
    pod_created = False
    try:
        pod = {'apiVersion': 'v1', 'kind': 'Pod',
               'metadata': {'name': name, 'namespace': kube.namespace,
                            'labels': {'finance.maintenance': 'files'}},
               'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                        'containers': [{'name': 'files', 'image': HELPER_IMAGE,
                                        'command': ['sh', '-c', 'sleep 7200'],
                                        'securityContext': {'allowPrivilegeEscalation': False,
                                                            'capabilities': {'drop': ['ALL']}},
                                        'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'},
                                                      'limits': {'memory': '256Mi'}},
                                        'volumeMounts': [{'name': 'uploads', 'mountPath': '/data/uploads',
                                                         'readOnly': readonly}]}],
                        'volumes': [{'name': 'uploads', 'persistentVolumeClaim':
                                     {'claimName': PVC, 'readOnly': readonly}}]}}
        pvc = kube.get('pvc', PVC)
        if not pvc.get('spec', {}).get('volumeName') and getattr(kube, 'overlay', 'homelab') == 'homelab':
            # The maintenance Pod may be the first consumer of a fresh local-path PVC.
            pod['spec']['nodeSelector'] = {'finance.node/role': 'gpu'}
        kube.run('create', '-f', '-', input=json.dumps(pod))
        pod_created = True
        kube.run('wait', '--for=condition=Ready', f'pod/{name}', '-n', kube.namespace,
                 '--timeout=180s', timeout=240)
        yield name
    finally:
        if pod_created:
            kube.run('delete', 'pod', name, '-n', kube.namespace, '--ignore-not-found',
                     '--wait=true', '--timeout=120s', timeout=180)


def database(kube, command, *, source=None, output=None):
    stream_command(kube, ['exec', '-i', '-n', kube.namespace, 'postgres-0', '-c', 'postgres',
                          '--', 'sh', '-c', command], source=source, output=output)


def configuration(kube, state):
    """Export only resources referenced by these workloads, with app writers paused."""
    workloads = [('Deployment', name, dep) for name,dep in state.items()]
    workloads.append(('StatefulSet', 'postgres', kube.get('statefulset', 'postgres')))
    resources, refs = [], set()
    for kind,name,workload in workloads:
        spec = json.loads(json.dumps(workload['spec']))
        if kind == 'Deployment':
            spec['replicas'] = 0
        resources.append({'apiVersion':'apps/v1', 'kind':kind,
                          'metadata':{'name':name,'namespace':kube.namespace},'spec':spec})
        pod = spec.get('template', {}).get('spec', {})
        for pull in pod.get('imagePullSecrets', []):
            refs.add(('Secret', pull['name']))
        for container in pod.get('containers', []):
            for item in container.get('envFrom', []):
                for key,resource in [('configMapRef','ConfigMap'),('secretRef','Secret')]:
                    if key in item:
                        refs.add((resource,item[key]['name']))
            for item in container.get('env', []):
                for key,resource in [('configMapKeyRef','ConfigMap'),('secretKeyRef','Secret')]:
                    if key in item.get('valueFrom', {}):
                        refs.add((resource,item['valueFrom'][key]['name']))
    for kind,name in sorted(refs):
        value = kube.get(kind.lower(), name)
        item = {'apiVersion':'v1','kind':kind,'metadata':{'name':name,'namespace':kube.namespace}, 'data':value.get('data',{})}
        if kind == 'Secret':
            item['type'] = value.get('type','Opaque')
        if value.get('binaryData'):
            item['binaryData'] = value['binaryData']
        resources.append(item)
    return {'apiVersion':'v1','kind':'List','items':resources}


def backup(kube, directory):
    # Never overwrite an earlier backup. Restrict access to financial/audio data.
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(directory, 0o700)
    with maintenance_lock(kube):
        state = app_state(kube)
        replicas = replicas_of(state)
        if any(count > 1 for count in replicas.values()):
            raise ValueError('Only single-replica deployments are supported.')
        try:
            pause(kube, state)
            with files_pod(kube, readonly=True) as helper:
                dump = directory / 'postgres.dump'
                uploads = directory / 'uploads.tar.gz'
                database(kube, 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-acl', output=dump)
                stream_command(kube, ['exec', '-n', kube.namespace, helper, '--', 'tar', '-czf', '-',
                                      '-C', '/data/uploads', '.'], output=uploads)
                config = directory / 'configuration.json'
                config.write_text(json.dumps(configuration(kube, state), indent=2))
                meta = {'format_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                        'namespace': kube.namespace, 'replicas': replicas,
                        'images': {name: dep['spec']['template']['spec']['containers'][0]['image']
                                   for name, dep in state.items()},
                        'sha256': {path.name: sha256(path) for path in (dump, uploads, config)}}
                # Presence of metadata.json marks a complete backup.
                (directory / 'metadata.json').write_text(json.dumps(meta, indent=2) + '\n')
                validate_bundle(directory)
        except BaseException:
            (directory / 'metadata.json').unlink(missing_ok=True)
            raise
        finally:
            resume(kube, replicas)
    print(f'Backup complete: {directory}')


def restore(kube, directory, replace_existing):
    meta = validate_bundle(directory)  # Must run before making any cluster changes.
    state = app_state(kube)
    started = False
    with maintenance_lock(kube):
        try:
            started = True
            pause(kube, state)
            with files_pod(kube, readonly=False) as helper:
                # Validate compatibility/structure with destination pg_restore before replacing anything.
                database(kube, 'exec pg_restore --list', source=directory / 'postgres.dump')
                if not replace_existing:
                    tables = kube.run('exec', '-n', kube.namespace, 'postgres-0', '-c', 'postgres', '--',
                                      'sh', '-c', 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN (\'pg_catalog\', \'information_schema\');"')
                    if int(tables.strip()) != 0:
                        raise ValueError('Destination DB has user tables. Use --replace-existing only for an intentional replacement.')
                    kube.run('exec', '-n', kube.namespace, helper, '--', 'sh', '-c',
                             'test -z "$(ls -A /data/uploads)" || { echo "Uploads are not empty; use --replace-existing for intentional replacement." >&2; exit 1; }')
                database(kube, 'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-acl --exit-on-error --single-transaction', source=directory / 'postgres.dump')
                if replace_existing:
                    kube.run('exec', '-n', kube.namespace, helper, '--', 'find', '/data/uploads',
                             '-mindepth', '1', '-maxdepth', '1', '-exec', 'rm', '-rf', '--', '{}', '+')
                stream_command(kube, ['exec', '-i', '-n', kube.namespace, helper, '--', 'tar', '-xzf', '-',
                                      '--no-same-owner', '-C', '/data/uploads'], source=directory / 'uploads.tar.gz')
        except BaseException:
            if started:
                for name in APPS:
                    try:
                        kube.run('scale', f'deployment/{name}', '-n', kube.namespace, '--replicas=0')
                    except (OSError, RuntimeError) as exc:
                        print(f'Could not stop {name}: {exc}', file=sys.stderr)
                print('Restore failed; apps are not resumed. Verify both are stopped, then repair/retry restoration.', file=sys.stderr)
            raise
        resume(kube, meta['replicas'])
    print('Restore complete. Verify DB, upload files and application readiness.')


# Hold the operation lock while writers pause, files transfer and apps resume.
@contextmanager
def maintenance_lock(kube):
    body = {'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': LOCK, 'namespace': kube.namespace},
            'data': {'started_at': datetime.now(timezone.utc).isoformat()}}
    kube.run('create', '-f', '-', input=json.dumps(body))
    try:
        yield
    finally:
        kube.run('delete', 'configmap', LOCK, '-n', kube.namespace, '--ignore-not-found')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_cluster_args(parser)
    parser.add_argument('--overlay', choices=['homelab', 'cpu'], default='homelab')
    parser.add_argument('action', choices=['backup', 'restore'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--replace-existing', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'restore':
        validate_bundle(args.directory)
    kube = Kubectl(args)
    kube.overlay = args.overlay
    kube.target()
    if args.action == 'backup':
        backup(kube, args.directory)
    else:
        restore(kube, args.directory, args.replace_existing)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        print(f'Failed: {exc}', file=sys.stderr)
        sys.exit(1)
