from flask import Flask, jsonify, request, Response, send_from_directory
from collections import deque
import threading
import queue
import time
import datetime
import json
import os

from main import scrape_query, generate_queries
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# ── Global state ───────────────────────────────────────────────────────────────
state = {
    'running': False,
    'queries_done': 0,
    'businesses_found': 0,
    'current_query': '',
    'session_csv': '',
    'start_time': None,
    'end_time': None,
}
log_buffer = deque(maxlen=300)   # history shown on page reload
log_q = queue.Queue(maxsize=500) # live stream to SSE clients
stop_event = threading.Event()


def emit(msg: str):
    print(msg)
    log_buffer.append(msg)
    try:
        log_q.put_nowait(msg)
    except queue.Full:
        pass


def infinite_queries(locations: list[str]):
    """Cycle through category × location combinations until stopped."""
    while True:
        yield from generate_queries(locations)


# ── Scraper thread ─────────────────────────────────────────────────────────────
def run_scraper(mode: str, search: str, locations: str, duration: int, total: int):
    stop_event.clear()

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    session_ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    save_dir = os.path.join('GMaps Data', today)
    os.makedirs(save_dir, exist_ok=True)
    session_csv = os.path.join(save_dir, f"session_{session_ts}.csv")

    end_time = time.time() + duration * 60 if duration else None

    state.update({
        'running': True,
        'queries_done': 0,
        'businesses_found': 0,
        'session_csv': session_csv,
        'start_time': time.time(),
        'end_time': end_time,
        'current_query': '',
    })

    emit(f"Session CSV: {session_csv}")
    if duration:
        stop_at = datetime.datetime.fromtimestamp(end_time).strftime('%H:%M:%S')
        emit(f"Running for {duration} min — will stop at {stop_at}")

    try:
        with sync_playwright() as p:
            # Persistent profile so cookie consent / preferences survive between runs
            user_data_dir = os.path.abspath('./browser_data')
            os.makedirs(user_data_dir, exist_ok=True)

            context = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
                locale="en-GB",
                viewport={'width': 1280, 'height': 800},
            )
            page = context.pages[0] if context.pages else context.new_page()

            emit("Opening Google Maps...")
            page.goto("https://www.google.com/maps", timeout=60000)
            page.wait_for_timeout(2500)

            # Handle cookie consent on first run (or if the profile was wiped)
            consent_selectors = [
                'button[aria-label="Accept all"]',
                'button[aria-label="Reject all"]',
                'button:has-text("Accept all")',
                'button:has-text("Reject all")',
                'form[action*="consent"] button[type="submit"]',
            ]
            for sel in consent_selectors:
                try:
                    btn = page.locator(sel)
                    if btn.count() > 0:
                        emit("Accepting cookie consent...")
                        btn.first.click()
                        page.wait_for_timeout(3000)
                        break
                except Exception:
                    continue

            if mode == 'single':
                query_iter = iter([search])
            else:
                loc_list = [l.strip() for l in locations.splitlines() if l.strip()]
                query_iter = infinite_queries(loc_list)

            for search_for in query_iter:
                if stop_event.is_set():
                    emit("Stopped by user.")
                    break
                if end_time and time.time() >= end_time:
                    emit("Time limit reached.")
                    break

                remaining = (end_time - time.time()) / 60 if end_time else None
                time_tag = f" ({remaining:.1f} min left)" if remaining is not None else ""
                emit(f"[Query {state['queries_done'] + 1}]{time_tag}: {search_for}")
                state['current_query'] = search_for

                try:
                    added = scrape_query(page, search_for, total, session_csv, log_fn=emit)
                    state['queries_done'] += 1
                    state['businesses_found'] += added
                    emit(f"  +{added} businesses | session total: {state['businesses_found']}")
                except Exception as e:
                    emit(f"  Query error: {e}")

            context.close()

    except Exception as e:
        emit(f"Fatal error: {e}")
    finally:
        state['running'] = False
        state['current_query'] = ''
        emit("Done.")


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/start', methods=['POST'])
def start():
    if state['running']:
        return jsonify({'error': 'Scraper is already running'}), 400

    data = request.json or {}
    mode = data.get('mode', 'single')
    search = data.get('search', '').strip()
    locations = data.get('locations', '').strip()
    duration = int(data.get('duration') or 0)
    total = int(data.get('total') or 0) or 1_000_000

    if mode == 'single' and not search:
        return jsonify({'error': 'Search query is required'}), 400
    if mode == 'continuous' and not locations:
        return jsonify({'error': 'At least one location is required'}), 400

    # Clear stale log queue
    while not log_q.empty():
        try:
            log_q.get_nowait()
        except queue.Empty:
            break

    thread = threading.Thread(
        target=run_scraper,
        args=(mode, search, locations, duration or None, total),
        daemon=True,
    )
    thread.start()
    return jsonify({'ok': True})


@app.route('/stop', methods=['POST'])
def stop():
    stop_event.set()
    return jsonify({'ok': True})


@app.route('/status')
def status():
    s = dict(state)
    if s['end_time'] and s['running']:
        s['seconds_left'] = max(0, int(s['end_time'] - time.time()))
    else:
        s['seconds_left'] = None
    return jsonify(s)


@app.route('/logs')
def logs():
    return jsonify(list(log_buffer))


@app.route('/stream')
def stream():
    def event_gen():
        while True:
            try:
                msg = log_q.get(timeout=10)
                yield f"data: {json.dumps(msg)}\n\n"
            except queue.Empty:
                yield "data: \"\"\n\n"  # heartbeat keeps connection alive

    return Response(
        event_gen(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


if __name__ == '__main__':
    print("Starting server at http://localhost:5000")
    app.run(port=5000, threaded=True, use_reloader=False)
