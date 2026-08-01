from __future__ import annotations

import functools
import hashlib
import math
import os
import shutil
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .validation import ValidationError


ROOT = Path(__file__).resolve().parent.parent
_WARNED_LEGACY: set[str] = set()
DATA_DIRECTORY_LOCK = threading.RLock()
_BACKGROUND_DATA_OPERATIONS = 0
_PROCESS_DATA_LOCK_DEPTH = 0
_PROCESS_DATA_LOCK_HOME: Path | None = None
_PROCESS_DATA_LOCK_STREAM = None
WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    *(f"com{number}" for number in "¹²³"),
    *(f"lpt{number}" for number in "¹²³"),
}


class DataDirectoryBusyError(ValidationError):
    """The selected data directory is being changed by another process."""


def data_home_identity(home: Path) -> str:
    value = os.path.abspath(os.fspath(home))
    if os.name == "nt":
        value = value.casefold()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _process_data_lock_path(home: Path) -> Path:
    return home.parent / f".mealcircuit-import-lock-{data_home_identity(home)}"


def _try_lock_stream(stream) -> bool:
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_stream(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _windows_data_mutex_name(home: Path) -> str:
    value = os.path.abspath(os.fspath(home)).casefold()
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return (
        "Global\\MealCircuit.2a7c0c93-763f-4d3c-93f1-c8a5768da92a."
        f"DataLock.{digest}"
    )


@contextmanager
def _windows_named_data_mutex(
    home: Path,
    *,
    wait: bool,
    timeout_seconds: float,
) -> Iterator[object]:
    """Acquire the Windows data lock without creating a replaceable sibling file."""
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    mutex_modify_state = 0x00000001
    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    wait_timeout = 0x00000102
    wait_failed = 0xFFFFFFFF
    sddl_revision_1 = 1
    private_mutex_sddl = (
        "D:P"
        "(A;;GA;;;SY)"
        "(A;;GA;;;BA)"
        "(A;;GA;;;OW)"
    )

    class SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        )

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_sddl.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    )
    convert_sddl.restype = wintypes.BOOL
    create_mutex = kernel32.CreateMutexExW
    create_mutex.argtypes = (
        ctypes.POINTER(SecurityAttributes),
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    create_mutex.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = (wintypes.HANDLE,)
    release_mutex.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL

    descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.ULONG(0)
    handle = wintypes.HANDLE()
    acquired = False
    try:
        if not convert_sddl(
            private_mutex_sddl,
            sddl_revision_1,
            ctypes.byref(descriptor),
            ctypes.byref(descriptor_size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes),
            descriptor,
            False,
        )
        handle = create_mutex(
            ctypes.byref(attributes),
            _windows_data_mutex_name(home),
            0,
            synchronize | mutex_modify_state,
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        timeout_milliseconds = (
            0
            if not wait
            else min(
                0xFFFFFFFE,
                max(0, math.ceil(max(0.0, timeout_seconds) * 1000.0)),
            )
        )
        wait_result = int(wait_for_single_object(handle, timeout_milliseconds))
        if wait_result in (wait_object_0, wait_abandoned):
            acquired = True
        elif wait_result == wait_timeout:
            raise DataDirectoryBusyError(
                "另一 MealCircuit 进程正在读取或更新数据；请稍后重试"
            )
        elif wait_result == wait_failed:
            raise ctypes.WinError(ctypes.get_last_error())
        else:
            raise OSError(f"Windows 数据互斥锁返回未知等待状态：{wait_result}")
        yield handle
    finally:
        release_error: OSError | None = None
        if acquired and not release_mutex(handle):
            release_error = ctypes.WinError(ctypes.get_last_error())
        if handle:
            close_handle(handle)
        if descriptor.value:
            local_free(descriptor)
        if release_error is not None:
            raise DataDirectoryBusyError("无法释放 Windows 数据目录互斥锁") from release_error


@contextmanager
def process_data_lock(
    home: Path | None = None,
    *,
    wait: bool = True,
    timeout_seconds: float = 120.0,
) -> Iterator[None]:
    """Serialize data access with whole-home replacement across local processes."""
    global _PROCESS_DATA_LOCK_DEPTH, _PROCESS_DATA_LOCK_HOME, _PROCESS_DATA_LOCK_STREAM
    with DATA_DIRECTORY_LOCK:
        if _PROCESS_DATA_LOCK_DEPTH:
            _PROCESS_DATA_LOCK_DEPTH += 1
            try:
                yield
            finally:
                _PROCESS_DATA_LOCK_DEPTH -= 1
            return

        resolved_home = _lexical_absolute_path(home or app_home())
        if os.name == "nt":
            with _windows_named_data_mutex(
                resolved_home,
                wait=wait,
                timeout_seconds=timeout_seconds,
            ) as handle:
                _PROCESS_DATA_LOCK_DEPTH = 1
                _PROCESS_DATA_LOCK_HOME = resolved_home
                _PROCESS_DATA_LOCK_STREAM = handle
                try:
                    yield
                finally:
                    _PROCESS_DATA_LOCK_DEPTH = 0
                    _PROCESS_DATA_LOCK_HOME = None
                    _PROCESS_DATA_LOCK_STREAM = None
            return
        lock_path = _process_data_lock_path(resolved_home)
        ensure_secure_directory(lock_path.parent)
        try:
            stream = lock_path.open("a+b")
        except OSError as exc:
            raise DataDirectoryBusyError(
                f"无法打开数据目录互斥锁：{lock_path}"
            ) from exc
        acquired = False
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + max(0.0, timeout_seconds)
            while not (acquired := _try_lock_stream(stream)):
                if not wait or time.monotonic() >= deadline:
                    raise DataDirectoryBusyError(
                        "另一 MealCircuit 进程正在读取或更新数据；请稍后重试"
                    )
                time.sleep(0.05)
            _PROCESS_DATA_LOCK_DEPTH = 1
            _PROCESS_DATA_LOCK_HOME = resolved_home
            _PROCESS_DATA_LOCK_STREAM = stream
            try:
                yield
            finally:
                _PROCESS_DATA_LOCK_DEPTH = 0
                _PROCESS_DATA_LOCK_HOME = None
                _PROCESS_DATA_LOCK_STREAM = None
        finally:
            if acquired:
                try:
                    _unlock_stream(stream)
                except OSError:
                    pass
            stream.close()


def process_data_locked(*, wait: bool = True, timeout_seconds: float = 120.0):
    def decorator(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with process_data_lock(wait=wait, timeout_seconds=timeout_seconds):
                return function(*args, **kwargs)

        return wrapped

    return decorator


def data_directory_locked(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with DATA_DIRECTORY_LOCK:
            return function(*args, **kwargs)

    return wrapped


@contextmanager
def background_data_operation() -> Iterator[None]:
    """Mark a long-running worker without holding the data lock during network waits."""
    global _BACKGROUND_DATA_OPERATIONS
    with DATA_DIRECTORY_LOCK:
        _BACKGROUND_DATA_OPERATIONS += 1
    try:
        yield
    finally:
        with DATA_DIRECTORY_LOCK:
            _BACKGROUND_DATA_OPERATIONS -= 1


def background_data_operations_active() -> bool:
    with DATA_DIRECTORY_LOCK:
        return _BACKGROUND_DATA_OPERATIONS > 0


def _environment(name: str, legacy: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    if legacy and os.environ.get(legacy):
        if legacy not in _WARNED_LEGACY:
            print(f"警告：{legacy} 已弃用，请改用 {name}", file=sys.stderr)
            _WARNED_LEGACY.add(legacy)
        return os.environ[legacy]
    return None


@data_directory_locked
def app_home() -> Path:
    configured = _environment("MEALCIRCUIT_HOME")
    if configured:
        expanded = Path(configured).expanduser()
        return (
            Path(os.path.abspath(os.fspath(expanded)))
            if os.name == "nt"
            else expanded.resolve()
        )
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(os.path.abspath(os.fspath(Path(base) / "MealCircuit")))
        return Path(
            os.path.abspath(os.fspath(Path.home() / "AppData" / "Local" / "MealCircuit"))
        )
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "MealCircuit").resolve()
    base = os.environ.get("XDG_DATA_HOME")
    return ((Path(base).expanduser() if base else Path.home() / ".local" / "share") / "mealcircuit").resolve()


@data_directory_locked
def db_path() -> Path:
    configured = _environment("MEALCIRCUIT_DB", "DIETOS_DB")
    if not configured:
        return app_home() / "mealcircuit.db"
    expanded = Path(configured).expanduser()
    return (
        Path(os.path.abspath(os.fspath(expanded)))
        if os.name == "nt"
        else expanded.resolve()
    )


def port_value() -> int:
    configured = _environment("MEALCIRCUIT_PORT", "DIETOS_PORT")
    return int(configured or "8765")


def upload_root() -> Path:
    return app_home() / "uploads"


def food_label_root() -> Path:
    return app_home() / "food-labels"


def managed_asset_root() -> Path:
    return app_home() / "assets"


def _reject_external_path_namespace(value: str | Path) -> None:
    """Reject Windows network/device paths before any filesystem resolution."""
    raw = os.fspath(value)
    normalized = raw.replace("/", "\\")
    if (
        normalized.startswith("\\\\")
        or normalized.startswith("\\??\\")
        or normalized.casefold().startswith("\\globalroot\\")
    ):
        raise ValidationError("媒体路径不能使用网络共享或设备命名空间")
    if os.name == "nt":
        path = Path(raw)
        if path.drive and not path.is_absolute():
            raise ValidationError("媒体路径不能使用驱动器相对路径")
        for part in path.parts[1 if path.anchor else 0 :]:
            stem = part.rstrip(" .").split(".", 1)[0].casefold()
            if (
                not part
                or part.endswith((" ", "."))
                or ":" in part
                or any(ord(character) < 32 for character in part)
                or any(character in '<>"|?*' for character in part)
                or stem in WINDOWS_RESERVED_NAMES
            ):
                raise ValidationError("媒体路径包含 Windows 不安全文件名")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _lexical_absolute_path(value: str | Path) -> Path:
    """Return an absolute path without resolving links or reparse points."""
    return Path(os.path.abspath(os.fspath(value)))


def _validate_existing_directory_ancestors(path: Path) -> None:
    """Inspect every existing component from the volume anchor without following links."""
    anchor = Path(path.anchor)
    if not anchor.anchor:
        raise ValidationError(f"安全目录路径不是绝对路径：{path}")
    current = anchor
    components = (anchor,)
    relative = path.relative_to(anchor)
    for part in relative.parts:
        current = current / part
        components += (current,)
    for component in components:
        try:
            result = component.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ValidationError(f"无法检查安全目录路径：{component}") from exc
        if _stat_is_reparse_point(component, result) or not stat.S_ISDIR(result.st_mode):
            raise ValidationError(f"安全目录路径包含重解析点或非目录：{component}")


def _stat_is_reparse_point(path: Path, result: os.stat_result) -> bool:
    attributes = int(getattr(result, "st_file_attributes", 0))
    is_junction = getattr(path, "is_junction", None)
    return (
        stat.S_ISLNK(result.st_mode)
        or bool(attributes & 0x400)
        or bool(callable(is_junction) and is_junction())
    )


def path_is_reparse_point(path: str | Path) -> bool:
    """Inspect one lexical path without following its final component."""
    candidate = Path(path)
    try:
        result = candidate.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValidationError(f"无法检查路径安全属性：{candidate}") from exc
    return _stat_is_reparse_point(candidate, result)


def _raise_tree_walk_error(error: OSError) -> None:
    blocked = Path(error.filename) if error.filename else Path("<unknown>")
    raise ValidationError(f"无法完整读取目录树：{blocked}") from error


def _require_safe_windows_dacl(
    path: Path,
    *,
    present: bool,
    address: int,
    control: int,
    require_protected: bool,
) -> None:
    """Reject absent/NULL DACLs and directories that still inherit access."""
    if not present or not address:
        raise ValidationError(f"目录缺少安全 DACL：{path}")
    if require_protected and not control & 0x1000:
        raise ValidationError(f"目录 DACL 仍允许从父目录继承：{path}")


def _create_windows_secure_directory(path: Path, dacl_source: Path | None) -> None:
    """Create one directory with a protected DACL in the same system call."""
    import ctypes
    from ctypes import wintypes

    dacl_security_information = 0x00000004
    se_dacl_protected = 0x1000
    sddl_revision_1 = 1
    private_directory_sddl = (
        "D:P"
        "(A;OICI;FA;;;SY)"
        "(A;OICI;FA;;;BA)"
        "(A;OICI;FA;;;OW)"
    )

    class SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        )

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_file_security = advapi32.GetFileSecurityW
    get_file_security.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_file_security.restype = wintypes.BOOL
    set_security_descriptor_control = advapi32.SetSecurityDescriptorControl
    set_security_descriptor_control.argtypes = (
        wintypes.LPVOID,
        wintypes.WORD,
        wintypes.WORD,
    )
    set_security_descriptor_control.restype = wintypes.BOOL
    get_security_descriptor_dacl = advapi32.GetSecurityDescriptorDacl
    get_security_descriptor_dacl.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    )
    get_security_descriptor_dacl.restype = wintypes.BOOL
    get_security_descriptor_control = advapi32.GetSecurityDescriptorControl
    get_security_descriptor_control.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    get_security_descriptor_control.restype = wintypes.BOOL
    convert_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_sddl.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    )
    convert_sddl.restype = wintypes.BOOL
    create_directory = kernel32.CreateDirectoryW
    create_directory.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(SecurityAttributes),
    )
    create_directory.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL

    def read_descriptor(source: Path):
        needed = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        loaded = get_file_security(
            str(source),
            dacl_security_information,
            None,
            0,
            ctypes.byref(needed),
        )
        first_error = ctypes.get_last_error()
        if not loaded and first_error not in (0, 122):
            raise ctypes.WinError(first_error)
        if not needed.value:
            raise ctypes.WinError(first_error)
        value = ctypes.create_string_buffer(needed.value)
        if not get_file_security(
            str(source),
            dacl_security_information,
            value,
            len(value),
            ctypes.byref(needed),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def validate_descriptor(
        descriptor,
        source: Path,
        *,
        require_protected: bool,
    ) -> None:
        descriptor_pointer = ctypes.cast(descriptor, wintypes.LPVOID)
        dacl_present = wintypes.BOOL(False)
        dacl_pointer = wintypes.LPVOID()
        dacl_defaulted = wintypes.BOOL(False)
        if not get_security_descriptor_dacl(
            descriptor_pointer,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl_pointer),
            ctypes.byref(dacl_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        control = wintypes.WORD(0)
        revision = wintypes.DWORD(0)
        if not get_security_descriptor_control(
            descriptor_pointer,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        _require_safe_windows_dacl(
            source,
            present=bool(dacl_present.value),
            address=int(dacl_pointer.value or 0),
            control=int(control.value),
            require_protected=require_protected,
        )

    descriptor_buffer = None
    allocated_descriptor = wintypes.LPVOID()
    descriptor_pointer = wintypes.LPVOID()
    created = False
    try:
        if dacl_source is not None:
            descriptor_buffer = read_descriptor(dacl_source)
            validate_descriptor(
                descriptor_buffer,
                dacl_source,
                require_protected=False,
            )
            if not set_security_descriptor_control(
                descriptor_buffer,
                se_dacl_protected,
                se_dacl_protected,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            descriptor_pointer = ctypes.cast(
                descriptor_buffer,
                wintypes.LPVOID,
            )
        else:
            descriptor_size = wintypes.ULONG(0)
            if not convert_sddl(
                private_directory_sddl,
                sddl_revision_1,
                ctypes.byref(allocated_descriptor),
                ctypes.byref(descriptor_size),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            descriptor_pointer = allocated_descriptor

        attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes),
            descriptor_pointer,
            False,
        )
        if not create_directory(str(path), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        created = True
        created_descriptor = read_descriptor(path)
        validate_descriptor(
            created_descriptor,
            path,
            require_protected=True,
        )
    except BaseException as primary_error:
        if created:
            try:
                path.rmdir()
            except OSError as cleanup_error:
                raise ValidationError(
                    f"安全目录验证失败且无法清理：{path}"
                ) from cleanup_error
        raise primary_error
    finally:
        if allocated_descriptor.value:
            local_free(allocated_descriptor)


def create_secure_directory(
    path: str | Path,
    *,
    dacl_source: str | Path | None = None,
) -> Path:
    """Securely create a missing directory chain without widening leaf access."""
    target = _lexical_absolute_path(path)
    source = _lexical_absolute_path(dacl_source) if dacl_source is not None else None
    if source is not None:
        _validate_existing_directory_ancestors(source)
    missing: list[Path] = []
    current = target
    while True:
        try:
            result = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise ValidationError(f"无法找到安全目录的既存父目录：{target}")
            current = parent
            continue
        except OSError as exc:
            raise ValidationError(f"无法检查安全目录路径：{current}") from exc
        if current == target:
            raise ValidationError(f"安全目录已存在：{target}")
        if _stat_is_reparse_point(current, result):
            raise ValidationError(f"安全目录父路径包含重解析点：{current}")
        if not stat.S_ISDIR(result.st_mode):
            raise ValidationError(f"安全目录父路径不是普通目录：{current}")
        break
    _validate_existing_directory_ancestors(current)

    created: list[Path] = []
    try:
        for directory in reversed(missing):
            if os.name == "nt":
                _create_windows_secure_directory(
                    directory,
                    source if directory == target else None,
                )
            else:
                directory.mkdir(mode=0o700)
            created.append(directory)
    except BaseException as primary_error:
        cleanup_error: OSError | None = None
        for directory in reversed(created):
            try:
                directory.rmdir()
            except FileNotFoundError:
                continue
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise ValidationError(
                f"安全目录创建失败且无法完整清理：{target}"
            ) from cleanup_error
        if isinstance(primary_error, OSError):
            raise ValidationError(f"无法安全创建目录：{target}") from primary_error
        raise
    return target


def ensure_secure_directory(path: str | Path) -> Path:
    """Return an existing ordinary directory, or securely create its missing chain."""
    directory = _lexical_absolute_path(path)
    try:
        result = directory.lstat()
    except FileNotFoundError:
        return create_secure_directory(directory)
    except OSError as exc:
        raise ValidationError(f"无法检查安全目录：{directory}") from exc
    if _stat_is_reparse_point(directory, result):
        raise ValidationError(f"安全目录包含重解析点：{directory}")
    if not stat.S_ISDIR(result.st_mode):
        raise ValidationError(f"安全目录不是普通目录：{directory}")
    _validate_existing_directory_ancestors(directory)
    return directory


def ensure_secure_app_home() -> Path:
    """Create the application home through the protected-directory path if absent."""
    return ensure_secure_directory(app_home())


def validate_private_directory_security(path: str | Path) -> Path:
    """Require an owned directory whose Windows DACL is non-NULL and protected."""
    directory = ensure_secure_directory(path)
    result = directory.lstat()
    if os.name != "nt":
        getuid = getattr(os, "geteuid", None)
        if callable(getuid) and result.st_uid != getuid():
            raise ValidationError(f"私人目录不属于当前用户：{directory}")
        return directory

    import ctypes
    from ctypes import wintypes

    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    token_query = 0x0008
    token_owner_class = 4

    class TokenOwner(ctypes.Structure):
        _fields_ = (("sid", wintypes.LPVOID),)

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_file_security = advapi32.GetFileSecurityW
    get_file_security.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_file_security.restype = wintypes.BOOL
    get_security_descriptor_dacl = advapi32.GetSecurityDescriptorDacl
    get_security_descriptor_dacl.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    )
    get_security_descriptor_dacl.restype = wintypes.BOOL
    get_security_descriptor_control = advapi32.GetSecurityDescriptorControl
    get_security_descriptor_control.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    get_security_descriptor_control.restype = wintypes.BOOL
    get_security_descriptor_owner = advapi32.GetSecurityDescriptorOwner
    get_security_descriptor_owner.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    )
    get_security_descriptor_owner.restype = wintypes.BOOL
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_token_information.restype = wintypes.BOOL
    equal_sid = advapi32.EqualSid
    equal_sid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
    equal_sid.restype = wintypes.BOOL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    try:
        needed = wintypes.DWORD(0)
        information = owner_security_information | dacl_security_information
        ctypes.set_last_error(0)
        loaded = get_file_security(
            str(directory),
            information,
            None,
            0,
            ctypes.byref(needed),
        )
        first_error = ctypes.get_last_error()
        if not loaded and first_error not in (0, 122):
            raise ctypes.WinError(first_error)
        if not needed.value:
            raise ctypes.WinError(first_error)
        descriptor = ctypes.create_string_buffer(needed.value)
        if not get_file_security(
            str(directory),
            information,
            descriptor,
            len(descriptor),
            ctypes.byref(needed),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        descriptor_pointer = ctypes.cast(descriptor, wintypes.LPVOID)
        dacl_present = wintypes.BOOL(False)
        dacl_pointer = wintypes.LPVOID()
        dacl_defaulted = wintypes.BOOL(False)
        if not get_security_descriptor_dacl(
            descriptor_pointer,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl_pointer),
            ctypes.byref(dacl_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        control = wintypes.WORD(0)
        revision = wintypes.DWORD(0)
        if not get_security_descriptor_control(
            descriptor_pointer,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        _require_safe_windows_dacl(
            directory,
            present=bool(dacl_present.value),
            address=int(dacl_pointer.value or 0),
            control=int(control.value),
            require_protected=True,
        )

        owner_pointer = wintypes.LPVOID()
        owner_defaulted = wintypes.BOOL(False)
        if not get_security_descriptor_owner(
            descriptor_pointer,
            ctypes.byref(owner_pointer),
            ctypes.byref(owner_defaulted),
        ) or not owner_pointer.value:
            raise ctypes.WinError(ctypes.get_last_error())

        token = wintypes.HANDLE()
        if not open_process_token(
            get_current_process(),
            token_query,
            ctypes.byref(token),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            token_needed = wintypes.DWORD(0)
            ctypes.set_last_error(0)
            loaded = get_token_information(
                token,
                token_owner_class,
                None,
                0,
                ctypes.byref(token_needed),
            )
            token_error = ctypes.get_last_error()
            if not loaded and token_error not in (0, 122):
                raise ctypes.WinError(token_error)
            if not token_needed.value:
                raise ctypes.WinError(token_error)
            token_buffer = ctypes.create_string_buffer(token_needed.value)
            if not get_token_information(
                token,
                token_owner_class,
                token_buffer,
                len(token_buffer),
                ctypes.byref(token_needed),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            token_owner = ctypes.cast(
                token_buffer,
                ctypes.POINTER(TokenOwner),
            ).contents
            if not equal_sid(owner_pointer, token_owner.sid):
                raise ValidationError(f"私人目录不属于当前用户：{directory}")
        finally:
            close_handle(token)
    except OSError as exc:
        raise ValidationError(f"无法验证私人目录安全属性：{directory}") from exc
    return directory


def ensure_no_reparse_components(path: str | Path, root: str | Path) -> Path:
    """Reject links/junctions in every existing component from root to path."""
    lexical_root = _lexical_absolute_path(root)
    lexical_path = _lexical_absolute_path(path)
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise ValidationError(f"路径逃逸受信目录：{lexical_path}") from exc

    current = lexical_root
    components = [current]
    for part in relative.parts:
        current = current / part
        components.append(current)
    for component in components:
        try:
            result = component.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ValidationError(f"无法检查路径安全属性：{component}") from exc
        if _stat_is_reparse_point(component, result):
            raise ValidationError(f"路径包含符号链接或 Windows 重解析点：{component}")
    return lexical_path


def iter_regular_tree_files(root: str | Path) -> Iterator[Path]:
    """Walk a tree without following links and yield only ordinary files."""
    lexical_root = _lexical_absolute_path(root)
    try:
        root_stat = lexical_root.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValidationError(f"无法检查目录安全属性：{lexical_root}") from exc
    if _stat_is_reparse_point(lexical_root, root_stat):
        raise ValidationError(f"目录包含符号链接或 Windows 重解析点：{lexical_root}")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValidationError(f"预期目录却得到其他文件类型：{lexical_root}")

    for current, directory_names, file_names in os.walk(
        lexical_root,
        topdown=True,
        onerror=_raise_tree_walk_error,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        current_path = Path(current)
        ensure_no_reparse_components(current_path, lexical_root)
        for directory_name in directory_names:
            directory = current_path / directory_name
            try:
                directory_stat = directory.lstat()
            except OSError as exc:
                raise ValidationError(f"无法检查目录安全属性：{directory}") from exc
            if _stat_is_reparse_point(directory, directory_stat):
                raise ValidationError(f"目录包含符号链接或 Windows 重解析点：{directory}")
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise ValidationError(f"目录树包含异常文件类型：{directory}")
        for file_name in file_names:
            file_path = current_path / file_name
            try:
                file_stat = file_path.lstat()
            except OSError as exc:
                raise ValidationError(f"无法检查文件安全属性：{file_path}") from exc
            if _stat_is_reparse_point(file_path, file_stat):
                raise ValidationError(f"目录包含符号链接或 Windows 重解析点：{file_path}")
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValidationError(f"目录树包含异常文件类型：{file_path}")
            yield file_path


def copy_tree_without_reparse_points(source: str | Path, target: str | Path) -> None:
    """Copy an existing tree after a no-follow validation of every source node."""
    lexical_source = _lexical_absolute_path(source)
    lexical_target = _lexical_absolute_path(target)
    try:
        lexical_target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValidationError(f"无法检查安全复制目标：{lexical_target}") from exc
    else:
        raise ValidationError(f"安全复制目标已存在：{lexical_target}")
    target_created = False
    try:
        files = list(iter_regular_tree_files(lexical_source))
        create_secure_directory(
            lexical_target,
            dacl_source=lexical_source,
        )
        target_created = True
        for current, directory_names, _ in os.walk(
            lexical_source,
            topdown=True,
            onerror=_raise_tree_walk_error,
            followlinks=False,
        ):
            current_path = ensure_no_reparse_components(current, lexical_source)
            relative = current_path.relative_to(lexical_source)
            (lexical_target / relative).mkdir(parents=True, exist_ok=True)
            for directory_name in directory_names:
                ensure_no_reparse_components(
                    current_path / directory_name,
                    lexical_source,
                )
        for source_file in files:
            source_file = ensure_no_reparse_components(
                source_file,
                lexical_source,
            )
            source_stat = source_file.lstat()
            if (
                _stat_is_reparse_point(source_file, source_stat)
                or not stat.S_ISREG(source_stat.st_mode)
            ):
                raise ValidationError(
                    f"安全复制期间源文件类型发生变化：{source_file}"
                )
            relative = source_file.relative_to(lexical_source)
            target_file = lexical_target / relative
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file, follow_symlinks=False)
        # Validate again to catch source changes that occurred while copying.
        list(iter_regular_tree_files(lexical_source))
        list(iter_regular_tree_files(lexical_target))
    except BaseException:
        if target_created:
            shutil.rmtree(lexical_target, ignore_errors=True)
        raise


def resolve_managed_media_path(
    value: str | Path,
    *,
    require_file: bool = True,
) -> Path:
    """Resolve a stored media path without allowing external files or UNC access."""
    _reject_external_path_namespace(value)
    home = _lexical_absolute_path(app_home())
    path = Path(value)
    candidate = _lexical_absolute_path(path if path.is_absolute() else home / path)
    allowed_roots = (
        _lexical_absolute_path(home / "uploads"),
        _lexical_absolute_path(home / "food-labels"),
        _lexical_absolute_path(home / "assets"),
    )
    selected_root = next(
        (root for root in allowed_roots if _is_relative_to(candidate, root)),
        None,
    )
    if selected_root is None:
        raise ValidationError("媒体路径不在 MealCircuit 受管目录内")

    ensure_no_reparse_components(selected_root, home)
    ensure_no_reparse_components(candidate, selected_root)
    resolved_home = home.resolve()
    resolved_root = selected_root.resolve()
    if not _is_relative_to(resolved_root, resolved_home):
        raise ValidationError("媒体受管目录逃逸 MealCircuit 数据目录")
    resolved_candidate = candidate.resolve()
    if not _is_relative_to(resolved_candidate, resolved_root):
        raise ValidationError("媒体路径逃逸 MealCircuit 受管目录")
    if require_file and not resolved_candidate.is_file():
        raise ValidationError("媒体文件不存在")
    return resolved_candidate


def profile_path() -> Path:
    return app_home() / "profile.md"


def settings_path() -> Path:
    return app_home() / "settings.json"


@data_directory_locked
def private_doctrine_path() -> Path:
    configured = _environment("MEALCIRCUIT_DOCTRINE")
    if not configured:
        return app_home() / "doctrine.private.md"
    expanded = Path(configured).expanduser()
    return (
        Path(os.path.abspath(os.fspath(expanded)))
        if os.name == "nt"
        else expanded.resolve()
    )


def core_rules_path() -> Path:
    return ROOT / "rules" / "core.md"


def exports_root() -> Path:
    return app_home() / "exports"


def backups_root() -> Path:
    return app_home() / "backups"


def resolve_data_path(value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (app_home() / path).resolve()


def store_data_path(path: str | Path) -> str:
    absolute = Path(path).resolve()
    try:
        return absolute.relative_to(app_home().resolve()).as_posix()
    except ValueError:
        return str(absolute)
