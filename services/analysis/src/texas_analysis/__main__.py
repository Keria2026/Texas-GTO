import os
import secrets
from pathlib import Path

import uvicorn


def main() -> None:
    if not os.getenv("TEXAS_API_TOKEN"):
        directory = Path(os.getenv("TEXAS_DATA_DIR", "artifacts/local"))
        directory.mkdir(parents=True, exist_ok=True)
        token_path = directory / "session.token"
        token = secrets.token_urlsafe(32)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token)
        os.environ["TEXAS_API_TOKEN"] = token
        print(f"Local API token saved to {token_path.resolve()} (not printed).")
    uvicorn.run("texas_analysis.app:create_app", factory=True, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
