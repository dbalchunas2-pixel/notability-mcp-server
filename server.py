"""
Notability MCP Server v2.3
Reads Notability PDF auto-backups from Google Drive and exposes them
as MCP tools for AI assistants.

v2.3: Download retry (3 attempts), surface real errors, search returns Drive matches without downloading
v2.2: Memory-safe LRU cache, size-limited search context
v2.1: Fixed download method
v2.0: Drive fullText search, folder context, dedup folders
"""

import os
import io
import json
import logging
import time
from collections import OrderedDict
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
_CACHE_MAX = 5
_text_cache: OrderedDict[str, str] = OrderedDict()
_DOWNLOAD_RETRIES = 3
_DOWNLOAD_RETRY_DELAY = 2  # seconds


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


def _drive_download_file(file_id: str) -> bytes:
    """Download a file from Drive with retry logic."""
    service = get_drive_service()
    last_error = None
    for attempt in range(_DOWNLOAD_RETRIES):
        try:
            result = service.files().get_media(fileId=file_id).execute()
            return result
        except Exception as e:
            last_error = e
            logger.warning(f"Download attempt {attempt + 1}/{_DOWNLOAD_RETRIES} failed for {file_id}: {e}")
            if attempt < _DOWNLOAD_RETRIES - 1:
                time.sleep(_DOWNLOAD_RETRY_DELAY)
    raise last_error


def _drive_get_subfolders(folder_id: str) -> list[dict]:
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id, name)", pageSize=100,
    ).execute()
    return results.get("files", [])


def _drive_fulltext_search(folder_id: str, query: str) -> list[dict]:
    """Use Google Drive's built-in full-text search to find PDFs containing query."""
    service = get_drive_service()
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

    tree_files = _drive_list_files_with_folders(folder_id)
    valid_ids = {f["id"] for f in tree_files}
    valid_id_to_folder = {f["id"]: f.get("_folder", "?") for f in tree_files}
    valid_id_to_size = {f["id"]: int(f.get("size", 0)) for f in tree_files}

    filtered = []
    for match in all_matches:
        if match["id"] in valid_ids:
            match["_folder"] = valid_id_to_folder.get(match["id"], "?")
            match["_size"] = valid_id_to_size.get(match["id"], 0)
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


def _cache_get(file_id: str):
    if file_id in _text_cache:
        _text_cache.move_to_end(file_id)
        return _text_cache[file_id]
    return None


def _cache_put(file_id: str, text: str):
    if file_id in _text_cache:
        _text_cache.move_to_end(file_id)
    _text_cache[file_id] = text
    while len(_text_cache) > _CACHE_MAX:
        evicted_key, _ = _text_cache.popitem(last=False)
        logger.info(f"Cache evicted {evicted_key} (limit {_CACHE_MAX})")


def _get_note_text(file_id: str, file_name: str = "") -> str:
    """Get extracted text for a note, using LRU cache, with retry on download."""
    cached = _cache_get(file_id)
    if cached is not None:
        return cached

    try:
        pdf_bytes = _drive_download_file(file_id)
        logger.info(f"Downloaded {file_name or file_id}: {len(pdf_bytes)} bytes")
        text = _extract_pdf_text(pdf_bytes)
        _cache_put(file_id, text)
        return text
    except Exception as e:
        logger.warning(f"Failed to download/extract {file_name or file_id} after {_DOWNLOAD_RETRIES} attempts: {e}")
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
            import glob as globmod
            search_path = os.path.join(BACKUP_DIR, folder) if folder else BACKUP_DIR
            notes = []
            for filepath in globmod.glob(os.path.join(search_path, "**/*.pdf"), recursive=True):
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
                targets = [f for f in subfolders if f["name"] == folder]
                if not targets:
                    return f"Folder '{folder}' not found."
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
        if text.strip():
            return text
        return "Note appears to be empty or handwritten (no extractable text). If this is unexpected, the download may have failed - check server logs."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool
def search_notes(query: str, max_results: int = 10) -> str:
    """Search across all Notability notes for a keyword or phrase.

    Uses Google Drive's server-side full-text search - fast, zero downloads.
    Returns matching file names, folders, and sizes. Use read_note with the file ID
    to get full text content of any match.

    Args:
        query: The keyword or phrase to search for.
        max_results: Maximum number of results to return (default 10).
    """
    try:
        backup_id = resolve_backup_folder_id()
        drive_matches = _drive_fulltext_search(backup_id, query)

        if not drive_matches:
            # Fallback: search filenames only
            logger.info(f"Drive fullText found no matches for '{query}', trying filename search")
            all_notes = _drive_list_files_with_folders(backup_id)
            query_lower = query.lower()
            drive_matches = [
                {"name": n.get("name", "?"), "_folder": n.get("_folder", "?"),
                 "id": n.get("id", ""), "_size": int(n.get("size", 0))}
                for n in all_notes if query_lower in n.get("name", "").lower()
            ]

        if not drive_matches:
            return f"No matches for '{query}'."

        results = []
        for match in drive_matches[:max_results]:
            name = match.get("name", "?")
            note_folder = match.get("_folder", "?")
            file_id = match.get("id", "")
            file_size_kb = round(match.get("_size", 0) / 1024, 1)
            results.append(f"{name} [{note_folder}] ({file_size_kb} KB)\n  file_id: {file_id}")

        header = f"Found {len(results)} match(es) via Google Drive search"
        if len(drive_matches) > max_results:
            header += f" (showing {max_results} of {len(drive_matches)})"
        header += ":\n\nUse read_note with the file_id to get full text.\n\n"
        return header + "\n\n".join(results)

    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    logger.info(f"Starting Notability MCP Server v2.3 (cache: {_CACHE_MAX}, retries: {_DOWNLOAD_RETRIES})")
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
