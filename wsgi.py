# Flask's `flask run` discovers wsgi.py automatically, so this module simply
# exposes the application defined in run.py. `python run.py` keeps working.
from run import app

if __name__ == "__main__":
    app.run()
