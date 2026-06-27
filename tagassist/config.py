"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    library: str
    host: str = "0.0.0.0"
    port: int = 8765

    @classmethod
    def from_env(cls) -> "Config":
        library = os.environ.get("TAGASSIST_LIBRARY", "").strip()
        if not library:
            raise SystemExit(
                "TAGASSIST_LIBRARY is not set.\n"
                "Point it at your TagStudio library folder (the one containing "
                "the .TagStudio directory), e.g.:\n"
                "    export TAGASSIST_LIBRARY=/path/to/library\n"
                "Or generate a sample one:\n"
                "    python scripts/make_sample_library.py /tmp/sample-lib"
            )
        return cls(
            library=library,
            host=os.environ.get("TAGASSIST_HOST", "0.0.0.0"),
            port=int(os.environ.get("TAGASSIST_PORT", "8765")),
        )
