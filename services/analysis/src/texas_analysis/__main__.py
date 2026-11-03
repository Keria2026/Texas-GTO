import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "texas_analysis.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=int(os.getenv("TEXAS_PORT", "8765")),
        ws="websockets",
        ws_max_size=256 * 1024,
        ws_max_queue=4,
        ws_per_message_deflate=False,
    )


if __name__ == "__main__":
    main()
