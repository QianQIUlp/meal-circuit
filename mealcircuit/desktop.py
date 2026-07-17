from __future__ import annotations

import argparse
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from mealcircuit.configuration import initialize_private_home
from mealcircuit.db import init_db
from mealcircuit.server import Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 MealCircuit 桌面客户端")
    parser.add_argument("--browser", action="store_true", help="使用系统浏览器作为故障回退")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    initialize_private_home()
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.allow_remote = False
    address = f"http://127.0.0.1:{server.server_address[1]}"
    worker = threading.Thread(target=server.serve_forever, name="mealcircuit-local-web", daemon=True)
    worker.start()
    try:
        if args.smoke_test:
            with urlopen(f"{address}/setup", timeout=10) as response:
                if response.status != 200 or b"MealCircuit" not in response.read():
                    raise RuntimeError("desktop smoke test failed")
            return
        if args.browser:
            webbrowser.open(address)
            worker.join()
            return
        try:
            import webview
        except ImportError:
            webbrowser.open(address)
            worker.join()
            return
        try:
            webview.create_window("MealCircuit", address, min_size=(360, 640))
            webview.start()
        except Exception:
            webbrowser.open(address)
            worker.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
