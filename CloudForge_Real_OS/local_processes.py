"""Safe local worker process management for CloudForge (no arbitrary commands)."""
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from os_telemetry import host_snapshot, process_marker, process_signature, process_metrics, terminate_process

ROOT = Path(__file__).resolve().parent
_lock = threading.RLock()
_proc_cpu = {}


def process_alive(pid, token):
    try:
        data = __import__('json').loads(process_marker(token).read_text(encoding='utf-8'))
        signature = process_signature(pid)
        return int(data['pid']) == int(pid) and signature is not None and data['signature'] == signature
    except (ValueError, TypeError, OSError, KeyError):
        return False


def process_stats(pid, token):
    if not process_alive(pid, token): return None
    try:
        data = process_metrics(pid)
        if data is None: return None
        now = time.monotonic()
        previous = _proc_cpu.get(int(pid))
        percent = 0.0
        if previous:
            interval = now - previous[1]
            percent = 100 * (data['cpu_seconds'] - previous[0]) / interval if interval > 0 else 0.0
        _proc_cpu[int(pid)] = (data['cpu_seconds'], now)
        return {'cpu_percent': round(max(0.0, percent), 2), 'rss': data['rss'], 'status': 'running'}
    except (OSError, ValueError, TypeError):
        return None


def start_worker(token=None):
    token = token or uuid.uuid4().hex
    parent_pid = os.getpid()
    parent_signature = process_signature(parent_pid)
    command = [sys.executable, str(ROOT / 'worker.py'), '--token', token,
               '--parent-pid', str(parent_pid), '--parent-signature', str(parent_signature)]
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    log_path = process_marker(token).with_suffix('.log')
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open('ab') as log_file:
        proc = subprocess.Popen(command, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                stdout=log_file, stderr=log_file,
                                close_fds=(os.name != 'nt'), creationflags=flags)
    marker = process_marker(token)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not marker.exists(): time.sleep(0.05)
    if not marker.exists():
        proc.terminate()
        raise RuntimeError('Local worker did not start correctly.')
    worker_pid = int(__import__('json').loads(marker.read_text(encoding='utf-8'))['pid'])
    return worker_pid, token, proc.pid


def stop_worker(pid, token, launcher_pid=None):
    if not process_alive(pid, token):
        return False
    try:
        if os.name == 'nt':
            for target in dict.fromkeys((int(launcher_pid or pid), int(pid))):
                try: terminate_process(target)
                except (ProcessLookupError, PermissionError, OSError): pass
        else:
            for target in dict.fromkeys((int(launcher_pid or pid), int(pid))):
                try: terminate_process(target)
                except (ProcessLookupError, PermissionError, OSError): pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and process_alive(pid, token): time.sleep(0.05)
        if process_alive(pid, token): return False
        try: process_marker(token).unlink()
        except FileNotFoundError: pass
        return True
    except (OSError, ValueError):
        return False


def kill_all(rows):
    for row in rows:
        stop_worker(row['pid'], row['token'], row['launcher_pid'])


def ordered_queue(rows, algorithm):
    """Return a stable educational scheduling order for queued process rows."""
    if algorithm == 'SJF':
        return sorted(rows, key=lambda r: (r['burst_time'], r['queue_order']))
    if algorithm == 'Priority':
        return sorted(rows, key=lambda r: (r['priority'], r['queue_order']))
    # FCFS is the default. RR rotates tied/oldest jobs in one-quantum turns;
    # it cannot preempt an already-running OS process.
    if algorithm == 'Round Robin':
        return sorted(rows, key=lambda r: (r['rr_turn'], r['queue_order']))
    return sorted(rows, key=lambda r: r['queue_order'])


def schedule_waiters(db, algorithm, limits, quantum=2):
    """Start queued local processes while reservation limits permit."""
    with _lock:
        rows = db.execute("""SELECT r.*, i.cpu, i.ram, i.name, i.user_id FROM local_runtime r
            JOIN instances i ON i.id=r.instance_id WHERE r.state='queued'""").fetchall()
        live = db.execute("""SELECT r.*, i.cpu, i.ram FROM local_runtime r
            JOIN instances i ON i.id=r.instance_id WHERE r.state='running'""").fetchall()
        used_cpu = sum(row['cpu'] for row in live)
        used_ram = sum(row['ram'] for row in live)
        queue = ordered_queue(rows, algorithm)
        now = time.time()
        for row in queue:
            if used_cpu + row['cpu'] > limits['cpu'] or used_ram + row['ram'] > limits['ram_gb']:
                continue
            pid, token, launcher_pid = start_worker()
            db.execute("UPDATE local_runtime SET state='running', pid=?, token=?, started_at=?, updated_at=? WHERE instance_id=?",
                       (pid, token, now, now, row['instance_id']))
            db.execute('UPDATE local_runtime SET launcher_pid=? WHERE instance_id=?', (launcher_pid, row['instance_id']))
            db.execute("UPDATE os_processes SET process_state='RUNNING', wait_reason='' WHERE id=?",
                       (row['os_process_id'],))
            db.execute("UPDATE local_runtime SET wait_reason='' WHERE instance_id=?", (row['instance_id'],))
            db.execute("UPDATE instances SET status='running' WHERE id=?", (row['instance_id'],))
            db.execute('INSERT INTO os_events(user_id,process_id,event,detail) VALUES(?,?,?,?)',
                       (row['user_id'], row['os_process_id'], 'Resource wait ended', f"{row['name']} dispatched under {algorithm}"))
            db.execute('INSERT INTO activity(user_id,action,detail) VALUES(?,?,?)',
                       (row['user_id'], 'Queued process started', f"{row['name']} • PID {pid} • {algorithm}"))
            used_cpu += row['cpu']
            used_ram += row['ram']
        db.commit()


