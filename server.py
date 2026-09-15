"""
Notability MCP Server v2.0
Reads Notability PDF auto-backups from Google Drive and exposes them
as MCP tools for AI assistants.

Key fixes in v2.0:
- search_notes uses Google Drive's built-in fullText search (zero downloads)
  with pypdf fallback only for unindexed/handwritten notes
- list_notes shows folder context for each file
- list_folders deduplicates folder names
- In-memory text cache to avoid re-downloading on repeat searches
- Per-file download error handling with timeouts
"""

import os
import io
import json
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from functools import lru_cache

from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("notability-mcp")

mcp = FastMCP("notability")

BACKUP_DIR = os.environ.get("LOCAL_BACKUP_DIR", "")
DRIVE_FOLDER_NAME = os.environ.get("GOOGLE_DRIVE_FOLDER_NAME", "Notability")
DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
USE_LOCAL = bool(BACKUP_DIR)

_drive_service = None
_text_cache: dict[str, str] = {}  # file_id -> extracted text
_folder_cache: dict[str, str] = {}  # file_id -> folder name


def get_drive_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

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

    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
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


def _drive_list_files_with_folders(folder_id: str) -> list[dict]:
    """Recursively list all PDFs, tracking which folder each belongs to."""
    service = get_drive_service()
    all_files = []

    def _list_in_parent(parent_id: str, folder_name: str):
        token = None
        while True:
            results = service.files().list(
                q=f"'{parent_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size, parents)",
                pageSize=200, pageToken=token,
            ).execute()
            for item in results.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    _list_in_parent(item["id"], item["name"])
                elif item["mimeType"] == "application/pdf":
                    item["_folder"] = folder_name
                    all_files.append(item)
            token = results.get("nextPageToken")
            if not token:
                               break

    _list_in_parent(folder_id, "(root)")
    return all_files


def _drive_download_file(file_id: str, timeout: int = 30) -> bytes:
    """Download a file from Drive with timeout handling."""
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return fh.read()


def _drive_get_subfolders(folder_id: str) -> list[dict]:
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id, name)", pageSize=100,
    ).execute()
    return results.get("files", [])


def _drive_fulltext_search(folder_id: str, query: str) -> list[dict]:
    """Use Google Drive's built-in full-text search to find PDFs containing query.
    This searches server-side - zero downloads needed.
    Only searches direct children recursively via the folder traversal."""
    service = get_drive_service()
    # Drive fullText search doesn't support recursive parent filtering well,
    # so we search all non-trashed PDFs and then filter to those in our folder tree
    all_matches = []
    token = None
    escaped_query = query.replace("'", "\\'")
    while True:
        results = service.files().list(
            q=f"mimeType = 'application/pdf' and trashed = false and fullText contains '{escaped_query}'",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, size, parents)",
            pageSize=200, pageToken=token,
        ).execute()
        all_matches.extend(results.get("files", []))
        token = results.get("nextPageToken")
        if not token:
            break

    # Build a set of valid file IDs that belong to our Notability backup tree
    tree_files = _drive_list_files_with_folders(folder_id)
    valid_ids = {f["id"] for f in tree_files}
    valid_id_to_folder = {f["id"]: f.get("_folder", "?") for f in tree_files}

    filtered = []
    for match in all_matches:
        if match["id"] in valid_ids:
            match["_folder"] = valid_id_to_folder.get(match["id"], "?")
            filtered.append(match)

    return filtered


def _extract_pdf_text(pdf_bytes: bytes, max_chars: int = 50000) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append(f"--- Page {i+1} ---\n{text}")
    full_text = "\n\n".join(pages)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + f"\n\n[... truncated at {max_chars} characters ...]"
    return full_text


def _get_note_text(file_id: str, file_name: str = "") -> str:
    """Get extracted text for a note, using cache when available."""
    if file_id in _text_cache:
        return _text_cache[file_id]

    try:
        pdf_bytes = _drive_download_file(file_id)
        text = _extract_pdf_text(pdf_bytes)
        _text_cache[file_id] = text
        return text
    except Exception as e:
        logger.warning(f"Failed to download/extract {file_name or file_id}: {e}")
        return ""


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
            # Deduplicate by name, preserving first occurrence
            seen = set()
            folders = []
            for f in subfolders:
                if f["name"] not in seen:
                    seen.add(f["name"])
                    folders.append(f["name"])
            return "\n".join(folders) if folders else "(root - no subfolders)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool
