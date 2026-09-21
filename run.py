import os
import sys
import time
import signal
import threading

from kadima.engine import Engine
from kadima.utils import in_premarket, in_market_hours, now_ist
from kadima import killswitch
from kadima import audit, alerts

def main():
    engine = Engine()
    from kadima import auto_executor, stock_fast, stock_fast_api, move_capture_execution
    stock_fast_worker = stock_fast.Worker(auto_executor.EXECUTOR, engine.scrips)
    move_capture_worker = move_capture_execution.MoveCaptureExecutionWorker(
        auto_executor.EXECUTOR, engine.scrips)

    def stop_all():
        move_capture_worker.stop()
        stock_fast_worker.stop()
        engine.stop()

    def _sigterm(signum, frame):
        audit.log("sigterm_received", signum=signum)
        stop_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    force_start = os.environ.get("KADIMA_FORCE_START", "0") == "1"

    # --- API FIRST -------------------------------------------------
    # Bind the web server before any blocking wait, so /state stays
    # reachable outside market hours and while the feed reconnects.
    # Without this the process hangs below and nginx returns 502.
    from kadima import api as state_api
    state_api.register(engine)
    import threading, uvicorn
    threading.Thread(
        target=lambda: uvicorn.run(state_api.app, host="0.0.0.0", port=8080,
                                     log_level="warning"),
        daemon=True, name="state-api").start()
    audit.log("state_api_started", port=8080)
    from kadima import control_api, auto_executor, angel_orders
    control_api.register(auto_executor.EXECUTOR, angel_orders)
    stock_fast_api.register(control_api, stock_fast_worker)
    threading.Thread(
        target=lambda: uvicorn.run(control_api.app, host="0.0.0.0", port=8081,
                                    log_level="warning"),
        daemon=True, name="control-api").start()
    audit.log("control_api_started", port=8081)

    if not force_start:
        while not (in_premarket() or in_market_hours()):
            time.sleep(30)

    engine.start()
    stock_fast_worker.start()
    move_capture_worker.start()

    try:
        while True:
            time.sleep(5)
            if killswitch.engaged():
                audit.log("kill_switch_detected_in_loop")
                alerts.send("Kill switch engaged - KADIMA halting.")
                stop_all()
                return 0
            if not force_start and not in_market_hours() and now_ist().hour >= 15:
                stop_all()
                return 0
    except KeyboardInterrupt:
        stop_all()
        return 0

if __name__ == "__main__":
    sys.exit(main())
