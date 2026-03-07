"""Debug API — browse screenshots and HTML dumps saved by the browser bot."""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

SCREENSHOT_DIR = Path("/app/data/screenshots")

router = APIRouter(prefix="/debug", tags=["Debug"])


@router.get("/screenshots", summary="List all debug screenshots and HTML dumps")
async def list_screenshots():
    """Return metadata for every file in the screenshot directory."""
    if not SCREENSHOT_DIR.exists():
        return {"files": [], "directory": str(SCREENSHOT_DIR)}

    files = []
    for f in sorted(SCREENSHOT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix not in (".png", ".html"):
            continue
        stat = f.stat()
        files.append({
            "name": f.name,
            "type": f.suffix.lstrip("."),
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })

    return {"files": files, "directory": str(SCREENSHOT_DIR)}


@router.get("/screenshots/{filename}", summary="Serve a single screenshot or HTML dump")
async def get_screenshot(filename: str):
    """Download or view a specific debug file by name."""
    # Prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = SCREENSHOT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if path.suffix == ".html":
        return HTMLResponse(content=path.read_text(errors="replace"))

    return FileResponse(str(path), media_type="image/png")
