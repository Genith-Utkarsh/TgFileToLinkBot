# Context: Zero-Cost Personal Telegram Video Streaming Engine for Web Embedding

## Objective
Build a lightweight, single-user Telegram-to-Web Video Streaming API. The system allows the user to upload video files (.mp4, .mkv, .webm) to a private Telegram bot and instantly get a direct HTTP raw URL that can be dropped into an HTML5 `<video>` tag on a custom website.

## Tech Stack & Architecture Constraints
- **Language/Framework:** Python 3.11+, FastAPI, Uvicorn, and `python-telegram-bot` (v20+ Async).
- **Storage:** Strict **Zero-Disk Cache**. The server must act as a transparent byte-passthrough reverse proxy from Telegram's servers to the browser.
- **Security:** Since this is for a single personal user, implement an optional simple API token query parameter (e.g., `/stream/{file_id}?token=mysecret`) to prevent random people from scraping your bot's streaming bandwidth.

## Critical Web Features

### 1. Robust CORS Configuration
Web browsers will block video streaming elements if Cross-Origin Resource Sharing (CORS) headers aren't present. The FastAPI server must explicitly allow all origins or a specified domain, supporting these headers on streaming endpoints:
- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Methods: GET, OPTIONS`
- `Access-Control-Allow-Headers: Range, Content-Type`

### 2. Precise Browser-Grade Range Requests (Status 206)
Safari, Chrome, and Firefox will fail to render or scrub through videos if the server does not flawlessly respond to `Range: bytes=X-Y`.
- Parse the `Range` header out of the request.
- Calculate `start` and `end` byte boundaries.
- Fetch *only* that chunk from Telegram's file paths using an asynchronous `httpx.AsyncClient`.
- Return a `206 Partial Content` status response code along with explicit `Content-Range`, `Accept-Ranges`, and `Content-Length` headers.
