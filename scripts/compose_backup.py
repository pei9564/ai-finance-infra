"""Consistent Compose backup, guarded restore, and an isolated PostgreSQL restore drill."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import subprocess
import tarfile
import tempfile
import time
import uuid

APPS = ('finance-scraper-service', 'stt-ai-worker')


def run(args, *, source=None, output=None):
    with (source.open('rb') if source else open(os.devnull, 'rb')) as stdin:
        with (output.open('xb') if output else tempfile.TemporaryFile()) as stdout:
            result = subprocess.run(args, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, timeout=1800)
            if result.returncode:
                raise RuntimeError(f'{args[0]} command failed (exit {result.returncode}); inspect the target service logs.')
            if output:
                return ''
            stdout.seek(0)
            return stdout.read().decode().strip()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive_files(path):
    files = {}
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe backup archive entry')
            if member.isfile():
                normalized = str(name)
                if normalized in files:
                    raise ValueError('Duplicate backup archive entry')
                stream = archive.extractfile(member)
                files[normalized] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return files


def config_archive(root, target, env_file):
    paths = [p for p in root.glob('docker-compose*.yml') if p.is_file()]
    paths += [root / 'Makefile']
    paths += [p for p in (root / 'scripts').glob('*.py') if p.is_file()]
    paths += [p for p in (root / 'k8s').rglob('*') if p.is_file()]
    with tarfile.open(target, 'x:gz') as archive:
        for path in paths:
            if path.is_symlink():
                raise ValueError('Configuration symlinks are not supported')
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
        if env_file.is_file():
            if env_file.is_symlink():
                raise ValueError('Environment file must not be a symlink')
            archive.add(env_file, arcname='.env', recursive=False)


def validate(directory):
    meta = json.loads((directory / 'metadata.json').read_text())
    if meta.get('format_version') != 'compose-v1':
        raise ValueError('Unsupported Compose backup format')
    for name in ('postgres.dump', 'uploads.tar.gz', 'configuration.tar.gz'):
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != meta['sha256'][name]:
            raise ValueError(f'Backup checksum mismatch: {name}')
    with (directory / 'postgres.dump').open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise ValueError('Invalid PostgreSQL dump')
    if archive_files(directory / 'uploads.tar.gz') != meta['upload_files']:
        raise ValueError('Upload inventory mismatch')
    archive_files(directory / 'configuration.tar.gz')
    return meta


def db(container, command, **kwargs):
    return run(['docker', 'exec', '-i', container, 'sh', '-c', command], **kwargs)


def sql(container, query):
    return run(['docker', 'exec', container, 'sh', '-c',
                'exec psql -XqAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"', 'sql', query])


def inventory(container):
    tables = sql(container, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").splitlines()
    result = {}
    for table in tables:
        name = '"' + table.replace('"', '""') + '"'
        value = sql(container, f"SET timezone='UTC'; SELECT count(*),md5(COALESCE(string_agg(md5(t::text), '' ORDER BY md5(t::text)),'')) FROM public.{name} t")
        result[table] = value
    return result


class Compose:
    def __init__(self, root, env_file):
        self.root, self.env_file = root, env_file
        self.command = ['docker', 'compose', '--project-directory', str(root), '--env-file', str(env_file), '-f', str(root / 'docker-compose.yml')]

    def containers(self):
        data = {}
        for service in ('postgres', *APPS, 'watchtower'):
            ids = run([*self.command, 'ps', '-aq', service]).splitlines()
            if not ids:
                if service == 'watchtower':
                    continue
                raise ValueError(f'Create the {service} container before this operation')
            if len(ids) != 1:
                raise ValueError('Only a single container per service is supported')
            data[service] = json.loads(run(['docker', 'inspect', ids[0]]))[0]
        if not data['postgres']['State']['Running']:
            raise ValueError('PostgreSQL must be running')
        return data


@contextmanager
def lock(root):
    with (root / '.backup.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def pause(state):
    for name in ('watchtower', *APPS):
        if name in state and state[name]['State']['Running']:
            run(['docker', 'stop', '--time', '30', state[name]['Id']])


def resume(state):
    for name in (*APPS, 'watchtower'):
        if name in state and state[name]['State']['Running']:
            run(['docker', 'start', state[name]['Id']])


def files(state, args, *, writable=False, **kwargs):
    image = state['postgres']['Config']['Image']
    name = 'finance-files-' + uuid.uuid4().hex[:12]
    try:
        return run(['docker', 'run', '--rm', '--name', name, '--network', 'none', '-i',
                    '--volumes-from', state['stt-ai-worker']['Id'] + (':rw' if writable else ':ro'),
                    '--entrypoint', args[0], image, *args[1:]], **kwargs)
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def revision(root):
    try:
        return run(['git','-C',str(root),'rev-parse','HEAD'])
    except (RuntimeError,OSError):
        return None


def backup(compose, directory):
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    with lock(compose.root):
        state = compose.containers()
        try:
            pause(state)
            db(state['postgres']['Id'], 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-acl', output=directory / 'postgres.dump')
            files(state, ['tar', '-czf', '-', '-C', '/data/uploads', '.'], output=directory / 'uploads.tar.gz')
            config_archive(compose.root, directory / 'configuration.tar.gz', compose.env_file)
            meta = {'format_version':'compose-v1', 'created_at':datetime.now(timezone.utc).isoformat(),
                    'images':{name:value['Config']['Image'] for name,value in state.items()},
                    'image_ids':{name:value['Image'] for name,value in state.items()},
                    'revisions':{name:revision(compose.root.parent/name) for name in ('ai-finance-infra',*APPS)},
                    'database':inventory(state['postgres']['Id']),
                    'upload_files':archive_files(directory / 'uploads.tar.gz'),
                    'sha256':{name:digest(directory / name) for name in ('postgres.dump','uploads.tar.gz','configuration.tar.gz')}}
            (directory / 'metadata.json').write_text(json.dumps(meta, indent=2))
            validate(directory)
        except BaseException:
            (directory / 'metadata.json').unlink(missing_ok=True)
            raise
        finally:
            resume(state)
    print(f'Backup complete: {directory}')


def restore(compose, directory, replace_existing):
    meta = validate(directory)
    with lock(compose.root):
        state = compose.containers()
        changed = False
        try:
            pause(state)
            database = state['postgres']['Id']
            db(database, 'exec pg_restore --list', source=directory / 'postgres.dump')
            if not replace_existing:
                if inventory(database) or files(state, ['find', '/data/uploads', '-mindepth', '1', '-maxdepth', '1']):
                    raise ValueError('Destination is not empty; explicit --replace-existing is required')
            changed = True
            db(database, 'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-acl --exit-on-error --single-transaction', source=directory / 'postgres.dump')
            files(state, ['find', '/data/uploads', '-mindepth','1','-maxdepth','1','-exec','rm','-rf','--','{}','+'], writable=True)
            files(state, ['tar', '-xzf','-','--no-same-owner','-C','/data/uploads'], source=directory / 'uploads.tar.gz', writable=True)
            if inventory(database) != meta['database']:
                raise ValueError('Restored database does not match backup inventory')
        except BaseException:
            if changed:
                print('Restore failed after writes started. Applications and Watchtower remain stopped; repair before resuming.')
            else:
                resume(state)
            raise
        resume(state)
    print('Restore complete. Configuration archive is preserved separately; it never overwrites .env automatically.')


def drill(directory):
    """Restore to a throwaway database and verify every table and audio file."""
    meta = validate(directory)
    name = 'finance-restore-drill-' + uuid.uuid4().hex[:12]
    try:
        run(['docker','run','-d','--name',name,'--network','none',
             '-e','POSTGRES_USER=drill','-e','POSTGRES_DB=drill','-e','POSTGRES_PASSWORD='+secrets.token_hex(24), 'postgres:15-alpine'])
        deadline = time.monotonic()+60
        while True:
            probe = subprocess.run(['docker','exec',name,'pg_isready','-h','127.0.0.1','-U','drill','-d','drill'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                break
            if time.monotonic()>deadline:
                raise RuntimeError('Drill PostgreSQL startup timed out')
            time.sleep(.25)
        db(name,'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl --exit-on-error --single-transaction', source=directory / 'postgres.dump')
        if inventory(name) != meta['database']:
            raise ValueError('Restored database inventory mismatch')
        with tempfile.TemporaryDirectory(prefix='finance-restore-') as folder:
            root=Path(folder)
            with tarfile.open(directory / 'uploads.tar.gz') as archive:
                archive.extractall(root, filter='data')
            actual={str(path.relative_to(root)):digest(path) for path in root.rglob('*') if path.is_file()}
            if actual != meta['upload_files']:
                raise ValueError('Restored audio content mismatch')
        report={'verified_at':datetime.now(timezone.utc).isoformat(), 'tables_verified':len(meta['database']),
                'audio_files_verified':len(meta['upload_files']), 'status':'passed'}
        (directory/'restore-drill.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report))
    finally:
        subprocess.run(['docker','rm','-fv',name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','restore','verify','drill'])
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--replace-existing',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    if args.action=='verify':validate(args.directory);print('Backup checksums and archive entries verified.')
    elif args.action=='drill':drill(args.directory)
    else:
        compose=Compose(args.root.resolve(),(args.env_file or args.root/'.env').resolve())
        if args.action=='backup':backup(compose,args.directory)
        else:restore(compose,args.directory,args.replace_existing)


if __name__=='__main__':
    main()
