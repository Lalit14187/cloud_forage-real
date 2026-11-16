"""Native, dependency-free Windows/Linux process and host telemetry."""
import ctypes
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / '.runtime'
_lock = threading.Lock()
_cpu_baseline = None
_proc_baseline = {}


def process_signature(pid):
    if os.name == 'nt':
        class FILETIME(ctypes.Structure):
            _fields_ = [('low', ctypes.c_ulong), ('high', ctypes.c_ulong)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            return (int(created.high) << 32) | int(created.low)
        finally:
            kernel.CloseHandle(handle)
    try:
        data = Path(f'/proc/{int(pid)}/stat').read_text()
        fields = data[data.rfind(')') + 2:].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def process_metrics(pid):
    if os.name == 'nt':
        class FILETIME(ctypes.Structure):
            _fields_ = [('low', ctypes.c_ulong), ('high', ctypes.c_ulong)]
        class COUNTERS(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('faults', ctypes.c_ulong),
                ('peak_ws', ctypes.c_size_t), ('ws', ctypes.c_size_t),
                ('peak_pool', ctypes.c_size_t), ('pool', ctypes.c_size_t),
                ('peak_np', ctypes.c_size_t), ('np', ctypes.c_size_t),
                ('pagefile', ctypes.c_size_t), ('peak_pagefile', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, int(pid))
        if not handle:
            return None
        try:
            created, exited, ktime, utime = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(ktime), ctypes.byref(utime)):
                return None
            counters = COUNTERS(); counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL('psapi', use_last_error=True)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            cpu = (((ktime.high << 32) | ktime.low) + ((utime.high << 32) | utime.low)) / 10_000_000
            return {'cpu_seconds': cpu, 'rss': int(counters.ws)}
        finally:
            kernel.CloseHandle(handle)
    try:
        data = Path(f'/proc/{int(pid)}/stat').read_text()
        fields = data[data.rfind(')') + 2:].split()
        cpu = (int(fields[11]) + int(fields[12])) / (os.sysconf('SC_CLK_TCK') or 100)
        rss = int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
        return {'cpu_seconds': cpu, 'rss': rss}
    except (OSError, ValueError, IndexError):
        return None


def terminate_process(pid):
    """Terminate one exact PID after its CloudForge marker has been validated."""
    if os.name != 'nt':
        os.kill(int(pid), 15)
        return True
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    access = 0x0001 | 0x00100000  # PROCESS_TERMINATE | SYNCHRONIZE
    handle = kernel.OpenProcess(access, False, int(pid))
    if not handle:
        return False
    try:
        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        if not kernel.TerminateProcess(handle, 1): return False
        kernel.WaitForSingleObject(handle, 3000)
        return True
    finally:
        kernel.CloseHandle(handle)


def host_snapshot():
    global _cpu_baseline
    if os.name == 'nt':
        class FILETIME(ctypes.Structure):
            _fields_ = [('low', ctypes.c_ulong), ('high', ctypes.c_ulong)]
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                ('total_phys', ctypes.c_ulonglong), ('avail_phys', ctypes.c_ulonglong),
                ('total_page', ctypes.c_ulonglong), ('avail_page', ctypes.c_ulonglong),
                ('total_virtual', ctypes.c_ulonglong), ('avail_virtual', ctypes.c_ulonglong),
                ('avail_extended', ctypes.c_ulonglong)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        idle, kernel_time, user = FILETIME(), FILETIME(), FILETIME()
        kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel_time), ctypes.byref(user))
        def ft(v): return (int(v.high) << 32) | int(v.low)
        current = (ft(idle), ft(kernel_time), ft(user))
        if _cpu_baseline is None:
            cpu_percent = 0.0
        else:
            old = _cpu_baseline
            total = sum(current[i] - old[i] for i in range(3))
            busy = total - (current[0] - old[0])
            cpu_percent = 100 * busy / total if total else 0.0
        _cpu_baseline = current
        memory = MEMORYSTATUSEX(); memory.length = ctypes.sizeof(memory)
        kernel.GlobalMemoryStatusEx(ctypes.byref(memory))
        total_bytes, available_bytes = int(memory.total_phys), int(memory.avail_phys)
        cpu_count = os.cpu_count() or 1
        mem_percent = 100 * (total_bytes - available_bytes) / total_bytes if total_bytes else 0
    else:
        cpu_percent = 0.0
        try:
            values = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            current = (idle, total)
            if _cpu_baseline is not None:
                dtotal = total - _cpu_baseline[1]
                cpu_percent = 100 * (dtotal - (idle - _cpu_baseline[0])) / dtotal if dtotal else 0
            _cpu_baseline = current
        except (OSError, ValueError, IndexError):
            pass
        meminfo = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1); meminfo[key] = int(value.strip().split()[0]) * 1024
        total_bytes = meminfo.get('MemTotal', 0)
        available_bytes = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
        cpu_count = os.cpu_count() or 1
        mem_percent = 100 * (total_bytes - available_bytes) / total_bytes if total_bytes else 0
    disk = shutil.disk_usage(str(ROOT.anchor or '/'))
    return {'cpu_percent': round(max(0, min(100, cpu_percent)), 1), 'cpu_count': cpu_count,
        'memory_total': total_bytes, 'memory_available': available_bytes,
        'memory_percent': round(max(0, min(100, mem_percent)), 1),
        'disk_total': disk.total, 'disk_free': disk.free}


def process_marker(token):
    return STATE_DIR / f'{token}.json'


def write_worker_marker(token):
    STATE_DIR.mkdir(exist_ok=True)
    process_marker(token).write_text(json.dumps({'pid': os.getpid(), 'signature': process_signature(os.getpid())}), encoding='utf-8')


def remove_worker_marker(token):
    try: process_marker(token).unlink()
    except FileNotFoundError: pass
