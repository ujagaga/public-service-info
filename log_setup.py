"""Bounded text logging shared by CLI, CGI, and web workers."""
import datetime
import fcntl
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_PATH = Path(__file__).resolve().parent / 'public-service-info.log'
MAX_BYTES = 1_000_000


class LocalFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.datetime.fromtimestamp(
            record.created, ZoneInfo('Europe/Belgrade')).isoformat(timespec='seconds')


class BoundedFileHandler(logging.Handler):
    """Reopen under a process lock so CGI workers cannot race during rotation."""
    def __init__(self, path=LOG_PATH):
        super().__init__()
        self.path = Path(path)

    def emit(self, record):
        try:
            data = (self.format(record) + '\n').encode('utf-8', errors='replace')
            if len(data) > MAX_BYTES:
                suffix = b'... [truncated]\n'
                data = (data[:MAX_BYTES - len(suffix)].decode('utf-8', errors='ignore')
                        .encode('utf-8') + suffix)
            with open(str(self.path) + '.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if self.path.exists() and self.path.stat().st_size + len(data) > MAX_BYTES:
                    os.replace(self.path, str(self.path) + '.1')
                with self.path.open('ab') as output:
                    output.write(data)
        except Exception:
            self.handleError(record)


def configure_logging():
    root = logging.getLogger()
    if not any(isinstance(handler, BoundedFileHandler) for handler in root.handlers):
        handler = BoundedFileHandler()
        handler.setFormatter(LocalFormatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
