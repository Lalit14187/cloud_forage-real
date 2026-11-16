# Import tools for the operating system, security, SQLite, Flask, and paths.
import os, secrets, sqlite3, threading, webbrowser
import time
from pathlib import Path
from functools import wraps
from flask import Flask, g, session, request, redirect, url_for, render_template, flash, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from local_processes import host_snapshot, start_worker, stop_worker, schedule_waiters, refresh_runtime, scheduler_metrics, sync_resource_pool


# ROOT is the folder containing this app.py file.
# DB is the SQLite database file where project data is saved.
ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('CLOUDFORGE_DB', ROOT / 'cloudforge.db'))

# These are the operating systems available in the instance creation form.
OS_OPTIONS = ['Ubuntu 24.04 LTS', 'Windows Server 2022', 'Debian 12', 'Amazon Linux 2023']
SCHEDULERS = ['FCFS', 'SJF', 'Priority', 'Round Robin']

# Create the Flask application.
app = Flask(__name__)

# Configure the secret used to protect login sessions and set cookie options.
app.config.update(
    SECRET_KEY=os.environ.get('CLOUDFORGE_SECRET') or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)


# DATABASE CONNECTION
# Reuse one SQLite connection during each web request.
def conn():
    if 'db' not in g:
        g.db = sqlite3.connect(DB, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db


# Close the database connection when a request is finished.
@app.teardown_appcontext
def close_db(_err):
    c = g.pop('db', None)
    if c:
        c.close()


# DATABASE TABLES
# Create the tables and indexes the first time the app runs.
# Existing tables and saved data are kept.
def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as c:
        c.execute('PRAGMA foreign_keys=ON')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user'
                CHECK(role IN ('user','admin')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS instances(
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            os TEXT NOT NULL,
            cpu INTEGER NOT NULL,
            ram INTEGER NOT NULL,
            storage INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'stopped'
                CHECK(status IN ('running','stopped')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS volumes(
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            size INTEGER NOT NULL,
            instance_id INTEGER REFERENCES instances(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS activity(
            id INTEGER PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS os_processes(
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            burst_time INTEGER NOT NULL CHECK(burst_time > 0),
            priority INTEGER NOT NULL DEFAULT 1 CHECK(priority BETWEEN 1 AND 10),
            remaining_time INTEGER NOT NULL DEFAULT 0 CHECK(remaining_time >= 0),
            process_state TEXT NOT NULL DEFAULT 'READY',
            waiting_time INTEGER NOT NULL DEFAULT 0,
            turnaround_time INTEGER NOT NULL DEFAULT 0,
            wait_reason TEXT NOT NULL DEFAULT '',
            max_printer INTEGER NOT NULL DEFAULT 1,
            max_database INTEGER NOT NULL DEFAULT 1,
            max_disk INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS os_resources(
            name TEXT PRIMARY KEY,
            total INTEGER NOT NULL CHECK(total >= 0),
            available INTEGER NOT NULL CHECK(available >= 0)
        );
        CREATE TABLE IF NOT EXISTS os_allocations(
            process_id INTEGER NOT NULL REFERENCES os_processes(id) ON DELETE CASCADE,
            resource_name TEXT NOT NULL REFERENCES os_resources(name),
            units INTEGER NOT NULL DEFAULT 0 CHECK(units >= 0),
            PRIMARY KEY(process_id, resource_name)
        );
        CREATE TABLE IF NOT EXISTS os_mutex(
            id INTEGER PRIMARY KEY CHECK(id=1),
            owner_process_id INTEGER REFERENCES os_processes(id) ON DELETE SET NULL
        );
        INSERT OR IGNORE INTO os_mutex(id,owner_process_id) VALUES(1,NULL);
        CREATE TABLE IF NOT EXISTS os_events(
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            process_id INTEGER,
            event TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_os_process_user_state ON os_processes(user_id,process_state);
        CREATE INDEX IF NOT EXISTS idx_os_event_user ON os_events(user_id,id DESC);

        CREATE INDEX IF NOT EXISTS idx_inst_user ON instances(user_id);
        CREATE INDEX IF NOT EXISTS idx_vol_user ON volumes(user_id);
        CREATE INDEX IF NOT EXISTS idx_act_user ON activity(user_id,id DESC);

        CREATE TABLE IF NOT EXISTS local_runtime(
            instance_id INTEGER PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
            os_process_id INTEGER REFERENCES os_processes(id) ON DELETE SET NULL,
            state TEXT NOT NULL CHECK(state IN ('queued','running','stopped','failed')),
            pid INTEGER,
            launcher_pid INTEGER,
            token TEXT,
            scheduler TEXT NOT NULL DEFAULT 'FCFS'
                CHECK(scheduler IN ('FCFS','SJF','Priority','Round Robin')),
            rr_quantum INTEGER NOT NULL DEFAULT 2 CHECK(rr_quantum BETWEEN 1 AND 60),
            priority INTEGER NOT NULL DEFAULT 5 CHECK(priority BETWEEN 1 AND 10),
            burst_time INTEGER NOT NULL DEFAULT 5 CHECK(burst_time BETWEEN 1 AND 300),
            queue_order INTEGER NOT NULL,
            rr_turn INTEGER NOT NULL DEFAULT 0,
            wait_reason TEXT NOT NULL DEFAULT '',
            actual_cpu REAL NOT NULL DEFAULT 0,
            actual_rss INTEGER NOT NULL DEFAULT 0,
            started_at REAL,
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_runtime_state_order ON local_runtime(state,queue_order);
        CREATE TABLE IF NOT EXISTS app_settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO app_settings(key,value) VALUES('scheduler','FCFS');
        INSERT OR IGNORE INTO app_settings(key,value) VALUES('rr_quantum','2');
        CREATE TABLE IF NOT EXISTS schema_migrations(
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO schema_migrations(version) VALUES(3);
        ''')
        runtime_columns = {row[1] for row in c.execute('PRAGMA table_info(local_runtime)')}
        if 'launcher_pid' not in runtime_columns:
            c.execute('ALTER TABLE local_runtime ADD COLUMN launcher_pid INTEGER')
        if 'rr_quantum' not in runtime_columns:
            c.execute('ALTER TABLE local_runtime ADD COLUMN rr_quantum INTEGER NOT NULL DEFAULT 2 CHECK(rr_quantum BETWEEN 1 AND 60)')
        # Retain the existing OS resource rows and add live host capacity.
        snapshot = host_snapshot()
        c.execute("INSERT OR IGNORE INTO os_resources(name,total,available) VALUES('CPU',?,?)",
                  (snapshot['cpu_count'], snapshot['cpu_count']))
        ram_gb = max(1, snapshot['memory_total'] // (1024**3))
        c.execute("INSERT OR IGNORE INTO os_resources(name,total,available) VALUES('RAM',?,?)",
                  (ram_gb, ram_gb))


# ACTIVITY LOG
# Save a record of a user's action in the activity table.
def log(uid, action, detail):
    conn().execute(
        'INSERT INTO activity(user_id,action,detail) VALUES(?,?,?)',
        (uid, action, detail)
    )


# Get the currently signed-in user's basic information.
def who():
    return conn().execute(
        'SELECT id,username,role FROM users WHERE id=?',
        (session['user_id'],)
    ).fetchone() if session.get('user_id') else None


# Make these common values available to all HTML templates.
@app.context_processor
def globals_():
    session.setdefault('csrf', secrets.token_urlsafe(32))
    return {
        'csrf_token': session['csrf'],
        'current_user': who(),
        'os_options': OS_OPTIONS,
        'scheduler_options': SCHEDULERS
    }


# CSRF SECURITY
# Check the security token whenever a form sends a POST request.
@app.before_request
def csrf():
    if request.method == 'POST' and (
        not session.get('csrf')
        or not secrets.compare_digest(
            request.form.get('csrf_token', ''),
            session['csrf']
        )
    ):
        abort(400, 'Invalid or expired form token. Refresh and try again.')


# LOGIN REQUIRED
# This decorator redirects visitors to the login page if they are not signed in.
def required(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if not who():
            return redirect(url_for('login'))
        return fn(*a, **kw)
    return inner


# Check whether a database record belongs to the current user or an admin.
def owned(row):
    u = who()
    return row and (row['user_id'] == u['id'] or u['role'] == 'admin')


# PAGE ROUTES
# Show the landing page, or the dashboard if the user is already signed in.
@app.get('/')
def home():
    return redirect(url_for('dashboard')) if who() else render_template('landing.html')


# REGISTER
# Validate the form, hash the password, save the account, and sign the user in.
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('username', '').strip()
        pw = request.form.get('password', '')

        if not (3 <= len(name) <= 32 and name.replace('_', '').isalnum()):
            flash('Username must be 3–32 letters, numbers or underscores.', 'error')
        elif len(pw) < 8:
            flash('Use a password with at least 8 characters.', 'error')
        else:
            try:
                with conn():
                    cur = conn().execute(
                        'INSERT INTO users(username,password_hash) VALUES(?,?)',
                        (name, generate_password_hash(pw))
                    )
                    log(cur.lastrowid, 'Account created', f'Welcome, {name}')

                uid = conn().execute(
                    'SELECT id FROM users WHERE username=?', (name,)
                ).fetchone()['id']

                session.clear()
                session['user_id'] = uid
                session['csrf'] = secrets.token_urlsafe(32)
                return redirect(url_for('dashboard'))

            except sqlite3.IntegrityError:
                flash('That username is already taken.', 'error')

    return render_template('auth.html', mode='register')


# LOGIN
# Check the entered password against the stored password hash.
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = conn().execute(
            'SELECT * FROM users WHERE username=?',
            (request.form.get('username', '').strip(),)
        ).fetchone()

        if u and check_password_hash(
            u['password_hash'],
            request.form.get('password', '')
        ):
            with conn():
                log(u['id'], 'Login', 'Signed in successfully')
            session.clear()
            session['user_id'] = u['id']
            session['csrf'] = secrets.token_urlsafe(32)
            return redirect(url_for('dashboard'))

        with conn():
            log(None, 'Login failed', f"Username attempt: {request.form.get('username', '').strip()[:32]}")
        flash('Username or password is incorrect.', 'error')

    return render_template('auth.html', mode='login')


# LOGOUT
# Clear the user's session to sign them out.
@app.post('/logout')
@required
def logout():
    with conn():
        log(who()['id'], 'Logout', 'Signed out')
    session.clear()
    return redirect(url_for('home'))


# DASHBOARD
# Load the user's instances, volumes, activity, and current resource usage.
@app.get('/dashboard')
@required
def dashboard():
    u = who()
    admin = u['role'] == 'admin'

    if admin:
        instances = conn().execute(
            'SELECT i.*,u.username FROM instances i '
            'JOIN users u ON u.id=i.user_id ORDER BY i.id DESC'
        ).fetchall()

        volumes = conn().execute(
            'SELECT v.*,i.name instance_name FROM volumes v '
            'LEFT JOIN instances i ON i.id=v.instance_id ORDER BY v.id DESC'
        ).fetchall()

        logs = conn().execute(
            'SELECT a.*,u.username FROM activity a '
            'LEFT JOIN users u ON u.id=a.user_id '
            'ORDER BY a.id DESC LIMIT 12'
        ).fetchall()

        usage = conn().execute(
            "SELECT COALESCE(SUM(cpu),0) cpu,"
            "COALESCE(SUM(ram),0) ram,"
            "COALESCE(SUM(storage),0) storage "
            "FROM instances i WHERE EXISTS (SELECT 1 FROM local_runtime r WHERE r.instance_id=i.id AND r.state='running')"
        ).fetchone()

    else:
        instances = conn().execute(
            'SELECT * FROM instances WHERE user_id=? ORDER BY id DESC',
            (u['id'],)
        ).fetchall()

        volumes = conn().execute(
            'SELECT v.*,i.name instance_name FROM volumes v '
            'LEFT JOIN instances i ON i.id=v.instance_id '
            'WHERE v.user_id=? ORDER BY v.id DESC',
            (u['id'],)
        ).fetchall()

        logs = conn().execute(
            'SELECT * FROM activity WHERE user_id=? ORDER BY id DESC LIMIT 12',
            (u['id'],)
        ).fetchall()

        usage = conn().execute(
            "SELECT COALESCE(SUM(cpu),0) cpu,"
            "COALESCE(SUM(ram),0) ram,"
            "COALESCE(SUM(storage),0) storage "
            "FROM instances i WHERE user_id=? AND EXISTS (SELECT 1 FROM local_runtime r WHERE r.instance_id=i.id AND r.state='running')",
            (u['id'],)
        ).fetchone()

    # Maximum practice capacity for this CloudForge lab.
    limits = {
        'CPU': (usage['cpu'], 32, 'vCPU'),
        'RAM': (usage['ram'], 128, 'GB'),
        'Storage': (usage['storage'], 2048, 'GB')
    }

    with conn():
        refresh_runtime(conn())
        policy = conn().execute("SELECT value FROM app_settings WHERE key='scheduler'").fetchone()['value']
        quantum = int(conn().execute("SELECT value FROM app_settings WHERE key='rr_quantum'").fetchone()['value'])
        snapshot = host_snapshot()
        schedule_waiters(conn(), policy, {'cpu':snapshot['cpu_count'], 'ram_gb':max(1,snapshot['memory_total']//(1024**3))}, quantum)
        sync_resource_pool(conn(), snapshot)
        runtime_rows = conn().execute('''SELECT r.*,i.name,i.cpu,i.ram,i.storage,o.process_state
            FROM local_runtime r JOIN instances i ON i.id=r.instance_id
            LEFT JOIN os_processes o ON o.id=r.os_process_id
            WHERE (?=1 OR i.user_id=?) ORDER BY r.queue_order''',
            (int(admin), u['id'])).fetchall()
        ready = [dict(row) for row in runtime_rows if row['state'] == 'queued']
        comparison_jobs = [dict(row) for row in runtime_rows if row['state'] in ('queued', 'running')]
        sched_stats = scheduler_metrics(comparison_jobs, quantum)
        mutex_owner = conn().execute('SELECT owner_process_id FROM os_mutex WHERE id=1').fetchone()['owner_process_id']
        runtime_by_id = {row['instance_id']: row for row in runtime_rows}
        for item in instances:
            runtime = runtime_by_id.get(item['id'])
            item = dict(item)
            item['display_status'] = runtime['state'] if runtime else 'stopped'
            item['pid'] = runtime['pid'] if runtime else None
            item['actual_rss'] = runtime['actual_rss'] if runtime else 0
            item['wait_reason'] = runtime['wait_reason'] if runtime else 'No local process is currently managed'
            item['process_state'] = runtime['process_state'] if runtime else 'LEGACY'
            item['os_process_id'] = runtime['os_process_id'] if runtime else None
            item['_runtime'] = True
            instances = [item if x['id'] == item['id'] else x for x in instances]
        runtime_alloc = conn().execute("SELECT COALESCE(SUM(i.cpu),0) cpu, COALESCE(SUM(i.ram),0) ram FROM local_runtime r JOIN instances i ON i.id=r.instance_id WHERE r.state='running'").fetchone()

    return render_template(
        'dashboard.html',
        instances=instances,
        volumes=volumes,
        logs=logs,
        limits=limits,
        host={**snapshot, 'ram_total_gb': round(snapshot['memory_total'] / 1024**3, 1),
              'ram_available_gb': round(snapshot['memory_available'] / 1024**3, 1),
              'allocated_cpu': runtime_alloc['cpu'], 'allocated_ram': runtime_alloc['ram']},
        scheduler=policy,
        scheduler_quantum=quantum,
        scheduler_metrics=sched_stats,
        resource_waits=ready,
        queued_count=len(ready),
        mutex_owner=mutex_owner
    )


# CREATE INSTANCE
# Validate the form, reserve host capacity, and start a real local worker process.
@app.post('/instances/create')
@required
def create_instance():
    u = who()
    name = request.form.get('name', '').strip()
    osname = request.form.get('os', '')
    scheduler = request.form.get('scheduler', 'FCFS')
    rr_quantum = request.form.get('rr_quantum', '2')
    priority = request.form.get('priority', '5')
    burst = request.form.get('burst_time', '5')


    try:
        cpu, ram, disk = (
            int(request.form[k]) for k in ('cpu', 'ram', 'storage')
        )
        priority = int(priority)
        burst = int(burst)
        rr_quantum = int(rr_quantum)
    except (ValueError, KeyError):
        flash('Choose valid capacity values.', 'error')
        return redirect(url_for('dashboard'))


    if (
        not name
        or len(name) > 40
        or osname not in OS_OPTIONS
        or scheduler not in SCHEDULERS
        or not 1 <= rr_quantum <= 60
        or not 1 <= priority <= 10
        or not 1 <= burst <= 300
        or not (1 <= cpu <= 16 and 1 <= ram <= 64 and 10 <= disk <= 500)
    ):
        flash('Check the instance name and selected options.', 'error')
        return redirect(url_for('dashboard'))

    try:
        # This transaction keeps DBMS reservations and queue state consistent.
        with conn():
            conn().execute('BEGIN IMMEDIATE')
            old_scheduler = conn().execute("SELECT value FROM app_settings WHERE key='scheduler'").fetchone()['value']
            old_quantum = int(conn().execute("SELECT value FROM app_settings WHERE key='rr_quantum'").fetchone()['value'])
            current = conn().execute(
                "SELECT COALESCE(SUM(cpu),0) cpu,"
                "COALESCE(SUM(ram),0) ram,"
                "COALESCE(SUM(storage),0) storage "
                "FROM instances i WHERE user_id=? AND EXISTS (SELECT 1 FROM local_runtime r WHERE r.instance_id=i.id AND r.state='running')",
                (u['id'],)
            ).fetchone()

            if current['cpu'] + cpu > 32 or current['ram'] + ram > 128 or current['storage'] + disk > 2048:
                flash(
                    'This instance would exceed your lab capacity '
                    '(32 vCPU, 128 GB RAM, 2048 GB disk).',
                    'error'
                )
                return redirect(url_for('dashboard'))

            host = host_snapshot()
            live = conn().execute("""SELECT COALESCE(SUM(i.cpu),0) cpu, COALESCE(SUM(i.ram),0) ram
                FROM local_runtime r JOIN instances i ON i.id=r.instance_id WHERE r.state='running'""").fetchone()
            can_start = live['cpu'] + cpu <= host['cpu_count'] and live['ram'] + ram <= max(1, host['memory_total'] // (1024**3))
            cur = conn().execute(
                "INSERT INTO instances(user_id,name,os,cpu,ram,storage,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (u['id'], name, osname, cpu, ram, disk, 'running' if can_start else 'stopped')
            )
            instance_id = cur.lastrowid
            proc = conn().execute('''INSERT INTO os_processes
                (user_id,name,burst_time,priority,remaining_time,process_state,wait_reason)
                VALUES(?,?,?,?,?,?,?)''',
                (u['id'], name, burst, priority, burst, 'READY' if can_start else 'WAITING', '' if can_start else 'Host CPU or memory reservation is full'))
            process_id = proc.lastrowid
            queue_order = conn().execute('SELECT COALESCE(MAX(queue_order),0)+1 FROM local_runtime').fetchone()[0]
            state = 'running' if can_start else 'queued'
            pid = token = launcher_pid = None
            if can_start:
                pid, token, launcher_pid = start_worker()
            conn().execute('''INSERT INTO local_runtime
                (instance_id,os_process_id,state,pid,launcher_pid,token,scheduler,rr_quantum,priority,burst_time,queue_order,wait_reason,started_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (instance_id, process_id, state, pid, launcher_pid, token, scheduler, rr_quantum, priority, burst,
                 queue_order, '' if can_start else 'Waiting for host CPU or RAM capacity', time.time() if can_start else None, time.time()))
            conn().execute('INSERT INTO os_allocations(process_id,resource_name,units) VALUES(?,?,?)', (process_id, 'CPU', cpu))
            conn().execute('INSERT INTO os_allocations(process_id,resource_name,units) VALUES(?,?,?)', (process_id, 'RAM', ram))
            conn().execute('INSERT INTO os_events(user_id,process_id,event,detail) VALUES(?,?,?,?)',
                           (u['id'], process_id, 'Process launched' if can_start else 'Resource wait', f'{name} • {scheduler}'))
            sync_resource_pool(conn(), host)
            conn().execute("UPDATE app_settings SET value=?,updated_at=CURRENT_TIMESTAMP WHERE key='scheduler'", (scheduler,))
            conn().execute("UPDATE app_settings SET value=?,updated_at=CURRENT_TIMESTAMP WHERE key='rr_quantum'", (str(rr_quantum),))
            log(
                u['id'],
                'Instance created',
                f'{name} • {cpu} vCPU • {ram} GB RAM • {disk} GB logical storage • {scheduler} • burst {burst}s • RR quantum {rr_quantum}s'
            )
            if (old_scheduler, old_quantum) != (scheduler, rr_quantum):
                log(u['id'], 'Scheduler configured', f'{scheduler} • Round Robin quantum {rr_quantum}s')
            if not can_start:
                log(u['id'], 'Resource wait', f'{name} queued • needs {cpu} vCPU and {ram} GB RAM reservation')

    except sqlite3.IntegrityError:
        flash('Could not create instance. Please retry.', 'error')
        return redirect(url_for('dashboard'))

    flash(f'{name} is running as local OS process PID {pid}.' if can_start else f'{name} is queued until host resources are available.', 'success')
    return redirect(url_for('dashboard'))


# INSTANCE ACTIONS
# Start, stop, restart, or delete an instance.
@app.post('/instances/<int:item_id>/<action>')
@required
def instance_action(item_id, action):
    row = conn().execute(
        'SELECT * FROM instances WHERE id=?', (item_id,)
    ).fetchone()

    if not owned(row):
        abort(404)

    with conn():
        runtime = conn().execute('SELECT * FROM local_runtime WHERE instance_id=?', (item_id,)).fetchone()
        if action == 'delete':
            if runtime and runtime['state'] == 'running':
                if not stop_worker(runtime['pid'], runtime['token'], runtime['launcher_pid']):
                    flash('The local process could not be stopped, so this record was kept.', 'error')
                    return redirect(url_for('dashboard'))
            if runtime and runtime['os_process_id']:
                conn().execute('UPDATE os_mutex SET owner_process_id=NULL WHERE id=1 AND owner_process_id=?', (runtime['os_process_id'],))
                conn().execute("UPDATE os_processes SET process_state='RUNNING',wait_reason='' WHERE process_state='WAITING' AND wait_reason LIKE 'Mutex held by process %'")
                conn().execute("UPDATE local_runtime SET wait_reason='' WHERE wait_reason LIKE 'Mutex held by process %'")
                conn().execute('INSERT INTO os_events(user_id,process_id,event,detail) VALUES(?,?,?,?)', (who()['id'], runtime['os_process_id'], 'Process deleted', row['name']))
                conn().execute('DELETE FROM os_processes WHERE id=?', (runtime['os_process_id'],))
            conn().execute(
                'DELETE FROM instances WHERE id=?', (item_id,)
            )
            log(who()['id'], 'Instance deleted', row['name'])

        elif action in ('start', 'stop', 'restart'):
            process_id = runtime['os_process_id'] if runtime else None
            if runtime:
                if action == 'restart' and runtime['state'] == 'running':
                    if not stop_worker(runtime['pid'], runtime['token'], runtime['launcher_pid']):
                        flash('The local process could not be restarted safely.', 'error')
                        return redirect(url_for('dashboard'))
                    conn().execute('UPDATE os_mutex SET owner_process_id=NULL WHERE id=1 AND owner_process_id=?', (runtime['os_process_id'],))
                    conn().execute("UPDATE os_processes SET process_state='RUNNING',wait_reason='' WHERE process_state='WAITING' AND wait_reason LIKE 'Mutex held by process %'")
                    conn().execute("UPDATE local_runtime SET wait_reason='' WHERE wait_reason LIKE 'Mutex held by process %'")
                if action == 'stop' and runtime['state'] == 'running':
                    if not stop_worker(runtime['pid'], runtime['token'], runtime['launcher_pid']):
                        flash('The local process could not be stopped safely.', 'error')
                        return redirect(url_for('dashboard'))
                    conn().execute('UPDATE os_mutex SET owner_process_id=NULL WHERE id=1 AND owner_process_id=?', (runtime['os_process_id'],))
                    conn().execute("UPDATE os_processes SET process_state='RUNNING',wait_reason='' WHERE process_state='WAITING' AND wait_reason LIKE 'Mutex held by process %' AND id IN (SELECT os_process_id FROM local_runtime WHERE state='running')")
                    conn().execute("UPDATE local_runtime SET state='stopped',pid=NULL,launcher_pid=NULL,token=NULL,updated_at=? WHERE instance_id=?", (time.time(), item_id))
                    conn().execute("UPDATE os_processes SET process_state='TERMINATED' WHERE id=?", (runtime['os_process_id'],))
                    conn().execute("UPDATE instances SET status='stopped' WHERE id=?", (item_id,))
                else:
                    # Restarted or started processes are queued then dispatched under host limits.
                    conn().execute("UPDATE local_runtime SET state='queued',pid=NULL,launcher_pid=NULL,token=NULL,rr_turn=rr_turn+1,wait_reason='Waiting for host CPU or RAM capacity' WHERE instance_id=?", (item_id,))
                    conn().execute("UPDATE os_processes SET process_state='READY' WHERE id=?", (runtime['os_process_id'],))
                    conn().execute("UPDATE instances SET status='stopped' WHERE id=?", (item_id,))
                    host = host_snapshot()
                    policy = conn().execute("SELECT value FROM app_settings WHERE key='scheduler'").fetchone()['value']
                    quantum = int(conn().execute("SELECT value FROM app_settings WHERE key='rr_quantum'").fetchone()['value'])
                    schedule_waiters(conn(), policy, {'cpu':host['cpu_count'], 'ram_gb':max(1,host['memory_total']//(1024**3))}, quantum)
                    sync_resource_pool(conn(), host)
                    live_row = conn().execute('SELECT state FROM local_runtime WHERE instance_id=?',(item_id,)).fetchone()
                    if live_row['state'] == 'running':
                        conn().execute("UPDATE instances SET status='running' WHERE id=?",(item_id,))
            else:
                # Legacy rows created by older CloudForge versions acquire a real worker on start.
                if action == 'stop':
                    conn().execute("UPDATE instances SET status='stopped' WHERE id=?", (item_id,))
                else:
                    host = host_snapshot()
                    proc = conn().execute("INSERT INTO os_processes(user_id,name,burst_time,priority,remaining_time,process_state) VALUES(?,?,5,5,5,'READY')", (row['user_id'],row['name']))
                    process_id = proc.lastrowid
                    pid, token, launcher_pid = start_worker()
                    queue_order = conn().execute('SELECT COALESCE(MAX(queue_order),0)+1 FROM local_runtime').fetchone()[0]
                    conn().execute("INSERT INTO local_runtime(instance_id,os_process_id,state,pid,launcher_pid,token,queue_order,started_at,updated_at) VALUES(?,?,'running',?,?,?,?, ?,?)",(item_id,process_id,pid,launcher_pid,token,queue_order,time.time(),time.time()))
                    conn().execute("UPDATE instances SET status='running' WHERE id=?", (item_id,))
            conn().execute('INSERT INTO os_events(user_id,process_id,event,detail) VALUES(?,?,?,?)',
                           (who()['id'], process_id, 'Process ' + action, row['name']))
            log(
                who()['id'],
                'Instance ' + (
                    'restarted' if action == 'restart' else action + 'ed'
                ),
                row['name']
            )
            if action == 'stop':
                policy = conn().execute("SELECT value FROM app_settings WHERE key='scheduler'").fetchone()['value']
                quantum = int(conn().execute("SELECT value FROM app_settings WHERE key='rr_quantum'").fetchone()['value'])
                host = host_snapshot()
                schedule_waiters(conn(), policy, {'cpu':host['cpu_count'], 'ram_gb':max(1,host['memory_total']//(1024**3))}, quantum)
                sync_resource_pool(conn(), host)
                conn().execute("UPDATE instances SET status='running' WHERE id IN (SELECT instance_id FROM local_runtime WHERE state='running')")
        else:
            abort(404)

    flash('Instance updated.', 'success')
    return redirect(url_for('dashboard'))


@app.get('/api/system-metrics')
@required
def system_metrics():
    u = who()
    snapshot = host_snapshot()
    with conn():
        refresh_runtime(conn())
        sync_resource_pool(conn(), snapshot)
    admin = u['role'] == 'admin'
    row = conn().execute('''SELECT COUNT(*) count, COALESCE(SUM(actual_rss),0) rss,
        COALESCE(SUM(actual_cpu),0) cpu FROM local_runtime r JOIN instances i ON i.id=r.instance_id
        WHERE r.state='running' AND (?=1 OR i.user_id=?)''',(int(admin),u['id'])).fetchone()
    queued = conn().execute('''SELECT COUNT(*) FROM local_runtime r JOIN instances i ON i.id=r.instance_id
        WHERE r.state='queued' AND (?=1 OR i.user_id=?)''',(int(admin),u['id'])).fetchone()[0]
    return jsonify(cpu_percent=snapshot['cpu_percent'], memory_total=snapshot['memory_total'],
        memory_available=snapshot['memory_available'], memory_percent=snapshot['memory_percent'],
        disk_total=snapshot['disk_total'], disk_free=snapshot['disk_free'],
        disk_percent=round(100 * (snapshot['disk_total'] - snapshot['disk_free']) / snapshot['disk_total'], 1) if snapshot['disk_total'] else 0,
        processes=row['count'], process_cpu_percent=round(row['cpu'],2), process_rss=row['rss'], queued=queued)


@app.post('/processes/<int:process_id>/mutex/<action>')
@required
def process_mutex(process_id, action):
    process = conn().execute('''SELECT o.*,i.user_id instance_owner,i.name instance_name,r.state
        FROM os_processes o JOIN local_runtime r ON r.os_process_id=o.id
        JOIN instances i ON i.id=r.instance_id WHERE o.id=?''', (process_id,)).fetchone()
    if not process or not owned({'user_id': process['instance_owner']}):
        abort(404)
    if action not in ('acquire','release'):
        abort(404)
    if action == 'acquire' and process['state'] != 'running':
        flash('Start this local process before requesting the mutex.', 'error')
        return redirect(url_for('dashboard'))
    with conn():
        if action == 'acquire':
            acquired = conn().execute('UPDATE os_mutex SET owner_process_id=? WHERE id=1 AND owner_process_id IS NULL', (process_id,)).rowcount == 1
            if acquired:
                conn().execute("UPDATE os_processes SET process_state='RUNNING',wait_reason='' WHERE id=?", (process_id,))
                conn().execute("UPDATE local_runtime SET wait_reason='Inside shared critical section' WHERE os_process_id=?", (process_id,))
                event, detail = 'Mutex acquired', f'{process["instance_name"]} entered the shared critical section'
            else:
                owner = conn().execute('SELECT owner_process_id FROM os_mutex WHERE id=1').fetchone()['owner_process_id']
                conn().execute("UPDATE os_processes SET process_state='WAITING',wait_reason=? WHERE id=?", (f'Mutex held by process {owner}', process_id))
                conn().execute("UPDATE local_runtime SET wait_reason=? WHERE os_process_id=?", (f'Mutex held by process {owner}', process_id))
                event, detail = 'Mutex wait', f'{process["instance_name"]} is waiting for the shared mutex'
                flash('The shared critical section is busy. This process is marked waiting.', 'error')
        else:
            if conn().execute('SELECT owner_process_id FROM os_mutex WHERE id=1').fetchone()['owner_process_id'] != process_id:
                abort(400, 'Only the mutex owner can release it.')
            conn().execute('UPDATE os_mutex SET owner_process_id=NULL WHERE id=1')
            conn().execute("UPDATE os_processes SET process_state='RUNNING',wait_reason='' WHERE process_state='WAITING' AND wait_reason LIKE 'Mutex held by process %'")
            conn().execute("UPDATE local_runtime SET wait_reason='' WHERE wait_reason LIKE 'Mutex held by process %' OR wait_reason='Inside shared critical section'")
            event, detail = 'Mutex released', f'{process["instance_name"]} released the shared critical section'
            flash('Shared mutex released; waiting processes can proceed.', 'success')
        conn().execute('INSERT INTO os_events(user_id,process_id,event,detail) VALUES(?,?,?,?)', (who()['id'],process_id,event,detail))
        log(who()['id'], event, detail)
    return redirect(url_for('dashboard'))


# CREATE VOLUME
# Save a simulated extra storage volume for the current user.
@app.post('/volumes/create')
@required
def create_volume():
    u = who()
    name = request.form.get('name', '').strip()

    try:
        size = int(request.form.get('size', ''))
    except ValueError:
        size = 0

    if not name or len(name) > 40 or not 1 <= size <= 500:
        flash('Enter a volume name and size between 1 and 500 GB.', 'error')
        return redirect(url_for('dashboard'))

    with conn():
        conn().execute(
            'INSERT INTO volumes(user_id,name,size) VALUES(?,?,?)',
            (u['id'], name, size)
        )
        log(u['id'], 'Volume created', f'{name} • {size} GB')

    flash('Volume created.', 'success')
    return redirect(url_for('dashboard'))


# VOLUME ACTIONS
# Attach a volume to an instance, detach it, or delete it.
@app.post('/volumes/<int:item_id>/<action>')
@required
def volume_action(item_id, action):
    row = conn().execute(
        'SELECT * FROM volumes WHERE id=?', (item_id,)
    ).fetchone()

    if not owned(row):
        abort(404)

    with conn():
        if action == 'delete':
            conn().execute(
                'DELETE FROM volumes WHERE id=?', (item_id,)
            )
            log(who()['id'], 'Volume deleted', row['name'])

        elif action in ('attach', 'detach'):
            iid = None

            if action == 'attach':
                try:
                    iid = int(request.form.get('instance_id', ''))
                except ValueError:
                    abort(400)

                # Only allow attaching to an instance owned by this user.
                if not conn().execute(
                    'SELECT id FROM instances WHERE id=? AND user_id=?',
                    (iid, row['user_id'])
                ).fetchone():
                    abort(400, 'Choose an instance belonging to this user.')

            conn().execute(
                'UPDATE volumes SET instance_id=? WHERE id=?',
                (iid, item_id)
            )
            log(
                who()['id'],
                'Volume ' + ('attached' if action == 'attach' else 'detached'),
                row['name']
            )
        else:
            abort(404)

    flash('Volume updated.', 'success')
    return redirect(url_for('dashboard'))


# ERROR PAGES
# Display a friendly page for invalid requests or missing resources.
@app.errorhandler(400)
def bad(error):
    return render_template(
        'error.html',
        code=400,
        message=getattr(error, 'description', 'Please refresh and try again.')
    ), 400


@app.errorhandler(404)
def missing(_error):
    return render_template(
        'error.html',
        code=404,
        message='That resource was not found.'
    ), 404


# START THE APP
# Initialize the database, then run the development server on this computer only.
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', '5000'))
    # Opening the browser after the server has had a moment to bind avoids the
    # connection-refused page caused by opening localhost before Flask starts.
    threading.Timer(1.2, lambda: webbrowser.open_new_tab(f'http://127.0.0.1:{port}')).start()
    app.run(
        host='127.0.0.1',
        port=port,
        debug=False
    )
