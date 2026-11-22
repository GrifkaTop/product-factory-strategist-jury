"""Offer verified, read-only local state through MCP over stdio."""
import json

from . import __version__
from .errors import StrategistError
from .grounds import list_cases, trace
from .knowledge_search import search_knowledge
from .review import build_review
from .state import REGISTRIES, load_case


_TOOLS = {
    "list_cases": ("Проверенные кейсы и сохранённые следующие шаги", (), ()),
    "case_review": ("Снимок решения, рисков и обязательств", ("case_id",), ()),
    "trace_record": ("Явные связи записи до источников", ("case_id", "record_id"), ("registry",)),
    "search_methods": ("Темы и локаторы в базе методик", ("query",), ()),
}
_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _definitions():
    return [{
        "name": name, "description": details,
        "inputSchema": {"type": "object", "required": list(required), "additionalProperties": False,
                        "properties": {key: {"type": "string"} for key in required + optional}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    } for name, (details, required, optional) in _TOOLS.items()]


def _tool_call(root, name, args):
    if name == "list_cases":
        return list_cases(root)
    if name == "search_methods":
        return search_knowledge(root, args["query"])
    state = load_case(root, args["case_id"])
    if name == "case_review":
        return build_review(state)
    return trace(state, args["record_id"], args.get("registry"))


def serve(root, reader, writer):
    def send(payload):
        writer.write(json.dumps({"jsonrpc": "2.0", **payload}, ensure_ascii=True) + "\n")
        writer.flush()

    def error(identity, code, message):
        send({"id": identity, "error": {"code": code, "message": message}})

    initialized = False
    negotiated = False
    while line := reader.readline(1_000_001):
        if len(line) > 1_000_000:
            error(None, -32700, "Message too large")
            break
        try:
            request = json.loads(line)
        except (ValueError, RecursionError):
            error(None, -32700, "Invalid JSON")
            continue
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or ("id" in request and type(request["id"]) not in (str, int))):
            error(None, -32600, "Invalid request")
            continue
        method, identity = request["method"], request.get("id")
        if identity is None:
            if method == "notifications/initialized" and negotiated:
                initialized = True
            continue
        params = request.get("params", {})
        if not isinstance(params, dict):
            error(identity, -32602, "Invalid params")
            continue
        if method == "initialize" and not negotiated and isinstance(params.get("protocolVersion"), str):
            requested = params["protocolVersion"]
            result = {"protocolVersion": requested if requested in _PROTOCOLS else _PROTOCOLS[0],
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "product-factory-strategist", "version": __version__}}
            negotiated = True
        elif method == "ping":
            result = {}
        elif not initialized:
            error(identity, -32002, "Server not initialized")
            continue
        elif method == "tools/list":
            result = {"tools": _definitions()}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in _TOOLS or not isinstance(args, dict):
                error(identity, -32602, "Unknown tool or invalid arguments")
                continue
            _, required, optional = _TOOLS[name]
            if (not set(required).issubset(args) or set(args) - set(required + optional)
                    or any(not isinstance(value, str) or not value.strip() for value in args.values())
                    or ("registry" in args and args["registry"] not in REGISTRIES)):
                error(identity, -32602, "Invalid tool arguments")
                continue
            try:
                value = _tool_call(root, name, args)
            except (StrategistError, OSError, ValueError) as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            else:
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        else:
            error(identity, -32601, "Method not found")
            continue
        send({"id": identity, "result": result})
