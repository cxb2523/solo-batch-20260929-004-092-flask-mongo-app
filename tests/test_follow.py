import os
import sys
import uuid
from datetime import datetime

import mongomock
import pytest

os.environ.setdefault("MONGO_DBNAME", "follow_test")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/follow_test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import run
from user import User
from backfill_follows import backfill


class FakeMongo:
    def __init__(self):
        self.client = mongomock.MongoClient()
        self.db = self.client["follow_test"]


@pytest.fixture()
def env(monkeypatch):
    fake = FakeMongo()
    monkeypatch.setattr(run, "mongo", fake)
    run.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                          TALISMAN_FORCE_HTTPS=False, BCRYPT_LOG_ROUNDS=4)
    run.talisman.force_https = False
    with run.app.test_client() as client:
        yield client, fake.db


def make_user(db, first="Ann", last="Able", **overrides):
    user = User("Ms", first, last, uuid.uuid4().hex[:8] + "@example.com")
    data = user.dict()
    data["password"] = run.bc.generate_password_hash("pw").decode("utf-8")
    data.update(overrides)
    db.users.insert_one(data)
    return data


def make_legacy_user(db, first="Leg", last="Ace"):
    # Old-style document without any of the follow-related fields
    data = {
        "id": uuid.uuid4().hex,
        "title": "Mr",
        "first_name": first,
        "last_name": last,
        "email": uuid.uuid4().hex[:8] + "@example.com",
        "verified": True,
        "password": run.bc.generate_password_hash("pw").decode("utf-8"),
    }
    db.users.insert_one(data)
    return data


def make_note(db, user, title):
    db.notes.insert_one({
        "title": title,
        "body": "body of " + title,
        "user_id": user["id"],
        "user_name": user["first_name"] + " " + user["last_name"],
        "timestamp": datetime.utcnow(),
        "date_string": "1 Jan 2026",
        "id": uuid.uuid4().hex,
        "deleted": False,
    })


def login(client, user):
    response = client.post("/login", data={"email": user["email"], "pass": "pw"})
    assert response.status_code == 302
    assert client.get("/profile").status_code == 200


def follows_doc(db, user_id):
    return db.follows.find_one({"user_id": user_id}) or {}


def counts(db, user_id):
    user = db.users.find_one({"id": user_id})
    return user["followers_count"], user["following_count"]


# Idempotency

