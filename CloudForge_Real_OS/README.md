# CloudForge

CloudForge is a local operating-systems learning lab built with Flask and SQLite. It launches genuine, low-impact Python worker processes on the computer running the app, records their OS PIDs, measures host and per-worker CPU/RAM use, controls their lifecycle, and queues work when configured host capacity is reserved. It does **not** create virtual machines, containers, cloud resources, or OS-level CPU/memory quotas. The selected operating-system name is descriptive metadata only.

## Windows setup (Hindi-English)

1. Python 3.10+ install karein: [python.org](https://www.python.org/downloads/). Setup mein **Add Python to PATH** tick karein.
2. ZIP ko ek normal writable folder mein extract karein, phir `run.bat` par double-click karein.
3. First run par `.venv` banega aur Flask install hoga (sirf pehli baar internet chahiye). Uske baad local app offline chal sakti hai.
4. Server ready hote hi browser khud `http://127.0.0.1:5000` kholega. Agar tab load hone se pehle khul jaye to thodi der baad Reload dabayein.
5. Register karke local process lab use karein. Window khuli rakhein; band karne ke liye console mein Ctrl+C dabayein. Existing uploaded database ke account passwords available hon to wahi accounts kaam karenge; warna naya account register karein.

Server sirf `127.0.0.1` par chalta hai, isliye aapke network par expose nahi hota. App chalane wali console window khuli rehni chahiye; usse band karne par local website bhi band ho jaati hai.

## Features

- Register/sign in; passwords Werkzeug PBKDF2 hash se store hote hain.
- Har write form par session-bound CSRF token; HTTP-only, SameSite=Lax session cookie.
- OS label, CPU/RAM reservation and disk metadata; start/stop/restart/delete controls backed by actual local child-process PIDs. Worker code is fixed and the app accepts no shell command input.
- Live host CPU, physical RAM, disk capacity and managed-process RSS/CPU metrics, refreshed every three seconds.
- Resource-aware READY/WAITING/RUNNING/TERMINATED process records, OS process IDs, per-process CPU/RAM reservation rows, visible resource queueing, and selectable FCFS/SJF/Priority/Round Robin scheduling comparisons. Round Robin quantum is editable from 1–60 seconds.
- A shared database mutex demonstrates mutual exclusion; a competing local process enters WAITING until the lock is released. Worker processes automatically exit if CloudForge itself closes unexpectedly.
- Volumes create/attach/detach/delete; instance delete hone par attached volume detach hota hai.
- DBMS concepts: SQLite transactions for linked resource updates; foreign keys, unique/check constraints, indexes, joins, schema migration records, and persistent audit history for sign-in, configuration, process lifecycle, queue dispatch, mutex and other user actions in activity and OS event tables.
- Offline dark cloud/network atmosphere with a real-time WebGL cloud globe, drifting cloud layers, stars, and subtle pointer parallax; first visit par animated frontend → Flask API → SQLite intro.
- Scroll progress/reveal motion, accessible focus states, reduced-motion support, and button/link click ripples.
- All frontend effects are local; there are no external image, font, or script dependencies.

## Admin demo role

Account bana kar app band karein. Extracted folder mein Command Prompt kholkar ye command chalayein (username replace karein):

```bat
.venv\Scripts\python.exe promote_admin.py YOUR_USERNAME
```

App restart karke dobara sign in karein. Is classroom demo ko untrusted/public users ke liye deploy na karein.

## Manual start

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python app.py
```

Database `cloudforge.db` first start par banta hai. Optional `CLOUDFORGE_DB` se path aur `CLOUDFORGE_SECRET` se session key set kar sakte hain. Default random key ke saath restart par users ko dobara sign in karna hota hai.

Per process limits remain 16 vCPU, 64 GB RAM and 500 GB disk metadata. Host reservation capacity uses the current logical core count and physical RAM, and unfit starts wait in the resource queue. These reservation values are accounting limits only: worker processes use the host OS scheduler, and the selected vCPU/RAM values do not impose actual per-process quotas. Disk allocations, volumes and instance OS labels are database concepts; they do not create disk images, partitions, or guest operating systems. The burst length is a teaching input for scheduler ordering and average-wait/turnaround calculations, not the worker's lifetime or an enforced CPU run. The user-selected Round Robin quantum (1–60 seconds) affects the textbook scheduling comparison; it does not preempt already-running child processes. SQLite is intended for this single-computer classroom app, not concurrent multi-host production deployment.

CSRF aur password hashing local demonstration ke liye hain. Public deployment se pehle HTTPS, stable secret key, login rate limiting aur production hosting add karein.
