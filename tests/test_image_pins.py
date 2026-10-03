from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_images import validate_images


class ImagePinTests(unittest.TestCase):
    def manifests(self, scraper='sha-33734b8', stt='sha-9153e89'):
        return f'image: ghcr.io/pei9564/finance-scraper-service:{scraper}\nimage: ghcr.io/pei9564/stt-ai-worker:{stt}\n'

    def test_versioned_images_accepted(self):
        validate_images(self.manifests())

    def test_mutable_tag_rejected(self):
        for tag in ('latest', 'main', '1.0', ''):
            with self.assertRaises(ValueError):
                validate_images(self.manifests(stt=tag))

    def test_missing_service_rejected(self):
        with self.assertRaises(ValueError):
            validate_images('image: postgres:15-alpine\n')

    def test_digest_accepted(self):
        validate_images('image: ghcr.io/pei9564/finance-scraper-service@sha256:' + 'a' * 64 + '\nimage: ghcr.io/pei9564/stt-ai-worker:sha-9153e89\n')
