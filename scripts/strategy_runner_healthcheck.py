"""oxmgr health probe for the managed strategy runner socket."""

from __future__ import annotations

import socket
import sys
import json

import config


def main() -> int:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(config.STRATEGY_RUNNER_SOCKET)
            client.sendall(b'{"protocol_version":1,"request_id":"health","type":"health"}\n')
            with client.makefile("rb") as stream:
                response = json.loads(stream.readline().decode("utf-8"))
            if response.get("ok") is not True or response.get("result", {}).get("status") != "ready":
                raise RuntimeError(response.get("error") or "runner health response was not ready")
    except OSError as exc:
        print(f"strategy runner unavailable: {exc}", file=sys.stderr)
        return 1
    print("strategy runner ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
