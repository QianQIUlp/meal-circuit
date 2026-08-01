from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

from mealcircuit.configuration import initialize_private_home
from mealcircuit.db import init_db
from mealcircuit.server import Handler
from mealcircuit.storage import app_home, data_home_identity


class DesktopAlreadyRunningError(RuntimeError):
    pass


class _DesktopHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        _write_startup_error_log(
            "Embedded loopback request failed:\n" + traceback.format_exc()
        )


@contextlib.contextmanager
def _single_instance() -> Iterator[None]:
    if sys.platform != "win32":
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    mutex_name = f"Local\\MealCircuit-{data_home_identity(app_home())}"
    ctypes.set_last_error(0)
    handle = create_mutex(None, False, mutex_name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "无法创建 MealCircuit 单实例互斥锁")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        close_handle(handle)
        raise DesktopAlreadyRunningError("MealCircuit 已经在这个 Windows 账户中运行")
    try:
        yield
    finally:
        close_handle(handle)


def _startup_log_directories() -> tuple[Path, ...]:
    directories: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        directories.append(Path(local_app_data) / "MealCircuit" / "logs")
    directories.append(Path(tempfile.gettempdir()) / "MealCircuit" / "logs")
    return tuple(dict.fromkeys(directories))


def _write_startup_error_log(details: str) -> Path | None:
    filename = (
        f"MealCircuit-startup-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-"
        f"{os.getpid()}-{time.time_ns()}.log"
    )
    for directory in _startup_log_directories():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / filename
            path.write_text(details, encoding="utf-8")
        except OSError:
            continue
        return path
    return None


def _show_windows_error(message: str) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(0, message, "MealCircuit", 0x10)


def _show_startup_error(log_path: Path | None, *, use_dialog: bool = True) -> None:
    if log_path is None:
        message = "MealCircuit 无法启动，也无法写入本机诊断日志。"
    else:
        message = f"MealCircuit 无法启动。详细信息已写入本机日志：\n{log_path}"
    if use_dialog and sys.platform == "win32":
        try:
            _show_windows_error(message)
            return
        except (AttributeError, OSError):
            pass
    print(message, file=sys.stderr)


def _probe_loopback_server(port: int) -> None:
    """Check the embedded server without honoring machine proxy settings."""
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", "/setup")
        response = connection.getresponse()
        if response.status != 200 or b"MealCircuit" not in response.read():
            raise RuntimeError("desktop smoke test failed")
    finally:
        connection.close()


def _run_application(args: argparse.Namespace) -> None:
    initialize_private_home()
    init_db()
    server = _DesktopHTTPServer(("127.0.0.1", 0), Handler)
    server.allow_remote = False
    address = f"http://127.0.0.1:{server.server_address[1]}"
    worker = threading.Thread(target=server.serve_forever, name="mealcircuit-local-web", daemon=True)
    worker.start()
    try:
        if args.smoke_test:
            _probe_loopback_server(server.server_address[1])
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


def _run() -> None:
    parser = argparse.ArgumentParser(description="启动 MealCircuit 桌面客户端")
    parser.add_argument("--browser", action="store_true", help="使用系统浏览器作为故障回退")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    with _single_instance():
        _run_application(args)


def main() -> None:
    try:
        _run()
    except DesktopAlreadyRunningError as exc:
        if "--smoke-test" not in sys.argv[1:] and sys.platform == "win32":
            try:
                _show_windows_error(str(exc))
            except (AttributeError, OSError):
                print(str(exc), file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        log_path = _write_startup_error_log(traceback.format_exc())
        _show_startup_error(log_path, use_dialog="--smoke-test" not in sys.argv[1:])
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
