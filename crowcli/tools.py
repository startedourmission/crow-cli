"""crow_client MCP tools — ported from Crow's agent-history.py
(reverse_tool / reverse_tool_definitions / reverse_serve).

These run on the CLIENT. The agent process runs on a server, but every
filesystem/shell operation lands here, sandboxed to one client workspace
root: canonical paths must stay under the root, exactly as in Crow.
"""
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time


def canonical(path):
    return os.path.realpath(os.path.expanduser(path))


class ToolError(ValueError):
    pass


def run_tool(root, name, args):
    root = canonical(root)
    args = args or {}

    def path(value="."):
        result = canonical(os.path.join(root, value))
        if os.path.commonpath([root, result]) != root:
            raise ToolError("Path is outside the selected client workspace.")
        return result

    if name == "workspace_info":
        import platform
        return {"root": root, "host": os.uname().nodename,
                "platform": platform.system().lower()}
    if name == "list_directory":
        with os.scandir(path(args.get("path", "."))) as entries:
            return [{"name": item.name, "directory": item.is_dir()}
                    for _, item in zip(range(1000), entries)]
    if name == "read_file":
        with open(path(args["path"]), encoding="utf-8") as stream:
            text = stream.read(200001)
        return {"text": text[:200000], "truncated": len(text) > 200000}
    if name in ("write_file", "edit_file"):
        target = path(args["path"])
        if name == "write_file":
            text = args["text"]
            if os.path.exists(target):
                raise ToolError("File exists. Use edit_file with exact "
                                "old_text to change it.")
            mode = "x"
        else:
            with open(target, encoding="utf-8", newline="") as stream:
                original = stream.read(2000001)
            if len(original) > 2000000:
                raise ToolError("File exceeds the edit limit; use shell.")
            old = args["old_text"]
            if not old or original.count(old) != 1:
                raise ToolError("old_text must match exactly once; "
                                "read the file again.")
            text = original.replace(old, args["new_text"], 1)
            mode = "w"
        if not isinstance(text, str) or len(text) > 2000000:
            raise ToolError("Text exceeds the write limit.")
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, mode, encoding="utf-8", newline="") as stream:
            stream.write(text)
        return {"path": target, "written": True}
    if name == "shell":
        directory = path(args.get("cwd", "."))
        timeout = min(120, max(1, float(args.get("timeout", 60))))
        shell = os.environ.get("SHELL") or "/bin/sh"
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [shell, "-lc", "cd -- " + shlex.quote(directory) + " && "
                 + args["command"]],
                cwd=directory, stdin=subprocess.DEVNULL, stdout=output,
                stderr=output, start_new_session=True)
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if time.monotonic() > deadline or \
                            os.fstat(output.fileno()).st_size > 4 * 1024 * 1024:
                        raise ToolError("Client command exceeded its time "
                                        "or output limit.")
                    time.sleep(0.05)
                output.seek(0)
                data = output.read(200001)
                return {"exit_code": process.returncode,
                        "output": data[:200000].decode("utf-8",
                                                       errors="replace"),
                        "truncated": len(data) > 200000, "cwd": directory}
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                process.wait()
    raise ToolError("Unknown client tool.")


def tool_definitions(root):
    def tool(name, description, properties, required=()):
        return {"name": name,
                "description": description + " Runs on the crow-cli CLIENT. "
                "Workspace: " + root,
                "inputSchema": {"type": "object", "properties": properties,
                                "required": list(required),
                                "additionalProperties": False}}
    string = {"type": "string"}
    return [
        tool("workspace_info", "Identify the client and fixed working root.",
             {}),
        tool("list_directory", "List up to 1,000 entries.", {"path": string}),
        tool("read_file", "Read a UTF-8 file (up to 200,000 characters).",
             {"path": string}, ["path"]),
        tool("write_file", "Create a new UTF-8 file. Existing files are "
             "never overwritten.", {"path": string, "text": string},
             ["path", "text"]),
        tool("edit_file", "Replace one exact occurrence of old_text in a "
             "UTF-8 file.", {"path": string, "old_text": string,
                             "new_text": string},
             ["path", "old_text", "new_text"]),
        tool("shell", "Run a shell command on the client. Default cwd is "
             "the workspace.", {"command": string, "cwd": string,
                                "timeout": {"type": "number", "minimum": 1,
                                            "maximum": 120}}, ["command"]),
    ]


def handle_message(root, request):
    """One JSON-RPC request -> response dict (or None for notifications)."""
    identifier = request.get("id")
    if identifier is None:
        return None
    method = request.get("method")
    response = {"jsonrpc": "2.0", "id": identifier}
    try:
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "crow-client", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": tool_definitions(root)}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            params = request["params"]
            try:
                value = run_tool(root, params["name"],
                                 params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(
                    value, ensure_ascii=False)}]}
            except Exception as error:  # tool errors are results, as in Crow
                result = {"isError": True,
                          "content": [{"type": "text", "text": str(error)}]}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
            return response
        response["result"] = result
    except Exception as error:
        response["error"] = {"code": -32602, "message": str(error)}
    return response


def serve_stdio(root):
    """MCP server over stdio (Crow's reverse_serve). Stateless per line."""
    if not os.path.isdir(root):
        raise ToolError("Client workspace no longer exists.")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        response = handle_message(root, request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
