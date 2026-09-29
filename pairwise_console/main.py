import argparse
from pathlib import Path

from .api import serve
from .config import load_config
from .db import Database
from .service import PairwiseService


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude A/B GSB Console")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-import", action="store_true", help="do not import legacy eligible tasks on startup")
    args = parser.parse_args()
    config = load_config()
    if args.host or args.port:
        # Config is frozen to keep runtime settings immutable; use dataclass replacement.
        from dataclasses import replace
        config = replace(config, host=args.host or config.host, port=args.port or config.port)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.projects_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.db_path)
    db.initialize()
    service = PairwiseService(config, db)
    if not args.no_import:
        result = service.import_historical(1000)
        print("Historical task import: %s" % result, flush=True)
    service.start_scheduler()
    serve(config, db, service)


if __name__ == "__main__":
    main()
