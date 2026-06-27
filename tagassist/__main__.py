"""Entrypoint: ``python -m tagassist`` launches the interview web UI."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import Config


def main() -> None:
    config = Config.from_env()
    app = create_app(config)
    print(f"Tag-Assist -> library: {config.library}")
    print(f"Open http://localhost:{config.port}")
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
