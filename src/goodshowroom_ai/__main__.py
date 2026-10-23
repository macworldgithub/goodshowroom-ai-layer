"""Run one loopback-only development process without request logs."""

import uvicorn
from pydantic import ValidationError

from goodshowroom_ai.app import create_app


def main() -> None:
    try:
        app = create_app()
    except ValidationError:
        raise SystemExit("Invalid backend configuration.") from None
    uvicorn.run(app, host="127.0.0.1", port=8000, workers=1, access_log=False)


if __name__ == "__main__":
    main()
