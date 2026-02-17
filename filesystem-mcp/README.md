# filesystem-mcp

A **simple, well-structured MCP server** that gives any MCP-compatible client (Claude Desktop, Cursor, Continue, etc.) seven practical filesystem tools — all written in Python using FastMCP.

---

## What is MCP?

**Model Context Protocol (MCP)** is an open standard that lets LLMs call external tools in a structured, safe way. Think of it as a typed API contract between an AI assistant and the outside world.

```
Claude Desktop ←──stdio──→ filesystem_mcp server ←──Python──→ Your filesystem
```

The server runs as a subprocess. The client (Claude Desktop) sends JSON-RPC messages over **stdin/stdout**; the server replies with tool results.

---

## Tools provided

| Tool | What it does |
|---|---|
| `fs_read_file` | Read a text file, optionally capped to N lines |
| `fs_write_file` | Write (or overwrite) a text file |
| `fs_list_directory` | List files and folders in a directory |
| `fs_search_files` | Find files by glob pattern (`*.py`, `README*`, …) |
| `fs_grep` | Search for a string inside files |
| `fs_delete_file` | Delete a single file |
| `fs_file_info` | Get metadata without reading the file |

All tools are scoped to the **working directory** the server is launched from. Attempts to escape via `../` are rejected automatically.

---

## Quick start

### 1. Install dependencies

```bash
cd filesystem-mcp
pip install "mcp[cli]>=1.0.0" pydantic
```

### 2. Verify it starts

```bash
python server.py
# Starting filesystem_mcp …
# Working directory: /your/current/dir
# Tools available: fs_read_file, fs_write_file, …
# (Ctrl-C to stop)
```

### 3. Test with MCP Inspector

```bash
npx @modelcontextprotocol/inspector python server.py
```

Open the URL it prints. You can call every tool interactively from the browser — no Claude required.

---

## Connect to Claude Desktop

1. Open (or create) **`~/Library/Application Support/Claude/claude_desktop_config.json`** on macOS  
   (Windows: `%APPDATA%\Claude\claude_desktop_config.json`)

2. Add the server entry — use the **absolute path** to `server.py`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "python",
      "args": ["/absolute/path/to/filesystem-mcp/server.py"]
    }
  }
}
```

3. Restart Claude Desktop. You'll see a 🔨 icon in the chat input bar when tools are available.

4. Ask Claude something like:

> *"List all Python files in my project, then show me the contents of main.py."*

---

## Project layout

```
filesystem-mcp/
├── server.py                       # The entire MCP server (single file)
├── pyproject.toml                  # Project metadata & dependencies
├── claude_desktop_config.example.json   # Paste into Claude Desktop config
└── README.md
```

---

## How the code is structured

```
server.py
│
├── WORK_DIR                ← root path; all file access is jailed here
│
├── _safe_path()            ← resolves & validates paths (no ../escape)
├── _file_info()            ← shared metadata dict builder
├── _handle_error()         ← consistent error strings for all tools
│
├── Input models (Pydantic)
│   ├── ReadFileInput
│   ├── WriteFileInput
│   ├── ListDirInput
│   ├── SearchFilesInput
│   ├── GrepInput
│   └── DeleteFileInput
│
└── @mcp.tool handlers
    ├── fs_read_file
    ├── fs_write_file
    ├── fs_list_directory
    ├── fs_search_files
    ├── fs_grep
    ├── fs_delete_file
    └── fs_file_info
```

### Key design choices

**FastMCP + Pydantic**  
Every tool accepts a single Pydantic `BaseModel`. FastMCP auto-generates the JSON Schema that the MCP client uses to validate inputs before they ever reach your code.

```python
class ReadFileInput(BaseModel):
    path: str = Field(..., description="Path to the file", min_length=1)
    max_lines: Optional[int] = Field(default=None, ge=1, le=10_000)

@mcp.tool(name="fs_read_file", annotations={"readOnlyHint": True, ...})
async def fs_read_file(params: ReadFileInput) -> str:
    ...
```

**Path jailing**  
`_safe_path()` resolves symlinks and ensures the result starts with `WORK_DIR`. A query like `path="../../../etc/passwd"` is rejected before any I/O happens.

```python
def _safe_path(raw: str) -> Path:
    resolved = (WORK_DIR / raw).resolve()
    if not str(resolved).startswith(str(WORK_DIR)):
        raise ValueError("Path escapes working directory")
    return resolved
```

**Annotations**  
Each tool declares `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`. Clients can use these to ask the user for confirmation before running destructive operations.

**Consistent error handling**  
All tools pass exceptions through `_handle_error()`, which returns a plain English string with a suggested fix — no stack traces dumped on the LLM.

---

## Example tool calls

### Read a file (first 20 lines)
```json
{
  "tool": "fs_read_file",
  "arguments": { "path": "server.py", "max_lines": 20 }
}
```

### Write a new file
```json
{
  "tool": "fs_write_file",
  "arguments": {
    "path": "notes/idea.txt",
    "content": "Remember to add pagination next sprint.",
    "overwrite": false
  }
}
```

### Find all Python files recursively
```json
{
  "tool": "fs_search_files",
  "arguments": { "pattern": "*.py", "recursive": true }
}
```

### Grep for a string across all `.md` files
```json
{
  "tool": "fs_grep",
  "arguments": {
    "text": "TODO",
    "file_pattern": "*.md",
    "case_sensitive": false
  }
}
```

---

## Extending the server

Adding a new tool takes about 15 lines:

```python
class RenameInput(BaseModel):
    src: str = Field(..., description="Current file path")
    dst: str = Field(..., description="New file path")

@mcp.tool(name="fs_rename_file", annotations={"destructiveHint": True, ...})
async def fs_rename_file(params: RenameInput) -> str:
    """Rename or move a file within the working directory."""
    try:
        _safe_path(params.src).rename(_safe_path(params.dst))
        return f"✓ Renamed '{params.src}' → '{params.dst}'"
    except Exception as e:
        return _handle_error(e)
```

---

## Requirements

- Python 3.10+
- `mcp[cli] >= 1.0.0`
- `pydantic >= 2.0`

Install everything at once:

```bash
pip install "mcp[cli]>=1.0.0" "pydantic>=2.0"
```

---

## Security notes

- The server **only exposes files inside the working directory** it is launched from.
- It does **not** delete directories, create symlinks, or execute code.
- Mark it read-only in your MCP config if you don't want write access.
- For production use, consider running it as a dedicated user with a narrow home directory.
