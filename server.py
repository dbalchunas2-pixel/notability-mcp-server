"""
Notability MCP Server
Reads Notability PDF auto-backups from Google Drive and exposes them
as MCP tools for AI assistants.
"""

import os
import glob
import io
import json
import logging
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("notability-mcp")

mcp = FastMCP("notability")

BACKUP_DIR = os.environ.get("LOCAL_BACKUP_DIR", "")
DRIVE_FOLDER_NAME = os.environ.get("GOOGLE_DRIVE_FOLDER_NAME", "Notability")
DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
USE_LOCAL = bool(BACKUP_DIR)

_drive_service = None

def get_drive_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        sa_info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
        logger.info("Google Drive service initialized from env var")
    elif os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE"):
        creds = service_account.Credentials.from_service_account_file(
            os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=scopes)
        logger.info("Google Drive service initialized from file")
    else:
        raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE env var")

    _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service


def resolve_backup_folder_id() -> str:
    if DRIVE_FOLDER_ID:
        return DRIVE_FOLDER_ID
    service = get_drive_service()
    results = service.files().list(
        q=f"mimeType = 'application/vnd.google-apps.folder' and name = '{DRIVE_FOLDER_NAME}' and trashed = false",
        fields="files(id, name)", pageSize=10,
    ).execute()
    folders = results.get("files", [])
    if not folders:
        raise RuntimeError(f"No folder named '{DRIVE_FOLDER_NAME}' found")
    return folders[0]["id"]


def _drive_list_files(folder_id: str) -> list[dict]:
    service = get_drive_service()
    all_files = []
    def _list_in_parent(parent_id):
        token = None
        while True:
            results = service.files().list(
                q=f"'{parent_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size, parents)",
                pageSize=200, pageToken=token,
            ).execute()
            for item in results.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    _list_in_parent(item["id"])
                elif item["mimeType"] == "application/pdf":
                    all_files.append(item)
            token = results.get("nextPageToken")
            if not token:
                break
    _list_in_parent(folder_id)
    return all_files


def _drive_download_file(file_id: str) -> bytes:
    service = get_drive_service()
    return service.files().get_media(fileId=file_id).execute()


def _drive_get_subfolders(folder_id: str) -> list[dict]:
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id, name)", pageSize=100,
    ).execute()
    return results.get("files", [])


def _extract_pdf_text(pdf_bytes: bytes, max_chars: int = 50000) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        pages.append(f"--- Page {i+1} ---\n{text}")
    full_text = "\n\n".join(pages)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + f"\n\n[... truncated at {max_chars} characters ...]"
    return full_text


@mcp.tool
def list_folders() -> str:
    """List all Notability folders from backups."""
    try:
        if USE_LOCAL:
            folders = set()
            for root, _, files in os.walk(BACKUP_DIR):
                if any(f.endswith(".pdf") for f in files):
                    folders.add(os.path.relpath(root, BACKUP_DIR))
            return "\n".join(sorted(folders)) if folders else "No folders found."
        else:
            folder_id = resolve_backup_folder_id()
            subfolders = _drive_get_subfolders(folder_id)
            folders = [f["name"] for f in subfolders]
            return "\n".join(folders) if folders else "(root - no subfolders)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool
def list_notes(folder: str = "") -> str:
    """List all Notability notes (PDFs) available in backups."""
    try:
        if USE_LOCAL:
            search_path = os.path.join(BACKUP_DIR, folder) if folder else BACKUP_DIR
            notes = []
            for filepath in glob.glob(os.path.join(search_path, "**/*.pdf"), recursive=True):
                name = Path(filepath).stem
                rel = os.path.relpath(filepath, BACKUP_DIR)
                size_kb = round(os.path.getsize(filepath) / 1024, 1)
                mtime = datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d %H:%M")
                notes.append(f"{name} | {rel} | {size_kb} KB | {mtime}")
            return "\n".join(notes) if notes else "No notes found."
        else:
            backup_id = resolve_backup_folder_id()
            if folder:
                subfolders = _drive_get_subfolders(backup_id)
                target = next((f for f in subfolders if f["name"] == folder), None)
                search_id = target["id"] if target else backup_id
            else:
                search_id = backup_id
            notes = _drive_list_files(search_id)
            if not notes:
                return "No notes found."
            lines = []
            for note in notes:
                name = note.get("name", "Untitled")
                file_id = note.get("id", "")
                size_kb = round(int(note.get("size", 0)) / 1024, 1)
                mtime = note.get("modifiedTime", "unknown")[:16].replace("T", " ")
                lines.append(f"{name} | {file_id} | {size_kb} KB | {mtime}")
            return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool
def read_note(note_path: str) -> str:
    """Read the full text content of a specific Notability note."""
    try:
        if USE_LOCAL:
            full_path = os.path.join(BACKUP_DIR, note_path)
            if not os.path.exists(full_path):
                full_path += ".pdf"
            with open(full_path, "rb") as f:
                pdf_bytes = f.read()
        else:
            pdf_bytes = _drive_download_file(note_path)
        text = _extract_pdf_text(pdf_bytes)
        return text if text.strip() else "Note appears to be empty or handwritten."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool
def search_notes(query: str, max_results: int = 10) -> str:
    """Search across all Notability notes for a keyword or phrase."""
    try:
        if USE_LOCAL:
            search_path = BACKUP_DIR
            notes = []
            for filepath in glob.glob(os.path.join(search_path, "**/*.pdf"), recursive=True):
                notes.append({"name": Path(filepath).stem, "path": filepath})
        else:
            backup_id = resolve_backup_folder_id()
            drive_notes = _drive_list_files(backup_id)
            notes = [{"name": n.get("name", "?"), "id": n.get("id", "")} for n in drive_notes]
        if not notes:
            return "No notes available to search."
        query_lower = query.lower()
        results = []
        for note in notes:
            try:
                if USE_LOCAL:
                    with open(note["path"], "rb") as f:
                        pdf_bytes = f.read()
                else:
                    pdf_bytes = _drive_download_file(note["id"])
                text = _extract_pdf_text(pdf_bytes, max_chars=100000)
                if query_lower in text.lower():
                    idx = text.lower().find(query_lower)
                    context = text[max(0,idx-100):idx+len(query)+200].replace("\n", " ").strip()
                    results.append(f"{note['name']}\n  ...{context}...")
                    if len(results) >= max_results:
                        break
            except Exception:
                continue
        return f"Found {len(results)} match(es):\n\n" + "\n\n".join(results) if results else f"No matches for '{query}'."
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    logger.info("Starting Notability MCP Server")
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
