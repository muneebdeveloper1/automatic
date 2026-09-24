from __future__ import annotations
import logging, time
from contextlib import contextmanager

class ContextFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.book_id = "-"
        self.book_title = "-"
        self.stage = "-"

    def set(self, book_id=None, book_title=None, stage=None):
        if book_id is not None: self.book_id = str(book_id)
        if book_title is not None: self.book_title = str(book_title)
        if stage is not None: self.stage = str(stage)

    def filter(self, record):
        record.book_id = self.book_id
        record.book_title = self.book_title
        record.stage = self.stage
        return True

ctx = ContextFilter()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | BOOK %(book_id)s | STAGE %(stage)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("audiobook")
log.addFilter(ctx)

@contextmanager
def stage(book_id, title, name):
    old = ctx.stage
    ctx.set(book_id=book_id, book_title=title, stage=name)
    started = time.monotonic()
    try:
        yield
        log.info("stage_success duration=%.1fs", time.monotonic()-started)
    finally:
        ctx.stage = old
