import os
import sys

import mongomock
import pytest

# run.py reads these when it is imported.
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/test")
os.environ.setdefault("MONGO_DBNAME", "test")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flask_pymongo


class _FakeMongo:
    """PyMongo replacement backed by an in-process mongomock database."""

    _client = mongomock.MongoClient("mongodb://localhost:27017/test")

    def __init__(self, app=None, *args, **kwargs):
        self.db = self._client["test"]


flask_pymongo.PyMongo = _FakeMongo

import run as run_module


@pytest.fixture()
def app():
    run_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    # No HTTPS redirects from Flask-Talisman during tests.
    run_module.talisman.force_https = False
    # Reset every collection before a test starts.
    for name in run_module.mongo.db.list_collection_names():
        run_module.mongo.db.drop_collection(name)
    run_module.ensure_follow_indexes(run_module.mongo.db)
    return run_module.app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    return run_module.mongo.db
