import csv, io, secrets, sqlite3
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, Response, session, g
import qrcode
import config

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
FMT = '%Y-%m-%d %H:%M:%S'


def db():
    if 'db' not in g:
        g.db = sqlite3.connect(config.DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop('db', None)
    if d:
        d.close()


def now():
    return datetime.now().strftime(FMT)


def new_token():
    return "STU-" + secrets.token_urlsafe(6)


def init_db():
    c = db()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS students (
            student_id TEXT PRIMARY KEY, name TEXT NOT NULL, class_name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, student_id TEXT NOT NULL,
            scanned_at TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, started_at TEXT NOT NULL,
            ended_at TEXT, late_min INTEGER NOT NULL, absent_min INTEGER NOT NULL);
    ''')
    cols = lambda t: [r[1] for r in c.execute(f'PRAGMA table_info({t})')]
    if 'qr_token' not in cols('students'):
        c.execute('ALTER TABLE students ADD COLUMN qr_token TEXT')
    if 'session_id' not in cols('attendance_logs'):
        c.execute('ALTER TABLE attendance_logs ADD COLUMN session_id INTEGER')
    for r in c.execute('SELECT student_id FROM students WHERE qr_token IS NULL').fetchall():
        c.execute('UPDATE students SET qr_token=? WHERE student_id=?', (new_token(), r[0]))
    c.commit()


with app.app_context():
    init_db()


# ---------- instructor login ----------
def authed():
    return not config.INSTRUCTOR_PIN or session.get('ok')


def instructor(f):
    @wraps(f)
    def w(*a, **k):
        if not authed():
            return jsonify(message='Instructor login required.'), 401
        return f(*a, **k)
    return w


@app.route('/api/login', methods=['POST'])
def login():
    if (request.get_json(silent=True) or {}).get('pin') == config.INSTRUCTOR_PIN:
        session['ok'] = True
        return jsonify(status='ok')
    return jsonify(message='Wrong PIN.'), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify(status='ok')


# ---------- sessions ----------
def current(c):
    return c.execute('SELECT * FROM sessions ORDER BY id DESC LIMIT 1').fetchone()


def active(c):
    s = current(c)
    return s if s and not s['ended_at'] else None


def phase(s):
    mins = (datetime.now() - datetime.strptime(s['started_at'], FMT)).total_seconds() / 60
    return mins, ('PRESENT' if mins <= s['late_min'] else 'LATE' if mins <= s['absent_min'] else 'ABSENT')


def read_times():
    d = request.get_json(silent=True) or {}
    try:
        late, absent = int(d.get('late_min')), int(d.get('absent_min'))
    except (TypeError, ValueError):
        return None, 'Enter the minutes as whole numbers.'
    if not 0 <= late < absent:
        return None, '"Absent after" must be bigger than "Late after".'
    return ((d.get('label') or '').strip() or 'Class', late, absent), None


@app.route('/api/session/start', methods=['POST'])
@instructor
def session_start():
    t, err = read_times()
    if err:
        return jsonify(message=err), 400
    c = db()
    s = active(c)
    if s:
        c.execute('UPDATE sessions SET ended_at=? WHERE id=?', (now(), s['id']))
    c.execute('INSERT INTO sessions (label,started_at,late_min,absent_min) VALUES (?,?,?,?)', (t[0], now(), t[1], t[2]))
    c.commit()
    return jsonify(status='ok')


@app.route('/api/session/update', methods=['POST'])
@instructor
def session_update():
    t, err = read_times()
    if err:
        return jsonify(message=err), 400
    c = db()
    s = active(c)
    if not s:
        return jsonify(message='No attendance is running.'), 409
    c.execute('UPDATE sessions SET label=?, late_min=?, absent_min=? WHERE id=?', (*t, s['id']))
    c.commit()
    return jsonify(status='ok')


@app.route('/api/session/end', methods=['POST'])
@instructor
def session_end():
    c = db()
    s = active(c)
    if s:
        c.execute('UPDATE sessions SET ended_at=? WHERE id=?', (now(), s['id']))
        c.commit()
    return jsonify(status='ok')


# ---------- pages ----------
@app.route('/')
def index():
    return render_template('register.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html', authed=authed(),
                           d_late=config.DEFAULT_LATE_MIN, d_absent=config.DEFAULT_ABSENT_MIN)


# ---------- students (public) ----------
@app.route('/api/register', methods=['POST'])
def register():
    d = request.get_json(silent=True) or {}
    sid, name, cls = [(d.get(k) or '').strip() for k in ('student_id', 'name', 'class_name')]
    if not (sid and name and cls):
        return jsonify(message='Fill in your Student ID, name and class.'), 400
    token = new_token()
    try:
        db().execute('INSERT INTO students (student_id,name,class_name,qr_token) VALUES (?,?,?,?)', (sid, name, cls, token))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(message=f'Student ID {sid} is already registered. Use "Lost your QR?" below.'), 409
    return jsonify(status='ok', qr_token=token), 201


@app.route('/api/my-qr')
def my_qr():
    sid = request.args.get('student_id', '').strip()
    name = request.args.get('name', '').strip().lower()
    s = db().execute('SELECT * FROM students WHERE student_id=?', (sid,)).fetchone()
    if not s or s['name'].lower() != name:
        return jsonify(message='No match. Type the ID and name exactly as you registered them.'), 404
    return jsonify(student_id=s['student_id'], name=s['name'], class_name=s['class_name'], qr_token=s['qr_token'])


@app.route('/api/qr')
def qr_image():
    img = qrcode.make(request.args.get('d', ''), box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


# ---------- instructor API ----------
@app.route('/api/scan', methods=['POST'])
@instructor
def scan():
    token = ((request.get_json(silent=True) or {}).get('qr_code') or '').strip()
    c = db()
    s = active(c)
    if not s:
        return jsonify(status='no_session', message='Press Start attendance first.'), 409
    st = c.execute('SELECT * FROM students WHERE qr_token=?', (token,)).fetchone()
    if not st:
        return jsonify(status='not_found', message="This QR code isn't registered."), 404
    prev = c.execute('SELECT scanned_at, status FROM attendance_logs WHERE session_id=? AND student_id=?',
                     (s['id'], st['student_id'])).fetchone()
    if prev:
        return jsonify(status='duplicate', student_name=st['name'], attendance=prev['status'], time=prev['scanned_at'])
    status = phase(s)[1]
    t = now()
    c.execute('INSERT INTO attendance_logs (student_id, scanned_at, status, session_id) VALUES (?,?,?,?)',
              (st['student_id'], t, status, s['id']))
    c.commit()
    return jsonify(status='success', student_name=st['name'], class_name=st['class_name'], attendance=status, time=t)


@app.route('/api/dashboard')
@instructor
def api_dashboard():
    c = db()
    s = current(c)
    total = c.execute('SELECT COUNT(*) FROM students').fetchone()[0]
    if not s:
        return jsonify(session=None, active=False, logs=[], pending=[], present=0, late=0, absent=total)
    logs = [dict(r) for r in c.execute('''
        SELECT a.student_id, st.name, st.class_name, a.scanned_at, a.status
        FROM attendance_logs a JOIN students st ON a.student_id=st.student_id
        WHERE a.session_id=? ORDER BY a.id DESC''', (s['id'],))]
    pending = [dict(r) for r in c.execute('''
        SELECT student_id, name FROM students WHERE student_id NOT IN
        (SELECT student_id FROM attendance_logs WHERE session_id=?) ORDER BY name''', (s['id'],))]
    p = sum(l['status'] == 'PRESENT' for l in logs)
    lt = sum(l['status'] == 'LATE' for l in logs)
    mins, ph = phase(s)
    return jsonify(session=dict(s), active=not s['ended_at'], minutes=mins, phase=ph,
                   logs=logs, pending=pending, present=p, late=lt, absent=total - p - lt)


@app.route('/api/export.csv')
@instructor
def export_csv():
    c = db()
    sid = request.args.get('session', type=int)
    s = c.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone() if sid else current(c)
    if not s:
        return jsonify(message='No attendance session yet.'), 404
    rows = [list(r) for r in c.execute('''
        SELECT a.student_id, st.name, st.class_name, a.scanned_at, a.status
        FROM attendance_logs a JOIN students st ON a.student_id=st.student_id
        WHERE a.session_id=? ORDER BY a.id''', (s['id'],))]
    rows += [[r[0], r[1], r[2], '', 'ABSENT'] for r in c.execute('''
        SELECT student_id, name, class_name FROM students WHERE student_id NOT IN
        (SELECT student_id FROM attendance_logs WHERE session_id=?) ORDER BY name''', (s['id'],))]
    safe = lambda v: "'" + str(v) if str(v)[:1] and str(v)[0] in '=+-@' else v
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Student ID', 'Name', 'Class', 'Scanned at', 'Status'])
    for r in rows:
        w.writerow([safe(v) for v in r])
    return Response(out.getvalue(), mimetype='text/csv', headers={
        'Content-Disposition': f'attachment; filename=attendance_{s["started_at"][:10]}_{s["id"]}.csv'})


@app.route('/api/students')
@instructor
def list_students():
    return jsonify([dict(r) for r in db().execute('SELECT * FROM students ORDER BY class_name, name')])


@app.route('/api/students/<sid>', methods=['DELETE'])
@instructor
def delete_student(sid):
    c = db()
    c.execute('DELETE FROM attendance_logs WHERE student_id=?', (sid,))
    c.execute('DELETE FROM students WHERE student_id=?', (sid,))
    c.commit()
    return jsonify(status='ok')


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)