def test_follow_is_idempotent(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    bob = make_user(db, "Bob", "Baker")
    login(client, alice)
    for _ in range(2):
        response = client.post("/follow/" + bob["id"],
                               headers={"X-Requested-With": "XMLHttpRequest"})
        assert response.status_code == 200
        assert response.get_json()["status"] == "active"
    assert follows_doc(db, alice["id"])["following"] == [bob["id"]]
    assert follows_doc(db, bob["id"])["followers"] == [alice["id"]]
    assert counts(db, alice["id"]) == (0, 1)
    assert counts(db, bob["id"]) == (1, 0)


def test_unfollow_is_idempotent(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    bob = make_user(db, "Bob", "Baker")
    login(client, alice)
    client.post("/follow/" + bob["id"])
    for _ in range(2):
        response = client.post("/unfollow/" + bob["id"],
                               headers={"X-Requested-With": "XMLHttpRequest"})
        assert response.status_code == 200
        assert response.get_json()["status"] == "none"
    assert follows_doc(db, alice["id"])["following"] == []
    assert follows_doc(db, bob["id"])["followers"] == []
    assert counts(db, alice["id"]) == (0, 0)
    assert counts(db, bob["id"]) == (0, 0)


def test_follow_unfollow_cycle_keeps_counts_consistent(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    bob = make_user(db, "Bob", "Baker")
    login(client, alice)
    for _ in range(3):
        client.post("/follow/" + bob["id"])
        client.post("/unfollow/" + bob["id"])
    assert counts(db, alice["id"]) == (0, 0)
    assert counts(db, bob["id"]) == (0, 0)
    client.post("/follow/" + bob["id"])
    assert counts(db, alice["id"]) == (0, 1)
    assert counts(db, bob["id"]) == (1, 0)


# State transitions (private accounts)

def test_private_account_pending_then_approve(env):
    client, db = env
    carol = make_user(db, "Carol", "Clark", is_private=True)
    dave = make_user(db, "Dave", "Dunn")
    login(client, dave)
    response = client.post("/follow/" + carol["id"],
                           headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.get_json()["status"] == "pending"
    assert follows_doc(db, carol["id"])["pending"] == [dave["id"]]
    assert counts(db, carol["id"]) == (0, 0)
    assert counts(db, dave["id"]) == (0, 0)
    client.get("/logout")
    login(client, carol)
    response = client.post("/follow_requests/approve/" + dave["id"],
                           headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.get_json()["status"] == "active"
    assert follows_doc(db, carol["id"])["pending"] == []
    assert follows_doc(db, carol["id"])["followers"] == [dave["id"]]
    assert follows_doc(db, dave["id"])["following"] == [carol["id"]]
    assert counts(db, carol["id"]) == (1, 0)
    assert counts(db, dave["id"]) == (0, 1)
    # Approving again is an idempotent no-op
    response = client.post("/follow_requests/approve/" + dave["id"],
                           headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200
    assert counts(db, carol["id"]) == (1, 0)
    assert counts(db, dave["id"]) == (0, 1)


def test_decline_pending_request(env):
    client, db = env
    carol = make_user(db, "Carol", "Clark", is_private=True)
    dave = make_user(db, "Dave", "Dunn")
    login(client, dave)
    client.post("/follow/" + carol["id"])
    client.get("/logout")
    login(client, carol)
    response = client.post("/follow_requests/decline/" + dave["id"],
                           headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200
    assert follows_doc(db, carol["id"])["pending"] == []
    assert follows_doc(db, carol["id"])["followers"] == []
    assert counts(db, carol["id"]) == (0, 0)
    assert counts(db, dave["id"]) == (0, 0)


def test_unfollow_cancels_pending_request(env):
    client, db = env
    carol = make_user(db, "Carol", "Clark", is_private=True)
    dave = make_user(db, "Dave", "Dunn")
    login(client, dave)
    client.post("/follow/" + carol["id"])
    client.post("/unfollow/" + carol["id"])
    assert follows_doc(db, carol["id"])["pending"] == []
    assert counts(db, carol["id"]) == (0, 0)


# Permission branches

def test_cannot_follow_self(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    login(client, alice)
    response = client.post("/follow/" + alice["id"],
                           headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 400
    assert follows_doc(db, alice["id"]).get("following", []) == []
    assert counts(db, alice["id"]) == (0, 0)


def test_anonymous_follow_redirects_to_login(env):
    client, db = env
    bob = make_user(db, "Bob", "Baker")
    response = client.post("/follow/" + bob["id"])
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_profile_page_permission_branches(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    bob = make_user(db, "Bob", "Baker")
    carol = make_user(db, "Carol", "Clark", is_private=True)
    bob_page = "/profile/" + User.make_from_dict(bob).profile_name()

    # Anonymous visitor is offered the login page
    page = client.get(bob_page).get_data(as_text=True)
    assert "Log in to follow" in page

    login(client, alice)
    # Not following yet: Follow button
    page = client.get(bob_page).get_data(as_text=True)
    assert 'action="/follow/' + bob["id"] + '"' in page
    # Following: Unfollow button
    client.post("/follow/" + bob["id"])
    page = client.get(bob_page).get_data(as_text=True)
    assert 'action="/unfollow/' + bob["id"] + '"' in page
    # Private account: pending approval
    client.post("/follow/" + carol["id"])
    page = client.get("/profile/" + User.make_from_dict(carol).profile_name()
                      ).get_data(as_text=True)
    assert "pending approval" in page
    # Own profile: no follow button
    page = client.get("/profile/" + User.make_from_dict(alice).profile_name()
                      ).get_data(as_text=True)
    assert "This is your own profile" in page


# Home feed filtering

def test_home_feed_only_shows_followed_authors(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    bob = make_user(db, "Bob", "Baker")
    carol = make_user(db, "Carol", "Clark")
    make_note(db, bob, "Bob followed note")
    make_note(db, carol, "Carol unfollowed note")
    login(client, alice)
    client.post("/follow/" + bob["id"])
    page = client.get("/").get_data(as_text=True)
    assert "Bob followed note" in page
    assert "Carol unfollowed note" not in page


# Legacy data compatibility

def test_legacy_user_missing_fields_fails_until_backfilled(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    legacy = make_legacy_user(db)
    # Loading a legacy document into the model raises immediately
    with pytest.raises(KeyError):
        User.make_from_dict(db.users.find_one({"id": legacy["id"]}))
    login(client, alice)
    response = client.post("/follow/" + legacy["id"])
    assert response.status_code == 500
    # Backfill upgrades the legacy document, then following works
    backfill(db)
    response = client.post("/follow/" + legacy["id"],
                           headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200
    assert response.get_json()["status"] == "active"
    assert db.users.find_one({"id": legacy["id"]})["followers_count"] == 1


def test_backfill_is_idempotent_and_preserves_counts(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    bob = make_user(db, "Bob", "Baker")
    login(client, alice)
    client.post("/follow/" + bob["id"])
    # Corrupt the store: duplicate edge and a self-edge written directly
    db.follows.update_one({"user_id": alice["id"]},
                          {"$push": {"following": bob["id"]}})
    db.follows.update_one({"user_id": alice["id"]},
                          {"$push": {"following": alice["id"]}})
    counts_before = (counts(db, alice["id"]), counts(db, bob["id"]))
    backfill(db)
    second_run = backfill(db)
    assert follows_doc(db, alice["id"])["following"] == [bob["id"]]
    assert follows_doc(db, bob["id"])["followers"] == [alice["id"]]
    assert (counts(db, alice["id"]), counts(db, bob["id"])) == counts_before
    assert second_run["follows_docs_created"] == 0
    assert second_run["follows_docs_deduplicated"] == 0
    assert second_run["user_fields_filled"] == 0


# Form fallback and stats page

def test_follow_form_fallback_redirects(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    bob = make_user(db, "Bob", "Baker")
    login(client, alice)
    response = client.post("/follow/" + bob["id"])
    assert response.status_code == 302
    assert follows_doc(db, alice["id"])["following"] == [bob["id"]]


def test_stats_page(env):
    client, db = env
    alice = make_user(db, "Alice", "Adams")
    bob = make_user(db, "Bob", "Baker")
    login(client, alice)
    client.post("/follow/" + bob["id"])
    page = client.get("/stats").get_data(as_text=True)
    assert "Follow relationships" in page
    assert "Bob Baker" in page
