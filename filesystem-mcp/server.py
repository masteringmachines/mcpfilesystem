"""
filesystem_mcp - A simple MCP server for reading, writing, and searching files.

Run with:
    python server.py

Then connect via Claude Desktop or any MCP client using stdio transport.
"""

import json
import os
import fnmatch
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict, field_validator


# ─── Server setup ────────────────────────────────────────────────────────────

mcp = FastMCP(
    "filesystem_mcp",
    instructions=(
        "This server provides tools to read, write, list, search, and delete "
        "files on the local filesystem. All paths are resolved relative to the "
        "working directory unless absolute. Paths that escape the working "
        "directory are rejected for safety."
    ),
)

# Root the server to the directory it is launched from
WORK_DIR = Path(os.getcwd()).resolve()


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _safe_path(raw: str) -> Path:
    """Resolve *raw* relative to WORK_DIR and reject path-traversal attempts."""
    resolved = (WORK_DIR / raw).resolve()
    if not str(resolved).startswith(str(WORK_DIR)):
        raise ValueError(
            f"Path '{raw}' resolves outside the working directory '{WORK_DIR}'. "
            "Only paths inside the working directory are allowed."
        )
    return resolved


def _file_info(path: Path) -> dict:
    """Return a compact dict of metadata for *path*."""
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path.relative_to(WORK_DIR)),
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "is_dir": path.is_dir(),
    }


def _handle_error(e: Exception) -> str:
    """Produce a consistent, actionable error string."""
    if isinstance(e, FileNotFoundError):
        return f"Error: File not found — {e.filename}. Check the path and try again."
    if isinstance(e, PermissionError):
        return f"Error: Permission denied — {e.filename}. You don't have access to this file."
    if isinstance(e, ValueError):
        return f"Error: {e}"
    return f"Error: {type(e).__name__}: {e}"


# ─── Input models ─────────────────────────────────────────────────────────────

class ReadFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Relative or absolute path to the file to read.", min_length=1)
    max_lines: Optional[int] = Field(
        default=None,
        description="If set, only return the first N lines. Useful for large files.",
        ge=1, le=10_000,
    )


class WriteFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Relative path where the file should be written.", min_length=1)
    content: str = Field(..., description="Text content to write to the file.")
    overwrite: bool = Field(default=False, description="Set to true to overwrite an existing file.")


class ListDirInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(default=".", description="Directory path to list. Defaults to the working directory.")
    show_hidden: bool = Field(default=False, description="Include hidden files and directories (those starting with '.').")


class SearchFilesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    pattern: str = Field(..., description="Glob pattern to match filenames, e.g. '*.py' or 'README*'.", min_length=1)
    directory: str = Field(default=".", description="Root directory to search in. Defaults to the working directory.")
    recursive: bool = Field(default=True, description="Search subdirectories recursively.")

    @field_validator("pattern")
    @classmethod
    def pattern_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Search pattern cannot be blank.")
        return v


class DeleteFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    path: str = Field(..., description="Relative path to the file to delete.", min_length=1)


class GrepInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    text: str = Field(..., description="The string to search for inside files.", min_length=1)
    directory: str = Field(default=".", description="Root directory to search in.")
    file_pattern: str = Field(default="*.*", description="Glob pattern limiting which files to search, e.g. '*.py'.")
    case_sensitive: bool = Field(default=False, description="Whether the search is case-sensitive.")
    max_results: int = Field(default=50, description="Maximum number of matching lines to return.", ge=1, le=500)


