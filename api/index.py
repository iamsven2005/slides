from flask import Flask, request, jsonify, session, Response, render_template
import psycopg2
from flask_cors import CORS
import os
import time
import threading
from dotenv import load_dotenv
from queue import Queue

load_dotenv()
app = Flask(__name__)
CORS(app)

app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'fallback_secret')
conn_str = os.environ.get('DATABASE_URL')

def get_db():
    return psycopg2.connect(conn_str)

clients = []

def push_update():
    for q in clients:
        q.put("data: update\n\n")
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/events')
def stream():
    def event_stream(q):
        try:
            while True:
                data = q.get()
                yield data
        except GeneratorExit:
            clients.remove(q)

    q = Queue()
    clients.append(q)
    return Response(event_stream(q), mimetype="text/event-stream")

@app.route('/login', methods=['POST'])
def login():
    username = request.json['username']
    session['username'] = username
    return jsonify({'status': 'ok'})

@app.route('/logout')
def logout():
    session.clear()
    return jsonify({'status': 'logged out'})

@app.route('/add', methods=['POST'])
def add():
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401

    task = request.json['task']
    username = session['username']

    with get_db() as conn:
        with conn.cursor() as cur:
            # 1. Get the user's default column (create if not exists)
            cur.execute("SELECT id FROM columns WHERE owner = %s ORDER BY id LIMIT 1;", (username,))
            result = cur.fetchone()
            if result:
                column_id = result[0]
            else:
                cur.execute("INSERT INTO columns (name, owner) VALUES (%s, %s) RETURNING id;", ("To Do", username))
                column_id = cur.fetchone()[0]

            # 2. Get next position
            cur.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM todos WHERE column_id = %s AND owner = %s;",
                        (column_id, username))
            position = cur.fetchone()[0]

            # 3. Insert task
            cur.execute("INSERT INTO todos (task, owner, column_id, position) VALUES (%s, %s, %s, %s);",
                        (task, username, column_id, position))

    push_update()
    return jsonify({'status': 'ok'})


@app.route('/list')
def list_tasks():
    if 'username' not in session:
        return jsonify([])
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, task FROM todos WHERE owner = %s ORDER BY id;", (session['username'],))
            todos = cur.fetchall()
    return jsonify(todos)

@app.route('/edit/<int:id>', methods=['PUT'])
def edit_task(id):
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    new_task = request.json['task']
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE todos SET task = %s WHERE id = %s AND owner = %s;", (new_task, id, session['username']))
            if cur.rowcount == 0:
                return jsonify({'error': 'task not found or not owned by user'}), 404
    push_update()
    return jsonify({'status': 'updated'})

@app.route('/delete/<int:id>', methods=['DELETE'])
def delete_task(id):
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM todos WHERE id = %s AND owner = %s;", (id, session['username']))
            if cur.rowcount == 0:
                return jsonify({'error': 'task not found or not owned by user'}), 404
    push_update()
    return jsonify({'status': 'deleted'})
@app.route('/column', methods=['POST'])
def add_column():
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    name = request.json['name']
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO columns (name, owner) VALUES (%s, %s);", (name, session['username']))
    push_update()
    return jsonify({'status': 'ok'})

@app.route('/column/<int:id>', methods=['DELETE'])
def delete_column(id):
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM todos WHERE column_id = %s AND owner = %s;", (id, session['username']))
            cur.execute("DELETE FROM columns WHERE id = %s AND owner = %s;", (id, session['username']))
    push_update()
    return jsonify({'status': 'deleted'})


@app.route('/board')
def get_board():
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM columns WHERE owner = %s ORDER BY id;", (session['username'],))
            columns = cur.fetchall()
            board = []
            for col_id, col_name in columns:
                cur.execute("""
                    SELECT id, task, position FROM todos
                    WHERE column_id = %s AND owner = %s
                    ORDER BY position ASC;
                """, (col_id, session['username']))
                tasks = cur.fetchall()
                board.append({
                    'id': col_id,
                    'name': col_name,
                    'tasks': [{'id': tid, 'task': t, 'position': pos} for tid, t, pos in tasks]
                })
    return jsonify(board)
@app.route('/move', methods=['POST'])
def move_task():
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json  # expects: {task_id, to_column_id, new_position}
    with get_db() as conn:
        with conn.cursor() as cur:
            # Update task position and column
            cur.execute("""
                UPDATE todos
                SET column_id = %s, position = %s
                WHERE id = %s AND owner = %s;
            """, (data['to_column_id'], data['new_position'], data['task_id'], session['username']))
    push_update()
    return jsonify({'status': 'moved'})
@app.route('/column/<int:id>/duplicate', methods=['POST'])
def duplicate_column(id):
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    username = session['username']

    with get_db() as conn:
        with conn.cursor() as cur:
            # Get original column name
            cur.execute("SELECT name FROM columns WHERE id = %s AND owner = %s;", (id, username))
            result = cur.fetchone()
            if not result:
                return jsonify({'error': 'column not found'}), 404
            original_name = result[0]

            # Create new column
            new_name = f"{original_name} (copy)"
            cur.execute("INSERT INTO columns (name, owner) VALUES (%s, %s) RETURNING id;", (new_name, username))
            new_col_id = cur.fetchone()[0]

            # Copy tasks
            cur.execute("""
                SELECT task, position FROM todos
                WHERE column_id = %s AND owner = %s
                ORDER BY position ASC;
            """, (id, username))
            tasks = cur.fetchall()

            for task, pos in tasks:
                cur.execute("""
                    INSERT INTO todos (task, owner, column_id, position)
                    VALUES (%s, %s, %s, %s);
                """, (task, username, new_col_id, pos))

    push_update()
    return jsonify({'status': 'duplicated'})
@app.route('/column/<int:id>', methods=['PUT'])
def rename_column(id):
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.json
    new_name = data.get('name', '').strip()
    if not new_name:
        return jsonify({'error': 'name required'}), 400

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE columns SET name = %s
                WHERE id = %s AND owner = %s;
            """, (new_name, id, session['username']))
    push_update()
    return jsonify({'status': 'renamed'})

if __name__ == '__main__':
    app.run(debug=True, threaded=True)
