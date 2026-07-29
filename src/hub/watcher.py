"""Inbox watcher — automatically ingests files the moment teams drop them.

Run:  python -m hub.watcher            (Ctrl+C to stop)

For a scheduled scan instead of a live watcher, use:
    python -m hub.ingest.pipeline --scan
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .catalog import Catalog
from .config import load_config
from .ingest import metadata as meta
from .ingest.pipeline import apply_review, ingest_file


class InboxHandler(FileSystemEventHandler):
    def __init__(self):
        self.config = load_config()
        self.catalog = Catalog(self.config)

    def on_created(self, event):
        self._handle(Path(event.src_path))

    def on_moved(self, event):
        if isinstance(event, FileMovedEvent):
            self._handle(Path(event.dest_path))

    def _handle(self, path: Path) -> None:
        if not path.is_file():
            return
        if path.name.endswith("_metadata_review.yaml"):
            res = apply_review(path, self.catalog)
            print(f"[review] {path.name}: {res}", flush=True)
            path.unlink(missing_ok=True)
            return
        if path.suffix.lower() in (".yaml", ".yml"):
            return  # sidecars are consumed together with their data file
        res = ingest_file(path, self.config, self.catalog)
        print(f"[ingest] {res}", flush=True)


def main() -> int:
    cfg = load_config()
    inbox = cfg.path("inbox")
    handler = InboxHandler()
    observer = Observer()
    observer.schedule(handler, str(inbox), recursive=True)
    observer.start()
    print(f"Watching inbox: {inbox}  (Ctrl+C to stop)", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
