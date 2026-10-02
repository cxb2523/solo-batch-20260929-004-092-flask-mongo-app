"""Acceptance tests for the follow-relationship feature."""

import importlib.util
import os

import pytest

from note import Note, feed_notes
from user import (
    FollowError,
    STATE_FOLLOWING,
    STATE_NOT_FOLLOWING,
    STATE_PENDING,
    STATE_SELF,
    follow_state,
    follow_user,
    unfollow_user,
    approve_follow,
    deny_follow,
    get_following,
)

import run as run_module


BACKFILL_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts", "backfill_follows.py"))
spec = importlib.util.spec_from_file_location("backfill_follows", BACKFILL_PATH)
backfill_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill_module)


def make_user(db, name, is_private=False, with_counters=True):
    user = {
        "id": name + "-id",
        "title": "",
        "first_name": name,
        "last_name": "User",
        "email": name + "@example.com",
        "verified": True,
        "is_private": is_private,
        "password": "hashed",
    }
    if with_counters:
        user["following_count"] = 0
        user["followers_count"] = 0
    db.users.insert_one(user)
    return user["id"]


def counter_pair(db, user_id):
    doc = db.users.find_one({"id": user_id})
    return doc["following_count"], doc["followers_count"]


def all_counters(db, user_ids):
    return {uid: counter_pair(db, uid) for uid in user_ids}


def set_password(db, user_id):
    db.users.update_one(
        {"id": user_id},
        {"$set": {"password": run_module.bc.generate_password_hash(
            "secret").decode("utf-8")}})


def login(client, user_id):
    email = user_id.replace("-id", "") + "@example.com"
    return client.post("/login",
                       data={"email": email, "pass": "secret"},
                       follow_redirects=False)


