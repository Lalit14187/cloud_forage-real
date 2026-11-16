"""A low-impact, genuine child OS process supervised by CloudForge."""
import argparse
import time
from os_telemetry import write_worker_marker, remove_worker_marker

parser = argparse.ArgumentParser()
parser.add_argument('--token', required=True)
parser.add_argument('--parent-pid', required=True, type=int)
parser.add_argument('--parent-signature', required=True, type=int)
args = parser.parse_args()

# The process stays alive to be observed and controlled. A brief bounded CPU
# burst demonstrates real CPU time without running a permanent busy loop.
write_worker_marker(args.token)
try:
    while True:
        end = time.perf_counter() + 0.015
        value = 0
        while time.perf_counter() < end:
            value = (value * 33 + 17) % 10000019
        time.sleep(0.985)
        # If CloudForge exits unexpectedly, do not leave an unmanaged child behind.
        from os_telemetry import process_signature
        if process_signature(args.parent_pid) != args.parent_signature:
            break
finally:
    remove_worker_marker(args.token)