# ─── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="fs_read_file",
    annotations={
        "title": "Read File",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def fs_read_file(params: ReadFileInput) -> str:
    """Read the contents of a text file and return them as a string.

    Args:
        params (ReadFileInput): Input containing:
            - path (str): Path to the file.
            - max_lines (Optional[int]): Limit output to the first N lines.

    Returns:
        str: The file contents, optionally truncated to max_lines, followed
             by file metadata (size, modified date).
    """
    try:
        target = _safe_path(params.path)
        if target.is_dir():
            return f"Error: '{params.path}' is a directory. Use fs_list_directory to list it."

        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total_lines = len(lines)

        if params.max_lines and total_lines > params.max_lines:
            content = "\n".join(lines[: params.max_lines])
            truncation_note = f"\n\n[Showing {params.max_lines} of {total_lines} lines. Set max_lines higher to see more.]"
        else:
            content = text
            truncation_note = ""

        stat = target.stat()
        meta = (
            f"\n\n---\n"
            f"File: {params.path} | "
            f"{stat.st_size:,} bytes | "
            f"Modified: {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return content + truncation_note + meta

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="fs_write_file",
    annotations={
        "title": "Write File",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def fs_write_file(params: WriteFileInput) -> str:
    """Write text content to a file, creating parent directories as needed.

    Args:
        params (WriteFileInput): Input containing:
            - path (str): Destination path for the file.
            - content (str): Text to write.
            - overwrite (bool): Must be True to replace an existing file.

    Returns:
        str: Confirmation message with the file path and byte count.
    """
    try:
        target = _safe_path(params.path)

        if target.exists() and not params.overwrite:
            return (
                f"Error: '{params.path}' already exists. "
                "Set overwrite=true to replace it."
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(params.content, encoding="utf-8")

        return (
            f"✓ Written {len(params.content.encode()):,} bytes to '{params.path}'.\n"
            f"Lines: {params.content.count(chr(10)) + 1}"
        )

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="fs_list_directory",
    annotations={
        "title": "List Directory",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def fs_list_directory(params: ListDirInput) -> str:
    """List files and subdirectories in a directory.

    Args:
        params (ListDirInput): Input containing:
            - path (str): Directory to list.
            - show_hidden (bool): Include dotfiles.

    Returns:
        str: JSON list of file/directory metadata objects, each containing
             name, path, size_bytes, modified, and is_dir.
    """
    try:
        target = _safe_path(params.path)
        if not target.is_dir():
            return f"Error: '{params.path}' is not a directory."

        entries = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if not params.show_hidden and item.name.startswith("."):
                continue
            entries.append(_file_info(item))

        return json.dumps(
            {"directory": str(Path(params.path)), "count": len(entries), "entries": entries},
            indent=2,
        )

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="fs_search_files",
    annotations={
        "title": "Search Files by Name",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def fs_search_files(params: SearchFilesInput) -> str:
    """Find files whose names match a glob pattern.

    Args:
        params (SearchFilesInput): Input containing:
            - pattern (str): Glob pattern such as '*.py' or 'README*'.
            - directory (str): Root directory to search.
            - recursive (bool): Search subdirectories.

    Returns:
        str: JSON list of matching file metadata objects.
    """
    try:
        root = _safe_path(params.directory)
        if not root.is_dir():
            return f"Error: '{params.directory}' is not a directory."

        glob_fn = root.rglob if params.recursive else root.glob
        matches = [
            _file_info(p)
            for p in sorted(glob_fn(params.pattern))
            if p.is_file()
        ]

        return json.dumps(
            {
                "pattern": params.pattern,
                "directory": params.directory,
                "recursive": params.recursive,
                "count": len(matches),
                "matches": matches,
            },
            indent=2,
        )

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="fs_grep",
    annotations={
        "title": "Search Text Inside Files",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def fs_grep(params: GrepInput) -> str:
    """Search for a string inside files and return matching lines with context.

    Args:
        params (GrepInput): Input containing:
            - text (str): The substring to find.
            - directory (str): Root directory to search.
            - file_pattern (str): Glob pattern to restrict which files are searched.
            - case_sensitive (bool): Case-sensitive match.
            - max_results (int): Cap on total matching lines returned.

    Returns:
        str: JSON with matched lines grouped by file, each entry containing
             file path, line number, and the matching line content.
    """
    try:
        root = _safe_path(params.directory)
        needle = params.text if params.case_sensitive else params.text.lower()

        results: List[dict] = []
        files_searched = 0

        for filepath in sorted(root.rglob(params.file_pattern)):
            if not filepath.is_file():
                continue
            try:
                lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue

            files_searched += 1
            for lineno, line in enumerate(lines, start=1):
                haystack = line if params.case_sensitive else line.lower()
                if needle in haystack:
                    results.append(
                        {
                            "file": str(filepath.relative_to(WORK_DIR)),
                            "line": lineno,
                            "content": line.rstrip(),
                        }
                    )
                    if len(results) >= params.max_results:
                        break
            if len(results) >= params.max_results:
                break

        return json.dumps(
            {
                "search_text": params.text,
                "files_searched": files_searched,
                "matches_found": len(results),
                "truncated": len(results) >= params.max_results,
                "results": results,
            },
            indent=2,
        )

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="fs_delete_file",
    annotations={
        "title": "Delete File",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def fs_delete_file(params: DeleteFileInput) -> str:
    """Permanently delete a file (not directories).

    Args:
        params (DeleteFileInput): Input containing:
            - path (str): Path to the file to delete.

    Returns:
        str: Confirmation or error message.
    """
    try:
        target = _safe_path(params.path)

        if not target.exists():
            return f"Error: '{params.path}' does not exist."
        if target.is_dir():
            return (
                f"Error: '{params.path}' is a directory. "
                "This tool only deletes files, not directories."
            )

        target.unlink()
        return f"✓ Deleted '{params.path}'."

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="fs_file_info",
    annotations={
        "title": "Get File Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def fs_file_info(path: str) -> str:
    """Return metadata about a file or directory without reading its contents.

    Args:
        path (str): Relative or absolute path to inspect.

    Returns:
        str: JSON object with name, path, size_bytes, modified, is_dir,
             and for files: line_count and extension.
    """
    try:
        target = _safe_path(path)
        info = _file_info(target)

        if target.is_file():
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
                info["line_count"] = text.count("\n") + 1
            except Exception:
                info["line_count"] = None
            info["extension"] = target.suffix or "(none)"

        return json.dumps(info, indent=2)

    except Exception as e:
        return _handle_error(e)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Starting filesystem_mcp …")
    print(f"Working directory: {WORK_DIR}")
    print(f"Tools available: fs_read_file, fs_write_file, fs_list_directory,")
    print(f"                 fs_search_files, fs_grep, fs_delete_file, fs_file_info")
    mcp.run()  # stdio transport by default
