import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import compose_backup as tool


class ComposeBackupTests(unittest.TestCase):
    def test_archive_rejects_paths_and_links(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'files.tar.gz'
            for name,link in [('../outside',False),('/absolute',False),('link',True)]:
                with tarfile.open(path,'w:gz') as tar:
                    member=tarfile.TarInfo(name)
                    if link:member.type=tarfile.SYMTYPE;member.linkname='target'
                    else:member.size=1
                    tar.addfile(member,None if link else io.BytesIO(b'a'))
                with self.assertRaises(ValueError):tool.archive_files(path)

    def test_configuration_keeps_environment_private_in_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'k8s').mkdir();(root/'Makefile').write_text('test')
            env=root/'.env';env.write_text('TOKEN=fixture-only')
            bundle=root/'config.tar.gz';tool.config_archive(root,bundle,env)
            self.assertIn('.env',tool.archive_files(bundle))

    def test_invalid_bundle_never_touches_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'metadata.json').write_text(json.dumps({'format_version':'wrong'}))
            with patch.object(tool.Compose,'containers') as target,self.assertRaises(ValueError):
                tool.restore(tool.Compose(root,root/'.env'),root,False)
            target.assert_not_called()

    def test_partial_restore_keeps_applications_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);compose=tool.Compose(root,root/'.env');state={'postgres':{'Id':'db'}}
            with patch.object(tool,'validate',return_value={}),patch.object(compose,'containers',return_value=state),patch.object(tool,'pause'),patch.object(tool,'resume') as resume,patch.object(tool,'db',side_effect=['',RuntimeError('failed')]),self.assertRaises(RuntimeError):
                tool.restore(compose,root,True)
            resume.assert_not_called()

    def test_nonempty_target_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);compose=tool.Compose(root,root/'.env');state={'postgres':{'Id':'db'}}
            with patch.object(tool,'validate',return_value={}),patch.object(compose,'containers',return_value=state),patch.object(tool,'pause'),patch.object(tool,'resume') as resume,patch.object(tool,'db') as db,patch.object(tool,'inventory',return_value={'data':'hash'}),self.assertRaises(ValueError):
                tool.restore(compose,root,False)
            self.assertEqual(db.call_count,1);resume.assert_called_once()