def refresh_runtime(db):
    rows = db.execute("""SELECT r.*,i.name,i.user_id FROM local_runtime r
        JOIN instances i ON i.id=r.instance_id WHERE r.state='running'""").fetchall()
    for row in rows:
        stats = process_stats(row['pid'], row['token'])
        if stats is None:
            db.execute('INSERT INTO os_events(user_id,process_id,event,detail) VALUES(?,?,?,?)',
                       (row['user_id'], row['os_process_id'], 'Worker exited', f"{row['name']} worker was no longer running"))
            db.execute('INSERT INTO activity(user_id,action,detail) VALUES(?,?,?)',
                       (row['user_id'], 'Worker exited', f"{row['name']} local process ended"))
            db.execute("UPDATE local_runtime SET state='stopped', pid=NULL, launcher_pid=NULL, updated_at=? WHERE instance_id=?",
                       (time.time(), row['instance_id']))
            db.execute("UPDATE instances SET status='stopped' WHERE id=?", (row['instance_id'],))
            db.execute("UPDATE os_processes SET process_state='TERMINATED' WHERE id=?", (row['os_process_id'],))
            db.execute('UPDATE os_mutex SET owner_process_id=NULL WHERE id=1 AND owner_process_id=?', (row['os_process_id'],))
            db.execute("UPDATE os_processes SET process_state='RUNNING',wait_reason='' WHERE process_state='WAITING' AND wait_reason LIKE 'Mutex held by process %'")
            db.execute("UPDATE local_runtime SET wait_reason='' WHERE wait_reason LIKE 'Mutex held by process %'")
            try: process_marker(row['token']).unlink()
            except FileNotFoundError: pass
            _proc_cpu.pop(int(row['pid']), None)
        else:
            db.execute("UPDATE local_runtime SET actual_cpu=?, actual_rss=?, updated_at=? WHERE instance_id=?",
                       (stats['cpu_percent'], stats['rss'], time.time(), row['instance_id']))
    db.commit()


def sync_resource_pool(db, snapshot):
    """Keep DB resource totals and free-unit counters aligned with live reservations."""
    used = db.execute("""SELECT COALESCE(SUM(i.cpu),0) cpu, COALESCE(SUM(i.ram),0) ram
        FROM local_runtime r JOIN instances i ON i.id=r.instance_id WHERE r.state='running'""").fetchone()
    cpu_total = int(snapshot['cpu_count'])
    ram_total = max(1, int(snapshot['memory_total'] // (1024**3)))
    db.execute("UPDATE os_resources SET total=?,available=? WHERE name='CPU'",
               (cpu_total, max(0, cpu_total - used['cpu'])))
    db.execute("UPDATE os_resources SET total=?,available=? WHERE name='RAM'",
               (ram_total, max(0, ram_total - used['ram'])))


def scheduler_metrics(rows, quantum=2):
    """Compute standard waiting/turnaround/response metrics for a CPU policy."""
    metrics = {}
    for policy in ('FCFS', 'SJF', 'Priority', 'Round Robin'):
        ordered = ordered_queue(rows, policy)
        policy_quantum = max(1, int(quantum))
        completion = {}
        if policy == 'Round Robin':
            remaining = {i: max(1, int(row['burst_time'])) for i, row in enumerate(ordered)}
            elapsed = 0
            ready = list(range(len(ordered)))
            while ready:
                index = ready.pop(0)
                slice_time = min(policy_quantum, remaining[index])
                elapsed += slice_time
                remaining[index] -= slice_time
                if remaining[index]:
                    ready.append(index)
                else:
                    completion[index] = elapsed
        else:
            elapsed = 0
            for i, row in enumerate(ordered):
                elapsed += max(1, int(row['burst_time']))
                completion[i] = elapsed
        waits, turns = [], []
        for i, row in enumerate(ordered):
            burst = max(1, int(row['burst_time']))
            turnaround = completion.get(i, burst)
            waits.append(max(0, turnaround - burst))
            turns.append(turnaround)
        metrics[policy] = {
            'average_wait': round(sum(waits) / len(waits), 2) if waits else 0,
            'average_turnaround': round(sum(turns) / len(turns), 2) if turns else 0,
            'quantum_seconds': policy_quantum if policy == 'Round Robin' else None,
            'ready_count': len(ordered),
        }
    return metrics
