import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import k8s_backup as tool


def bundle(directory, name='audio.wav', link=False):
    directory.mkdir(exist_ok=True)
    (directory / 'postgres.dump').write_bytes(b'PGDMP-test-data')
    with tarfile.open(directory / 'uploads.tar.gz', 'w:gz') as archive:
        member = tarfile.TarInfo(name)
        if link:
            member.type = tarfile.SYMTYPE
            member.linkname = '/tmp/external-file'
            archive.addfile(member)
        else:
            member.size = 5
            archive.addfile(member, io.BytesIO(b'audio'))
    metadata = {'format_version': 1, 'replicas': {name: 1 for name in tool.APPS},
                'sha256': {name: tool.sha256(directory / name) for name in ('postgres.dump', 'uploads.tar.gz')}}
    (directory / 'metadata.json').write_text(json.dumps(metadata))


class FakeKubectl:
    namespace = 'finance'

    def __init__(self, lock_exists=False, existing_tables='0'):
        self.calls = []
        self.lock_exists = lock_exists
        self.existing_tables = existing_tables

    def get(self, resource, name):
        return {'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': name}},
                         'template': {'spec': {'containers': [{'image': 'test:sha-abcdef0'}]}}}}

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if self.lock_exists and args[0] == 'create' and 'ConfigMap' in kwargs.get('input', ''):
            raise RuntimeError('Lock already exists')
        if args[0] == 'exec' and 'psql' in args[-1]:
            return self.existing_tables
        return ''


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'bundle'

    def test_corruption_and_archive_escape_rejected(self):
        for name, link in [('audio.wav', False), ('../escape', False), ('audio.wav', True)]:
            bundle(self.directory, name, link)
            if name == 'audio.wav' and not link:
                (self.directory / 'postgres.dump').write_bytes(b'corrupted')
            with self.assertRaises(ValueError):
                tool.validate_bundle(self.directory)

    def test_invalid_bundle_makes_no_cluster_calls(self):
        bundle(self.directory)
        (self.directory / 'uploads.tar.gz').write_bytes(b'broken')
        kube = FakeKubectl()
        with self.assertRaises(ValueError):
            tool.restore(kube, self.directory, False)
        self.assertEqual(kube.calls, [])

    def test_existing_lock_does_not_resume_or_scale_other_operation(self):
        kube = FakeKubectl(lock_exists=True)
        with self.assertRaises(RuntimeError):
            tool.backup(kube, self.directory)
        self.assertFalse(any(call[0] == 'scale' for call in kube.calls))
        self.assertFalse(any(call[0] == 'delete' for call in kube.calls))

    def test_failed_backup_resumes_apps_and_has_no_completion_marker(self):
        kube = FakeKubectl()
        with patch.object(tool, 'database', side_effect=RuntimeError('DB unavailable')), self.assertRaises(RuntimeError):
            tool.backup(kube, self.directory)
        self.assertFalse((self.directory / 'metadata.json').exists())
        resumes = [call for call in kube.calls if call[0] == 'scale' and '--replicas=1' in call]
        self.assertEqual(len(resumes), 2)

    def test_restore_failure_never_resumes_writers(self):
        bundle(self.directory)
        kube = FakeKubectl()
        with patch.object(tool, 'database', side_effect=RuntimeError('Invalid dump')), self.assertRaises(RuntimeError):
            tool.restore(kube, self.directory, True)
        self.assertFalse(any('--replicas=1' in call for call in kube.calls))
        self.assertTrue(any(call[0] == 'delete' and call[1] == 'pod' for call in kube.calls))

    def test_nonempty_database_refused_before_replacement(self):
        bundle(self.directory)
        kube = FakeKubectl(existing_tables='3')
        with patch.object(tool, 'database') as database, self.assertRaises(ValueError):
            tool.restore(kube, self.directory, False)
        self.assertEqual(database.call_count, 1)  # Only pg_restore --list, no restore.
        self.assertFalse(any('--replicas=1' in call for call in kube.calls))

    def test_successful_restore_resumes_snapshot_replica_counts(self):
        bundle(self.directory)
        kube = FakeKubectl()
        with patch.object(tool, 'database'), patch.object(tool, 'stream_command'):
            tool.restore(kube, self.directory, False)
        self.assertEqual(sum('--replicas=1' in call for call in kube.calls), 2)

    def test_successful_backup_produces_valid_bundle(self):
        kube = FakeKubectl()

        def dump(kube, command, output=None, **kwargs):
            output.write_bytes(b'PGDMP-test-data')

        def audio(kube, args, output=None, **kwargs):
            with tarfile.open(output, 'w:gz') as archive:
                member = tarfile.TarInfo('audio.wav')
                member.size = 5
                archive.addfile(member, io.BytesIO(b'audio'))

        with patch.object(tool, 'database', side_effect=dump), patch.object(tool, 'stream_command', side_effect=audio):
            tool.backup(kube, self.directory)
        self.assertEqual(tool.validate_bundle(self.directory)['format_version'], 1)
        self.assertEqual(sum('--replicas=1' in call for call in kube.calls), 2)

    def test_new_pvc_is_bound_on_gpu_but_existing_pvc_keeps_its_node(self):
        for bound in (False, True):
            kube = FakeKubectl()
            body = []
            original = kube.run

            def capture(*args, **kwargs):
                if args[0] == 'create':
                    body.append(json.loads(kwargs['input']))
                return original(*args, **kwargs)

            with patch.object(kube, 'run', side_effect=capture), patch.object(kube, 'get', return_value={'spec': {'volumeName': 'existing-pv'} if bound else {}}):
                with tool.files_pod(kube, True):
                    pass
            if bound:
                self.assertNotIn('nodeSelector', body[0]['spec'])
            else:
                self.assertEqual(body[0]['spec']['nodeSelector'], {'finance.node/role': 'gpu'})

    def test_backup_does_not_overwrite_existing_bundle(self):
        bundle(self.directory)
        kube = FakeKubectl()
        with self.assertRaises(FileExistsError):
            tool.backup(kube, self.directory)
        self.assertEqual(kube.calls, [])


if __name__ == '__main__':
    unittest.main()
