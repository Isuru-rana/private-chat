import logging
import os
import resource
from pathlib import Path

import uvicorn

from .app import create_app

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core dumps holding the vault key
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("PC_PORT", "3050"))
    token = os.environ.pop("PC_APP_TOKEN", "") or None
    app = create_app(Path(os.environ.get("PC_DATA", ROOT / "data")), app_token=token)
    if token is None:  # started without run.sh: show the one URL that works for this launch
        print(f"open http://127.0.0.1:{port}/?k={app.state.app_token}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
