"""
Notability MCP Server
Reads Notability PDF auto-backups from Google Drive and exposes them
as MCP tools for AI assistants.

Tools:
  - list_folders: List all Notability folders from backups
  - list_notes: List notes (PDFs) in a folder or all notes
  - read_note: Read full text content of a specific note
  - search_notes: Search across all notes for a keyword/phrase

Environment variables:
  GOOGLE_SERVICE_ACCOUNT_JSON  - Paste service account JSON content directly (easiest)
  GOOGLE_SERVICE_ACCOUNT_FILE  - Path to service account JSON file (alternative)
  GOOGLE_CREDENTIALS_FILE      - Path to OAuth client secrets JSON (for local dev)
  GOOGLE_DRIVE_FOLDER_NAME     - Name of the Notability backup folder (default: "Notability")
  GOOGLE_DRIVE_FOLDER_ID       - Specific folder ID (optional, overrides name search)
  LOCAL_BACKUP_DIR             - Local directory mode (if not using Google Drive)
"""

import os
import glob
import io
import json
import logging
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("notability-mcp")

mcp = FastMCP("notability")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BACKUP_DIR = os.environ.get("LOCAL_BACKUP_DIR", "")
DRIVE_FOLDER_NAME = os.environ.get("GOOGLE_DRIVE_FOLDER_NAME", "Notability")
DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
USE_LOCAL = bool(BACKUP_DIR)

# Google Drive client (lazy init)
_drive_service = None

def get_drive_service():
    """Initialize and cache the Google Drive service."""
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "google-api-python-client and google-auth are required. "
            "pip install google-api-python-client google-auth-oauthlib"
        )

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]

    # Option 1: JSON content pasted directly as env var (easiest for Railway)
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        sa_info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
        logger.info("Google Drive service initialized from GOOGLE_SERVICE_ACCOUNT_JSON env var")
    # Option 2: JSON file path
    elif os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE"):
        sa_file = os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]
        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
        logger.info(f"Google Drive service initialized from file: {sa_file}")
    # Option 3: OAuth (local dev only)
    elif os.environ.get("GOOGLE_CREDENTIALS_FILE"):
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(os.environ["GOOGLE_CREDENTIALS_FILE"], scopes)
        creds = flow.run_local_server(port=0)
        logger.info("Google Drive service initialized via OAuth")
    else:
        raise RuntimeError(
            "Set GOOGLE_SERVICE_ACCOUNT_JSON (paste JSON content) or "
            "GOOGLE_SERVICE_ACCOUNT_FILE (file path) env var"
        )

    _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service


def resolve_backup_folder_id() -> str:
    """Find the Notability backup folder ID on Google Drive."""
    if DRIVE_FOLDER_ID:
        return DRIVE_FOLDER_ID

    service = get_drive_service()
    results = service.files().list(
        q=f"mimeType = 'application/vnd.google-apps.folder' and name = '{DRIVE_FOLDER_NAME}' and trashed = false",
        fields="files(id, name)",
        pageSize=10,
    ).execute()
    folders = results.get("files", [])
    if not folders:
        raise RuntimeError(f"No folder named '{DRIVE_FOLDER_NAME}' found in Google Drive")
    folder_id = folders[0]["id"]
    logger.info(f"Found Notability folder: {DRIVE_FOLDER_NAME} ({folder_id})")
    return folder_id


def _drive_list_files(folder_id: str, mime_filter: str = "application/pdf") -> list[dict]:
    """List all PDF files in a Google Drive folder (recursively)."""
    service = get_drive_service()
    all_files = []

    def _list_in_parent(parent_id):
        token = None
        while True:
            results = service.files().list(
                q=f"'{parent_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size, parents)",
                pageSize=200,
                pageToken=token,
            ).execute()
            items = results.get("files", [])
            for item in items:
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    _list_in_parent(item["id"])
                elif item["mimeType"] == mime_filter:
                    all_files.append(item)
            token = results.get("nextPageToken")
            if not token:
                break

    _list_in_parent(folder_id)
    return all_files


def _drive_download_file(file_id: str) -> bytes:
    """Download a file's content from Google Drive."""
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    return request.execute()


def _drive_get_subfolders(folder_id: str) -> list[dict]:
    """List subfolders in a Google Drive folder."""
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id, name)",
        pageSize=100,
    ).execute()
    return results.get("files", [])


