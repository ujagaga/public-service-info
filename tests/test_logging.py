import logging
from pathlib import Path
import tempfile
import unittest

from log_setup import BoundedFileHandler, LocalFormatter, MAX_BYTES


class LoggingTests(unittest.TestCase):
    def test_rotation_bounds_utf8_and_oversized_tracebacks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.log'
            handler = BoundedFileHandler(path)
            handler.setFormatter(LocalFormatter('%(asctime)s %(levelname)s %(message)s'))
            try:
                for message in ['ž' * 300_000] * 4 + ['ć' * MAX_BYTES, 'latest success']:
                    handler.handle(logging.LogRecord('test', logging.INFO, '', 0, message, (), None))
                self.assertEqual(sorted(p.name for p in Path(directory).iterdir()),
                                 ['test.log', 'test.log.1', 'test.log.lock'])
                for file in (path, Path(str(path) + '.1')):
                    self.assertLessEqual(file.stat().st_size, MAX_BYTES)
                    file.read_text(encoding='utf-8')
                self.assertIn('latest success', path.read_text())
                self.assertTrue(Path(str(path) + '.1').read_text().endswith('[truncated]\n'))
            finally:
                handler.close()
