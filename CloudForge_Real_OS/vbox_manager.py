"""VirtualBox management bridge for CloudForge.
Allows cloning existing VMs (e.g. Kali, Metasploitable 2) and controlling them in headless mode.
"""
import os
import re
import subprocess
import shutil
from pathlib import Path

# Detect VBoxManage path
DEFAULT_VBOX_PATH = r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
VBOX_MANAGE = shutil.which("VBoxManage") or (DEFAULT_VBOX_PATH if os.path.exists(DEFAULT_VBOX_PATH) else None)


def is_vbox_available():
    return VBOX_MANAGE is not None and os.path.exists(VBOX_MANAGE)


def run_vbox_cmd(args, timeout=120):
    """Run a VBoxManage command and return (returncode, stdout, stderr)."""
    if not is_vbox_available():
        return -1, "", "VBoxManage not found on system."
    try:
        proc = subprocess.run(
            [VBOX_MANAGE] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def list_templates():
    """Return list of existing non-CloudForge VMs that can be cloned as templates."""
    ret, out, _ = run_vbox_cmd(["list", "vms"])
    if ret != 0 or not out:
        return []
    
    templates = []
    for line in out.splitlines():
        # format: "Name" {uuid}
        match = re.match(r'^"([^"]+)"\s+\{([^}]+)\}', line.strip())
        if match:
            vm_name = match.group(1)
            # Exclude VMs created by CloudForge itself to prevent recursive cloning of clones
            if not vm_name.startswith("CF-"):
                templates.append(vm_name)
    return templates


def clone_vm(template_name, new_vm_name, cpus=None, ram_mb=None):
    """Clone an existing template VM and register it."""
    # 1. Clone VM with register
    args = ["clonevm", template_name, "--name", new_vm_name, "--register", "--mode", "all"]
    ret, out, err = run_vbox_cmd(args, timeout=300)
    if ret != 0:
        raise RuntimeError(f"VirtualBox clone failed: {err or out}")

    # 2. Optionally adjust CPU and RAM to match instance specs
    modify_args = ["modifyvm", new_vm_name]
    if cpus:
        modify_args.extend(["--cpus", str(cpus)])
    if ram_mb:
        modify_args.extend(["--memory", str(ram_mb)])
    
    if len(modify_args) > 2:
        run_vbox_cmd(modify_args)

    return get_vm_info(new_vm_name)


def start_vm_headless(vm_name):
    """Start VM in headless background mode."""
    ret, out, err = run_vbox_cmd(["startvm", vm_name, "--type", "headless"])
    if ret != 0:
        if "already locked by a session" in err.lower() or "is already running" in err.lower():
            return True
        raise RuntimeError(f"Failed to start VM '{vm_name}' headless: {err or out}")
    return True


def stop_vm(vm_name, force=False):
    """Stop the VM cleanly via acpipowerbutton or force poweroff."""
    action = "poweroff" if force else "acpipowerbutton"
    ret, out, err = run_vbox_cmd(["controlvm", vm_name, action])
    if ret != 0 and not force:
        # Fallback to poweroff if acpi button fails or isn't supported
        ret, out, err = run_vbox_cmd(["controlvm", vm_name, "poweroff"])
    return ret == 0


def delete_vm(vm_name):
    """Power off and completely delete/unregister the cloned VM and its files."""
    # First make sure it's powered off
    stop_vm(vm_name, force=True)
    ret, out, err = run_vbox_cmd(["unregistervm", vm_name, "--delete"])
    return ret == 0


def get_vm_state(vm_name):
    """Return the current VMState (e.g. running, poweroff, aborted, etc.)."""
    ret, out, _ = run_vbox_cmd(["showvminfo", vm_name, "--machinereadable"])
    if ret != 0:
        return "not_found"
    match = re.search(r'^VMState="([^"]+)"', out, re.MULTILINE)
    return match.group(1).lower() if match else "unknown"


def get_vm_info(vm_name):
    """Get VM information dictionary."""
    ret, out, _ = run_vbox_cmd(["showvminfo", vm_name, "--machinereadable"])
    if ret != 0:
        return None
    
    info = {"name": vm_name}
    for match in re.finditer(r'^([^=\n]+)="?([^"\n]*)"?', out, re.MULTILINE):
        key = match.group(1).strip()
        val = match.group(2).strip()
        info[key] = val
    return info