def _local_list_pdfs(folder: str = "") -> list[dict]:
    search_path = os.path.join(BACKUP_DIR, folder) if folder else BACKUP_DIR
    files = []
    for filepath in glob.glob(os.path.join(search_path, "**/*.pdf"), recursive=True):
        name = Path(filepath).stem
        rel = os.path.relpath(filepath, BACKUP_DIR)
        size = os.path.getsize(filepath)
        mtime = datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat()
        files.append({"id": rel, "name": name, "path": rel, "size": size, "modifiedTime": mtime})
    return files


def _local_read_pdf(rel_path: str) -> bytes:
    full_path = os.path.join(BACKUP_DIR, rel_path)
    if not os.path.exists(full_path):
        full_path += ".pdf"
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"Note not found: {rel_path}")
    with open(full_path, "rb") as f:
        return f.read()


def _local_list_folders() -> list[str]:
    folders = set()
    for root, _, files in os.walk(BACKUP_DIR):
        if any(f.endswith(".pdf") for f in files):
            rel = os.path.relpath(root, BACKUP_DIR)
            folders.add(rel)
    return sorted(folders)


def _extract_pdf_text(pdf_bytes: bytes, max_chars: int = 50000) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return "Error: pypdf not installed. Run: pip install pypdf"

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
            folders = _local_list_folders()
        else:
            folder_id = resolve_backup_folder_id()
            subfolders = _drive_get_subfolders(folder_id)
            folders = [f["name"] for f in subfolders]
            if not folders:
                folders = ["(root - no subfolders)"]
        if not folders:
            return "No folders found. Make sure Notability auto-backup is running."
        return "\n".join(folders)
    except Exception as e:
        logger.error(f"list_folders error: {e}")
        return f"Error: {e}"


@mcp.tool
def list_notes(folder: str = "") -> str:
    """List all Notability notes (PDFs) available in backups."""
    try:
        if USE_LOCAL:
            notes = _local_list_pdfs(folder)
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
            return "No notes found. Notability auto-backup may still be processing."
        lines = []
        for note in notes:
            name = note.get("name", "Untitled")
            path = note.get("path", note.get("id", ""))
            size_kb = round(int(note.get("size", 0)) / 1024, 1)
            mtime = note.get("modifiedTime", "unknown")[:16].replace("T", " ")
            lines.append(f"{name} | {path} | {size_kb} KB | {mtime}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"list_notes error: {e}")
        return f"Error: {e}"


@mcp.tool
def read_note(note_path: str) -> str:
    """Read the full text content of a specific Notability note."""
    try:
        if USE_LOCAL:
            pdf_bytes = _local_read_pdf(note_path)
        else:
            pdf_bytes = _drive_download_file(note_path)
        text = _extract_pdf_text(pdf_bytes)
        if not text.strip():
            return "Note appears to be empty or contains only handwritten content."
        return text
    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        logger.error(f"read_note error: {e}")
        return f"Error: {e}"


@mcp.tool
def search_notes(query: str, max_results: int = 10) -> str:
    """Search across all Notability notes for a keyword or phrase."""
    try:
        if USE_LOCAL:
            notes = _local_list_pdfs()
        else:
            backup_id = resolve_backup_folder_id()
            notes = _drive_list_files(backup_id)
        if not notes:
            return "No notes available to search."
        query_lower = query.lower()
        results = []
        for note in notes:
            try:
                if USE_LOCAL:
                    pdf_bytes = _local_read_pdf(note["path"])
                else:
                    pdf_bytes = _drive_download_file(note["id"])
                text = _extract_pdf_text(pdf_bytes, max_chars=100000)
                if query_lower in text.lower():
                    idx = text.lower().find(query_lower)
                    start = max(0, idx - 100)
                    end = min(len(text), idx + len(query) + 200)
                    context = text[start:end].replace("\n", " ").strip()
                    name = note.get("name", "Untitled")
                    path = note.get("path", note.get("id", ""))
                    results.append(f"{name} ({path})\n  ...{context}...")
                    if len(results) >= max_results:
                        break
            except Exception as e:
                logger.warning(f"Error searching note {note.get('name', '?')}: {e}")
                continue
        if not results:
            return f"No matches found for '{query}'."
        header = f"Found {len(results)} match(es) for '{query}':\n\n"
        return header + "\n\n".join(results)
    except Exception as e:
        logger.error(f"search_notes error: {e}")
        return f"Error: {e}"


if __name__ == "__main__":
    mode = "LOCAL" if USE_LOCAL else "GOOGLE DRIVE"
    logger.info(f"Starting Notability MCP Server ({mode} mode)")
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
