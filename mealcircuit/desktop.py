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


UI_SMOKE_TIMEOUT_SECONDS = 45.0
UI_SMOKE_SHUTDOWN_GRACE_SECONDS = 5.0


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


def _run_ui_smoke(webview, address: str, *, timeout: float) -> None:
    """Open the real embedded browser and close it after its first page load."""
    window = webview.create_window("MealCircuit", address, min_size=(360, 640))
    result: dict[str, object] = {"loaded": False}

    def close_after_load() -> None:
        try:
            result["loaded"] = window.events.loaded.wait(timeout)
            window.destroy()
        except Exception as exc:
            result["error"] = exc

    # Window.destroy normally returns the native event loop. A final process
    # watchdog keeps this purpose-built CLI check bounded even if the WebView
    # runtime itself hangs while starting or shutting down.
    hard_timeout = threading.Timer(
        timeout + UI_SMOKE_SHUTDOWN_GRACE_SECONDS,
        os._exit,
        args=(1,),
    )
    hard_timeout.daemon = True
    hard_timeout.start()

    # pywebview runs this callback outside the GUI thread after the native
    # event loop has started. Waiting on ``loaded`` therefore exercises the
    # selected Windows WebView backend instead of only probing the HTTP server.
    try:
        webview.start(close_after_load, private_mode=True)
    finally:
        hard_timeout.cancel()
    error = result.get("error")
    if isinstance(error, BaseException):
        raise RuntimeError("desktop UI smoke test could not close its window") from error
    if not result["loaded"]:
        raise RuntimeError(
            f"desktop UI smoke test timed out after {timeout:g} seconds"
        )


@contextlib.contextmanager
def _isolated_ui_smoke_home(enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    names = ("MEALCIRCUIT_HOME", "MEALCIRCUIT_DB", "DIETOS_DB")
    previous = {name: os.environ.get(name) for name in names}
    with tempfile.TemporaryDirectory(prefix="mealcircuit-ui-smoke-") as temp_name:
        os.environ["MEALCIRCUIT_HOME"] = str(Path(temp_name) / "home")
        os.environ.pop("MEALCIRCUIT_DB", None)
        os.environ.pop("DIETOS_DB", None)
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


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
            if args.ui_smoke_test:
                raise RuntimeError("desktop UI smoke test requires pywebview") from None
            webbrowser.open(address)
            worker.join()
            return
        if args.ui_smoke_test:
            _probe_loopback_server(server.server_address[1])
            _run_ui_smoke(webview, address, timeout=UI_SMOKE_TIMEOUT_SECONDS)
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
        worker.join(timeout=5)


def _run() -> None:
    parser = argparse.ArgumentParser(description="启动 MealCircuit 桌面客户端")
    parser.add_argument("--browser", action="store_true", help="使用系统浏览器作为故障回退")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    with _isolated_ui_smoke_home(args.ui_smoke_test):
        with _single_instance():
            _run_application(args)


def _smoke_mode_requested() -> bool:
    return any(
        argument in {"--smoke-test", "--ui-smoke-test"}
        for argument in sys.argv[1:]
    )


def main() -> None:
    try:
        _run()
    except DesktopAlreadyRunningError as exc:
        if not _smoke_mode_requested() and sys.platform == "win32":
            try:
                _show_windows_error(str(exc))
            except (AttributeError, OSError):
                print(str(exc), file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        log_path = _write_startup_error_log(traceback.format_exc())
        _show_startup_error(log_path, use_dialog=not _smoke_mode_requested())
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