class TestIdempotency:
    def test_repeated_follow_creates_single_edge_and_counter(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")

        assert follow_user(db, alice, bob) == STATE_FOLLOWING
        assert follow_user(db, alice, bob) == STATE_FOLLOWING
        assert follow_user(db, alice, bob) == STATE_FOLLOWING

        assert get_following(db, alice) == [bob]
        assert db.follows.count_documents({}) == 1
        assert all_counters(db, [alice, bob]) == {
            alice: (1, 0), bob: (0, 1)}

    def test_repeated_unfollow_is_idempotent(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")
        follow_user(db, alice, bob)

        assert unfollow_user(db, alice, bob) == STATE_NOT_FOLLOWING
        assert unfollow_user(db, alice, bob) == STATE_NOT_FOLLOWING

        assert get_following(db, alice) == []
        assert all_counters(db, [alice, bob]) == {
            alice: (0, 0), bob: (0, 0)}

    def test_unique_index_guards_one_document_per_user(self, db):
        import pymongo
        alice = make_user(db, "alice")
        make_user(db, "bob")
        db.follows.insert_one({"user_id": alice, "following": []})
        with pytest.raises(pymongo.errors.DuplicateKeyError):
            db.follows.insert_one({"user_id": alice, "following": []})


class TestCounters:
    def test_follow_unfollow_keeps_counters_consistent(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")
        carol = make_user(db, "carol")

        follow_user(db, alice, bob)
        follow_user(db, carol, bob)
        assert all_counters(db, [alice, bob, carol]) == {
            alice: (1, 0), bob: (0, 2), carol: (1, 0)}

        unfollow_user(db, alice, bob)
        assert all_counters(db, [alice, bob, carol]) == {
            alice: (0, 0), bob: (0, 1), carol: (1, 0)}

    def test_pending_requests_do_not_move_counters(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob", is_private=True)

        assert follow_user(db, alice, bob) == STATE_PENDING
        assert follow_user(db, alice, bob) == STATE_PENDING
        assert all_counters(db, [alice, bob]) == {
            alice: (0, 0), bob: (0, 0)}

        unfollow_user(db, alice, bob)
        assert all_counters(db, [alice, bob]) == {
            alice: (0, 0), bob: (0, 0)}


class TestStateTransitions:
    def test_pending_then_approve_creates_edge_and_counters(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob", is_private=True)

        assert follow_state(db, alice, bob) == STATE_NOT_FOLLOWING
        assert follow_user(db, alice, bob) == STATE_PENDING
        assert follow_state(db, alice, bob) == STATE_PENDING

        assert approve_follow(db, bob, alice) == STATE_FOLLOWING
        assert follow_state(db, alice, bob) == STATE_FOLLOWING
        assert all_counters(db, [alice, bob]) == {
            alice: (1, 0), bob: (0, 1)}

    def test_pending_then_deny_leaves_no_edge(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob", is_private=True)
        follow_user(db, alice, bob)

        deny_follow(db, bob, alice)
        assert follow_state(db, alice, bob) == STATE_NOT_FOLLOWING
        assert all_counters(db, [alice, bob]) == {
            alice: (0, 0), bob: (0, 0)}

    def test_approve_when_already_approved_is_idempotent(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob", is_private=True)
        follow_user(db, alice, bob)

        approve_follow(db, bob, alice)
        approve_follow(db, bob, alice)
        assert all_counters(db, [alice, bob]) == {
            alice: (1, 0), bob: (0, 1)}

    def test_making_account_public_after_request_does_not_auto_follow(self, db):
        # An unanswered pending request remains pending until explicit action.
        alice = make_user(db, "alice")
        bob = make_user(db, "bob", is_private=True)
        follow_user(db, alice, bob)
        db.users.update_one({"id": bob}, {"$set": {"is_private": False}})
        assert follow_state(db, alice, bob) == STATE_PENDING


class TestFeed:
    def test_home_route_filters_to_followed_authors(self, client, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")
        carol = make_user(db, "carol")
        set_password(db, alice)
        follow_user(db, alice, bob)
        db.notes.insert_many([
            Note("b1", "from bob", bob, "Bob User").dict(),
            Note("c1", "from carol", carol, "Carol User").dict(),
        ])

        login(client, alice)
        resp = client.get("/")
        text = resp.get_data(as_text=True)
        assert "from bob" in text
        assert "from carol" not in text

    def test_feed_only_returns_followed_authors(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")
        carol = make_user(db, "carol")
        follow_user(db, alice, bob)

        db.notes.insert_many([
            Note("b1", "from bob", bob, "Bob User").dict(),
            Note("c1", "from carol", carol, "Carol User").dict(),
            Note("b2", "bob again", bob, "Bob User").dict(),
        ])

        feed = feed_notes(db, alice)
        assert [note["user_id"] for note in feed] == [bob, bob]

    def test_pending_follow_does_not_enter_feed(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob", is_private=True)
        follow_user(db, alice, bob)
        db.notes.insert_one(Note("b1", "private note", bob, "Bob User").dict())
        assert feed_notes(db, alice) == []

    def test_deleted_notes_excluded_from_feed(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")
        follow_user(db, alice, bob)
        active = Note("live", "active", bob, "Bob User")
        deleted = Note("gone", "deleted", bob, "Bob User", deleted=True)
        db.notes.insert_many([active.dict(), deleted.dict()])

        feed = feed_notes(db, alice)
        assert [note["title"] for note in feed] == ["live"]


class TestPermissions:
    def test_self_state_and_follow_self_rejected(self, db):
        alice = make_user(db, "alice")
        assert follow_state(db, alice, alice) == STATE_SELF
        with pytest.raises(FollowError):
            follow_user(db, alice, alice)

    def test_anonymous_profile_and_stats_redirect_to_login(self, client, db):
        bob = make_user(db, "bob")
        resp = client.get("/profile/%s" % bob)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

        stats_resp = client.get("/stats")
        assert stats_resp.status_code == 302
        assert "/login" in stats_resp.headers["Location"]

    def test_follow_self_route_returns_400(self, client, db):
        alice = make_user(db, "alice")
        set_password(db, alice)
        login(client, alice)

        resp = client.post(
            "/follow/%s" % alice,
            headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.status_code == 400
        assert resp.get_json()["state"] == STATE_SELF

    def test_follow_and_unfollow_over_http(self, client, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")
        set_password(db, alice)
        login(client, alice)

        resp = client.post(
            "/follow/%s" % bob,
            headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.status_code == 200
        assert resp.get_json()["state"] == STATE_FOLLOWING

        resp = client.post(
            "/unfollow/%s" % bob,
            headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.status_code == 200
        assert resp.get_json()["state"] == STATE_NOT_FOLLOWING

    def test_private_follow_then_approve_over_http(self, client, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob", is_private=True)
        set_password(db, alice)
        login(client, alice)

        resp = client.post(
            "/follow/%s" % bob,
            headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.get_json()["state"] == STATE_PENDING

        client.get("/logout")
        set_password(db, bob)
        login(client, bob)
        resp = client.post(
            "/approve_follow/%s" % alice,
            headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.get_json()["state"] == STATE_FOLLOWING

    def test_form_fallback_redirects_to_profile(self, client, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")
        set_password(db, alice)
        login(client, alice)

        resp = client.post("/follow/%s" % bob)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/profile/%s" % bob)

    def test_profile_page_shows_counts_and_pending_entry(self, client, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob", is_private=True)
        set_password(db, alice)
        set_password(db, bob)
        login(client, alice)
        client.post("/follow/%s" % bob)

        # Bob's profile view of themselves exposes the pending request.
        client.get("/logout")
        login(client, bob)
        resp = client.get("/profile/%s" % bob)
        text = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "Pending follow requests (1)" in text
        assert "alice User" in text

    def test_follow_button_reflects_relationship_branch(self, client, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")
        private = make_user(db, "gina", is_private=True)
        set_password(db, alice)
        login(client, alice)

        # Not followed -> Follow button visible.
        text = client.get("/profile/%s" % bob).get_data(as_text=True)
        assert 'data-relationship="not_following"' in text

        # Following -> unfollow button visible.
        client.post("/follow/%s" % bob)
        text = client.get("/profile/%s" % bob).get_data(as_text=True)
        assert 'data-relationship="following"' in text

        # Private account -> pending button after the request.
        client.post("/follow/%s" % private)
        text = client.get("/profile/%s" % private).get_data(as_text=True)
        assert 'data-relationship="pending"' in text

        # Own profile -> self branch, no follow controls.
        text = client.get("/profile/%s" % alice).get_data(as_text=True)
        assert "Pending follow requests" in text
        assert "follow-controls" not in text

    def test_stats_page_reports_edge_count(self, client, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob")
        carol = make_user(db, "carol", is_private=True)
        set_password(db, alice)
        login(client, alice)
        follow_user(db, alice, bob)
        follow_user(db, alice, carol)  # pending, not counted

        resp = client.get("/stats")
        text = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "Active follow edges" in text
        assert "Pending follow requests" in text
        assert "<td>1</td>" in text


class TestLegacyData:
    def test_follow_fails_when_follower_field_missing(self, db):
        # Legacy user documents never received the counter fields.
        alice = make_user(db, "alice", with_counters=False)
        bob = make_user(db, "bob")

        with pytest.raises(FollowError):
            follow_user(db, alice, bob)

        # No half-written edge or counter mutation.
        assert get_following(db, alice) == []
        assert counter_pair(db, bob) == (0, 0)

    def test_follow_fails_when_followee_field_missing(self, db):
        alice = make_user(db, "alice")
        bob = make_user(db, "bob", with_counters=False)

        with pytest.raises(FollowError):
            follow_user(db, alice, bob)
        assert get_following(db, alice) == []
        assert counter_pair(db, alice) == (0, 0)

    def test_route_returns_error_status_for_legacy_user(self, client, db):
        alice = make_user(db, "alice", with_counters=False)
        bob = make_user(db, "bob")
        set_password(db, alice)
        login(client, alice)

        resp = client.post(
            "/follow/%s" % bob,
            headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.status_code == 409
        assert "missing counter field" in resp.get_json()["error"]


class TestBackfill:
    def test_projects_embedded_lists_into_follows(self, db):
        alice = make_user(db, "alice", with_counters=False)
        bob = make_user(db, "bob", with_counters=False)
        carol = make_user(db, "carol", with_counters=False)
        db.users.update_one(
            {"id": alice}, {"$set": {"following": [bob, carol]}})

        stats = backfill_module.backfill(db)
        assert stats["edges_added"] == 2
        assert set(get_following(db, alice)) == {bob, carol}

    def test_recovers_edges_from_embedded_followers_list(self, db):
        alice = make_user(db, "alice", with_counters=False)
        bob = make_user(db, "bob", with_counters=False)
        db.users.update_one(
            {"id": bob}, {"$set": {"followers": [alice]}})

        backfill_module.backfill(db)
        assert get_following(db, alice) == [bob]

    def test_repeatable_without_duplicate_edges(self, db):
        alice = make_user(db, "alice", with_counters=False)
        bob = make_user(db, "bob", with_counters=False)
        db.users.update_one(
            {"id": alice}, {"$set": {"following": [bob]}})

        first = backfill_module.backfill(db)
        second = backfill_module.backfill(db)
        assert first["edges_added"] == 1
        assert second["edges_added"] == 0
        assert get_following(db, alice) == [bob]
        assert db.follows.count_documents({}) == 1

    def test_never_touches_counter_fields(self, db):
        alice = make_user(db, "alice", with_counters=False)
        bob = make_user(db, "bob", with_counters=False)
        db.users.update_one(
            {"id": alice}, {"$set": {"following": [bob]}})

        backfill_module.backfill(db)
        backfill_module.backfill(db)
        assert "following_count" not in db.users.find_one({"id": alice})
        assert "followers_count" not in db.users.find_one({"id": alice})
        assert "following_count" not in db.users.find_one({"id": bob})

    def test_dry_run_writes_nothing(self, db):
        alice = make_user(db, "alice", with_counters=False)
        bob = make_user(db, "bob", with_counters=False)
        db.users.update_one(
            {"id": alice}, {"$set": {"following": [bob]}})

        stats = backfill_module.backfill(db, dry_run=True)
        assert stats["edges_added"] == 1
        assert list(db.follows.find({})) == []