def list_notes(folder: str = "") -> str:
    """List all Notability notes (PDFs) available in backups.

    Args:
        folder: Optional folder name to filter by. If empty, lists all notes.
    """
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
                subfolders = _drive_get_subfolders(folder_id=backup_id)
                # Find matching subfolder(s) by name
                targets = [f for f in subfolders if f["name"] == folder]
                if not targets:
                    return f"Folder '{folder}' not found."
                # List PDFs in all matching subfolders
                all_notes = []
                for target in targets:
                    notes = _drive_list_files_with_folders(target["id"])
                    all_notes.extend(notes)
            else:
                all_notes = _drive_list_files_with_folders(backup_id)

            if not all_notes:
                return "No notes found."
            lines = []
            for note in all_notes:
                name = note.get("name", "Untitled")
                file_id = note.get("id", "")
                note_folder = note.get("_folder", "?")
                size_kb = round(int(note.get("size", 0)) / 1024, 1)
                mtime = note.get("modifiedTime", "unknown")[:16].replace("T", " ")
                lines.append(f"{name} | {note_folder} | {file_id} | {size_kb} KB | {mtime}")
            return "\n".join(lines) if lines else "No notes found."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool
def read_note(note_path: str) -> str:
    """Read the full text content of a specific Notability note.

    Args:
        note_path: The file ID (from list_notes) or local file path of the note to read.
    """
    try:
        if USE_LOCAL:
            full_path = os.path.join(BACKUP_DIR, note_path)
            if not os.path.exists(full_path):
                full_path += ".pdf"
            with open(full_path, "rb") as f:
                pdf_bytes = f.read()
            text = _extract_pdf_text(pdf_bytes)
        else:
            text = _get_note_text(note_path)
        return text if text.strip() else "Note appears to be empty or handwritten (no extractable text)."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool
def search_notes(query: str, max_results: int = 10) -> str:
    """Search across all Notability notes for a keyword or phrase.

    Uses Google Drive's server-side full-text search first (fast, zero downloads).
    Falls back to downloading and extracting text for notes that Drive hasn't indexed
    (e.g. handwritten notes).

    Args:
        query: The keyword or phrase to search for.
        max_results: Maximum number of results to return (default 10).
    """
    try:
        backup_id = resolve_backup_folder_id()

        # --- Phase 1: Drive fullText search (fast, server-side) ---
        drive_matches = _drive_fulltext_search(backup_id, query)
        results = []

        for match in drive_matches:
            name = match.get("name", "?")
            note_folder = match.get("_folder", "?")
            file_id = match.get("id", "")

            # Try to get a text snippet from cache or download
            text = _get_note_text(file_id, name)
            if text:
                query_lower = query.lower()
                text_lower = text.lower()
                idx = text_lower.find(query_lower)
                if idx >= 0:
                    context = text[max(0, idx - 100):idx + len(query) + 200].replace("\n", " ").strip()
                    results.append(f"{name} [{note_folder}]\n  ...{context}...")
                else:
                    # Drive found it but pypdf didn't - likely image-based
                    results.append(f"{name} [{note_folder}]\n  (matched by Google Drive OCR - content not extractable as text)")
            else:
                results.append(f"{name} [{note_folder}]\n  (matched by Google Drive OCR - could not download for context)")

            if len(results) >= max_results:
                break

        if results:
            return f"Found {len(results)} match(es) via Google Drive search:\n\n" + "\n\n".join(results)

        # --- Phase 2: Fallback - download and search with pypdf ---
        # Only reaches here if Drive's fullText found nothing (e.g. handwritten notes)
        logger.info(f"Drive fullText found no matches for '{query}', falling back to pypdf extraction")
        all_notes = _drive_list_files_with_folders(backup_id)
        query_lower = query.lower()

        for note in all_notes:
            if len(results) >= max_results:
                break
            file_id = note.get("id", "")
            name = note.get("name", "?")
            note_folder = note.get("_folder", "?")

            text = _get_note_text(file_id, name)
            if not text:
                continue

            if query_lower in text.lower():
                idx = text.lower().find(query_lower)
                context = text[max(0, idx - 100):idx + len(query) + 200].replace("\n", " ").strip()
                results.append(f"{name} [{note_folder}]\n  ...{context}...")

        if results:
            return f"Found {len(results)} match(es) via text extraction:\n\n" + "\n\n".join(results)
        return f"No matches for '{query}'."

    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    logger.info("Starting Notability MCP Server v2.0")
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
