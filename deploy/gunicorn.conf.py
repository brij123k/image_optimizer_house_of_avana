"""Production server settings (used by the systemd service).

ONE worker with several threads, on purpose: the app keeps the state of a running
optimization job in memory, so every request must reach the same process.
Threads still let many merchants use the app at the same time.
"""
bind = "127.0.0.1:8000"      # nginx forwards to this; it is never exposed directly
workers = 1
threads = 8
timeout = 300                 # store scans and AI runs are slow requests
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
