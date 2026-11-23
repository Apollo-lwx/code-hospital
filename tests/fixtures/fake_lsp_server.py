"""假 LSP 语言服务：stdio 上的 JSON-RPC（Content-Length 分帧）。

给 tests/test_lsp.py 当夹具：initialize 回空 capabilities；references 回
环境变量 FAKE_LSP_REFS（JSON 数组，元素 {uri, line, character}）指定的位置；
shutdown 应答、exit 退出。其他请求一律回方法未找到。
"""

import json
import os
import sys


def _read_message(stream):
    length = None
    while True:
        line = stream.readline()
        if not line:
            raise EOFError
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


def _send(stream, message):
    body = json.dumps(message).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def _refs():
    raw = os.environ.get("FAKE_LSP_REFS") or "[]"
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [
        {
            "uri": str(row.get("uri") or ""),
            "range": {
                "start": {"line": int(row.get("line") or 0), "character": int(row.get("character") or 0)},
                "end": {"line": int(row.get("line") or 0), "character": int(row.get("character") or 0) + 1},
            },
        }
        for row in rows
    ]


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            message = _read_message(stdin)
        except (EOFError, OSError):
            return
        method = message.get("method")
        ident = message.get("id")
        if method == "exit":
            return
        if method == "textDocument/didOpen":
            # 客户端的「分析跑过了」屏障在等这条诊断通知（空诊断也算）。
            uri = ((message.get("params") or {}).get("textDocument") or {}).get("uri") or ""
            _send(stdout, {
                "jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            })
            continue
        if ident is None:
            continue  # 其余通知（initialized 等）不应答
        if method == "initialize":
            _send(stdout, {"jsonrpc": "2.0", "id": ident, "result": {"capabilities": {}}})
        elif method == "textDocument/references":
            _send(stdout, {"jsonrpc": "2.0", "id": ident, "result": _refs()})
        elif method == "shutdown":
            _send(stdout, {"jsonrpc": "2.0", "id": ident, "result": None})
        else:
            _send(stdout, {
                "jsonrpc": "2.0", "id": ident,
                "error": {"code": -32601, "message": f"unknown method {method}"},
            })


if __name__ == "__main__":
    main()
