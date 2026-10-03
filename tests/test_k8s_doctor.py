import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import k8s_doctor


class FakeKubectl:
    gpu = True
    missing_key = False

    def __init__(self, args):
        pass

    def target(self):
        print('Context: test')

    def get(self, resource, name='', namespaced=True):
        if resource == 'nodes':
            return {'items': [
                {'metadata': {'name': role, 'labels': {'finance.node/role': role}},
                 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                            'allocatable': {'nvidia.com/gpu': '1' if self.gpu else '0'}}}
                for role in ('data', 'gpu')
            ]}
        if resource == 'runtimeclass':
            return {'handler': 'nvidia'}
        if resource == 'storageclass':
            return {'provisioner': 'rancher.io/local-path'}
        if resource == 'namespace':
            return {'metadata': {'name': name}}
        if resource == 'pvc':
            return {'items': []}
        raise AssertionError('Doctor must not fetch Secret values')

    def run(self, *args):
        if 'app-secrets' in args:
            return 'Opaque\nPOSTGRES_USER\nPOSTGRES_DB\nGITHUB_TOKEN\nVLLM_API_KEY\n' + ('' if self.missing_key else 'POSTGRES_PASSWORD\n')
        if 'ghcr-cred' in args:
            return 'kubernetes.io/dockerconfigjson'
        raise AssertionError(args)


class DoctorTests(unittest.TestCase):
    def invoke(self, overlay='homelab', gpu=True, missing_key=False):
        output = io.StringIO()
        with patch.object(FakeKubectl, 'gpu', gpu), patch.object(FakeKubectl, 'missing_key', missing_key), patch.object(k8s_doctor, 'Kubectl', FakeKubectl), patch.object(sys, 'argv', ['doctor', '--overlay', overlay]), contextlib.redirect_stdout(output):
            result = k8s_doctor.main()
        return result, output.getvalue()

    def test_predeploy_cluster_without_pvcs_is_usable(self):
        code, output = self.invoke()
        self.assertFalse(code)
        self.assertIn('Not created yet', output)

    def test_missing_gpu_blocks_homelab_but_not_cpu(self):
        self.assertTrue(self.invoke(gpu=False)[0])
        self.assertFalse(self.invoke(overlay='cpu', gpu=False)[0])

    def test_missing_secret_key_reports_failure(self):
        code, output = self.invoke(missing_key=True)
        self.assertTrue(code)
        self.assertIn('Missing keys: POSTGRES_PASSWORD', output)


if __name__ == '__main__':
    unittest.main()
