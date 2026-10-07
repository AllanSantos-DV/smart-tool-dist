"""ReadDirectoryChangesW assíncrono: eventos nativos com parada cooperativa."""
import os
import struct
import threading


class DirectoryWatcher:
    def __init__(self, root, on_events, on_error):
        self.root = root
        self.on_events = on_events
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread = None
        self.ready = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="directory-events", daemon=True)
        self._thread.start()

    @property
    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    @staticmethod
    def decode(buffer, length):
        rows, offset = [], 0
        while offset < length:
            if offset + 12 > length:
                raise ValueError("Incomplete directory event.")
            next_offset, action, size = struct.unpack_from("<III", buffer, offset)
            if size % 2 or offset + 12 + size > length:
                raise ValueError("Invalid file name in event.")
            name = buffer[offset + 12:offset + 12 + size].decode("utf-16-le")
            rows.append({"path": name.replace("\\", "/"), "action": action})
            if not next_offset:
                break
            if next_offset < 12 or offset + next_offset >= length:
                raise ValueError("Invalid event offset.")
            offset += next_offset
        return rows

    def _run(self):
        if os.name != "nt":
            self.on_error("Native events unavailable on this platform; periodic reconciliation is active.")
            return
        import ctypes
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Overlapped), ctypes.c_void_p]
        kernel.ReadDirectoryChangesW.restype = wintypes.BOOL
        kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(Overlapped),
                                              ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        kernel.GetOverlappedResult.restype = wintypes.BOOL
        kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel.CancelIoEx.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ResetEvent.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle, event, operation = None, None, None
        pending = False
        try:
            handle = kernel.CreateFileW(self.root, 0x0001, 0x0007, None, 3,
                                        0x02000000 | 0x40000000, None)
            if handle == ctypes.c_void_p(-1).value:
                handle = None
                raise OSError(ctypes.get_last_error(), "Could not watch the folder.")
            event = kernel.CreateEventW(None, True, False, None)
            if not event:
                raise OSError(ctypes.get_last_error(), "Could not create the watch event.")
            buffer = ctypes.create_string_buffer(64 * 1024)
            count = wintypes.DWORD()
            while not self._stop.is_set():
                kernel.ResetEvent(event)
                operation = Overlapped()
                operation.hEvent = event
                ok = kernel.ReadDirectoryChangesW(handle, buffer, len(buffer), True,
                                                   0x0001 | 0x0002 | 0x0008 | 0x0010 | 0x0040,
                                                   None, ctypes.byref(operation), None)
                if not ok and ctypes.get_last_error() != 997:
                    raise OSError(ctypes.get_last_error(), "Failed to receive folder changes.")
                pending = True
                self.ready.set()
                while not self._stop.is_set():
                    status = kernel.WaitForSingleObject(event, 250)
                    if status == 0:
                        break
                    if status != 258:
                        raise OSError(ctypes.get_last_error(), "Failed while waiting for folder events.")
                if self._stop.is_set():
                    break
                ok = kernel.GetOverlappedResult(handle, ctypes.byref(operation), ctypes.byref(count), False)
                pending = False
                if not ok:
                    raise OSError(ctypes.get_last_error(), "Folder event interrupted.")
                try:
                    rows = self.decode(buffer.raw, count.value) if count.value else []
                except (ValueError, UnicodeError):
                    rows = []
                self.on_events(rows, not bool(rows))
        except Exception as exc:
            if not self._stop.is_set():
                self.on_error(f"{type(exc).__name__}: {exc}")
        finally:
            if handle and pending and operation is not None:
                kernel.CancelIoEx(handle, ctypes.byref(operation))
                count = wintypes.DWORD()
                kernel.GetOverlappedResult(handle, ctypes.byref(operation), ctypes.byref(count), True)
            if handle:
                kernel.CloseHandle(handle)
            if event:
                kernel.CloseHandle(event)
