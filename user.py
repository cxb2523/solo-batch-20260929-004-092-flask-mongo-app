from datetime import datetime
import uuid


# Follow graph collection.
#
# Relationships live in their own ``follows`` collection rather than being
# embedded inside user documents:
#   * edges are written on every follow/unfollow, so embedding them in the
#     users collection causes large, ever-growing documents and write
#     contention against the profile/account fields;
#   * a relationship is naturally a separate entity (two endpoints plus a
#     pending state) and needs its own uniqueness guarantee;
#   * an independent collection keeps the queries ("who follows X?", "what
#     is in X's feed?") and the idempotent backfill script straightforward.
#
# Each document holds the outgoing side for one user:
#   {"user_id": id, "following": [ids],
#    "pending_outgoing": [ids], "pending_incoming": [ids]}
# A unique index on ``user_id`` guarantees at most one document per user and,
# together with $addToSet/$pull, makes follow/unfollow idempotent.
FOLLOWS_COLLECTION = "follows"

# Relationship states used by the permission checks in the view layer.
STATE_SELF = "self"
STATE_FOLLOWING = "following"
STATE_PENDING = "pending"
STATE_NOT_FOLLOWING = "not_following"

FOLLOW_STATES = (STATE_SELF, STATE_FOLLOWING, STATE_PENDING, STATE_NOT_FOLLOWING)


class FollowError(Exception):
    """Raised when a follow operation is invalid or cannot stay consistent."""


# User class
class User():
    def __init__(self, title, first_name, last_name, email, id="", verified=False, is_private=False):
        # Main initialiser
        self.title = title if title != "none" else ""
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.id = uuid.uuid4().hex if not id else id
        self.verified = verified
        self.is_private = bool(is_private)

    @classmethod
    def make_from_dict(cls, d):
        # Initialise User object from a dictionary
        return cls(
            d['title'], d['first_name'], d['last_name'], d['email'],
            d['id'], d['verified'], d.get('is_private', False))

    def dict(self):
        # Return dictionary representation of the object
        return {
            "id": self.id,
            "title": self.title,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "verified": self.verified,
            "is_private": self.is_private
        }

    def display_name(self):
        # Return concatenation of name components
        if self.title:
            return self.title + " " + self.first_name + " " + self.last_name
        else:
            return self.first_name + " " + self.last_name

    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return self.id


# Anonymous user class
class Anonymous():

    @property
    def is_authenticated(self):
        return False

    @property
    def is_active(self):
        return False

    @property
    def is_anonymous(self):
        return True

    def get_id(self):
        return None


# ---------------------------------------------------------------------------
# Follow relationship service
#
# Every function takes the PyMongo (or mongomock) database as its first
# argument so the same code serves run.py and scripts/backfill_follows.py.
# ---------------------------------------------------------------------------

def ensure_follow_indexes(db):
    """Create the unique index that protects the follow edge collection."""
    db[FOLLOWS_COLLECTION].create_index("user_id", unique=True)


def _get_edge(db, user_id):
    return db[FOLLOWS_COLLECTION].find_one({"user_id": user_id}) or {}


def _set_add(db, user_id, field, other_id):
    """Idempotently add an id to an edge array. True only if the set changed."""
    # Two steps so the edge document always exists before $addToSet runs.
    # Combining $setOnInsert with $addToSet in a single upsert relies on
    # subtler server semantics; separating them is equivalent in MongoDB and
    # also behaves identically against in-memory test doubles.
    db[FOLLOWS_COLLECTION].update_one(
        {"user_id": user_id},
        {"$setOnInsert": {
            "following": [],
            "pending_outgoing": [],
            "pending_incoming": []
        }},
        upsert=True
    )
    result = db[FOLLOWS_COLLECTION].update_one(
        {"user_id": user_id},
        {"$addToSet": {field: other_id}}
    )
    return result.modified_count == 1


def _set_pull(db, user_id, field, other_id):
    """Remove an id from an edge array. True only if the set changed."""
    result = db[FOLLOWS_COLLECTION].update_one(
        {"user_id": user_id},
        {"$pull": {field: other_id}}
    )
    return result.modified_count == 1


def _inc_counter(db, user_id, field, delta):
    """Adjust a denormalised counter.

    The $exists filter means legacy user documents that were never given the
    counter fields are never silently given a fresh value of 1: the update
    matches nothing and the operation fails instead of diverging the
    counters from the graph.
    """
    result = db.users.update_one(
        {"id": user_id, field: {"$exists": True}},
        {"$inc": {field: delta}}
    )
    if result.matched_count != 1:
        raise FollowError(
            "user %s is missing counter field %r; refusing to create it"
            % (user_id, field))


def _require_counters(db, requirements):
    """Pre-flight check: all counter fields must exist before any edge write.

    Requirements are ``(user_id, field)`` pairs. Failing the check up-front
    (rather than halfway through the $inc pair) guarantees a legacy document
    missing a field aborts the whole operation without leaving the graph and
    the counters half-updated.
    """
    for user_id, field in requirements:
        if db.users.count_documents(
                {"id": user_id, field: {"$exists": True}}) != 1:
            raise FollowError(
                "user %s is missing counter field %r; refusing to create it"
                % (user_id, field))


def get_following(db, user_id):
    """Return the list of users this user actively follows."""
    return list(_get_edge(db, user_id).get("following", []))


def follow_state(db, viewer_id, target_id):
    """One of STATE_SELF / STATE_FOLLOWING / STATE_PENDING / STATE_NOT_FOLLOWING."""
    if not viewer_id:
        return STATE_NOT_FOLLOWING
    if viewer_id == target_id:
        return STATE_SELF
    edge = _get_edge(db, viewer_id)
    if target_id in edge.get("following", []):
        return STATE_FOLLOWING
    if target_id in edge.get("pending_outgoing", []):
        return STATE_PENDING
    return STATE_NOT_FOLLOWING


def follow_user(db, follower_id, followee_id):
    """Create a follow edge, or a pending request when the account is private.

    Idempotent: repeating an active follow or a pending request returns the
    current state without touching counters.
    """
    if follower_id == followee_id:
        raise FollowError("a user cannot follow themselves")

    follower = db.users.find_one({"id": follower_id})
    followee = db.users.find_one({"id": followee_id})
    if follower is None or followee is None:
        raise FollowError("follower or followee does not exist")

    edge = _get_edge(db, follower_id)
    if followee_id in edge.get("following", []):
        return STATE_FOLLOWING
    if followee_id in edge.get("pending_outgoing", []):
        return STATE_PENDING

    if followee.get("is_private", False):
        # Private accounts are not followed until they approve the request;
        # record the pending request on both sides and never touch counters.
        _set_add(db, follower_id, "pending_outgoing", followee_id)
        _set_add(db, followee_id, "pending_incoming", follower_id)
        return STATE_PENDING

    # Public account: write the single directed edge once.
    _require_counters(db, (
        (follower_id, "following_count"),
        (followee_id, "followers_count")))
    if not _set_add(db, follower_id, "following", followee_id):
        return STATE_FOLLOWING
    try:
        # Counters move with the edge write; roll the edge back on failure so
        # the graph and the counters cannot disagree.
        _inc_counter(db, follower_id, "following_count", 1)
        _inc_counter(db, followee_id, "followers_count", 1)
    except FollowError:
        _set_pull(db, follower_id, "following", followee_id)
        raise
    return STATE_FOLLOWING


def unfollow_user(db, follower_id, followee_id):
    """Remove an active edge or a pending request. Idempotent."""
    if follower_id == followee_id:
        raise FollowError("a user cannot unfollow themselves")

    if db.users.find_one({"id": followee_id}) is None:
        raise FollowError("followee does not exist")

    edge = _get_edge(db, follower_id)
    if followee_id in edge.get("pending_outgoing", []):
        # Cancelling a pending request only clears the request state.
        _set_pull(db, follower_id, "pending_outgoing", followee_id)
        _set_pull(db, followee_id, "pending_incoming", follower_id)
        return STATE_NOT_FOLLOWING

    if followee_id not in edge.get("following", []):
        return STATE_NOT_FOLLOWING

    _require_counters(db, (
        (follower_id, "following_count"),
        (followee_id, "followers_count")))
    if not _set_pull(db, follower_id, "following", followee_id):
        return STATE_NOT_FOLLOWING
    try:
        _inc_counter(db, follower_id, "following_count", -1)
        _inc_counter(db, followee_id, "followers_count", -1)
    except FollowError:
        # Restore the edge so a counter failure cannot lose a relationship.
        _set_add(db, follower_id, "following", followee_id)
        raise
    return STATE_NOT_FOLLOWING


def approve_follow(db, approver_id, requester_id):
    """Approve a pending follow request from requester_id. Idempotent."""
    approver_edge = _get_edge(db, approver_id)
    if requester_id not in approver_edge.get("pending_incoming", []):
        return follow_state(db, requester_id, approver_id)

    _set_pull(db, requester_id, "pending_outgoing", approver_id)
    _set_pull(db, approver_id, "pending_incoming", requester_id)
    _require_counters(db, (
        (requester_id, "following_count"),
        (approver_id, "followers_count")))
    if not _set_add(db, requester_id, "following", approver_id):
        return STATE_FOLLOWING
    try:
        _inc_counter(db, requester_id, "following_count", 1)
        _inc_counter(db, approver_id, "followers_count", 1)
    except FollowError:
        _set_pull(db, requester_id, "following", approver_id)
        raise
    return STATE_FOLLOWING


def deny_follow(db, approver_id, requester_id):
    """Reject/remove a pending follow request. Idempotent, no counter change."""
    _set_pull(db, requester_id, "pending_outgoing", approver_id)
    _set_pull(db, approver_id, "pending_incoming", requester_id)
    return STATE_NOT_FOLLOWING


def get_follow_counts(db, user_id):
    """Return following/followers counts from the user document."""
    user = db.users.find_one({"id": user_id}) or {}
    return {
        "following_count": user.get("following_count", 0),
        "followers_count": user.get("followers_count", 0),
        "pending_count": len(_get_edge(db, user_id).get("pending_incoming", []))
    }


def get_pending_followers(db, user_id):
    """Return the user objects waiting for this user's approval."""
    pending_ids = _get_edge(db, user_id).get("pending_incoming", [])
    if not pending_ids:
        return []
    return list(db.users.find({"id": {"$in": pending_ids}}, {"_id": 0}))


def delete_user_relationships(db, user_id):
    """Purge every edge involving user_id when an account is deleted.

    Counter fields of the deleted user disappear with the user document; the
    surviving counterpart users are decremented only when their counter
    fields exist so account deletion never fails on legacy data.
    """
    edge = _get_edge(db, user_id)
    for followee_id in edge.get("following", []):
        _set_pull(db, followee_id, "following", user_id)
        db.users.update_one(
            {"id": followee_id, "followers_count": {"$exists": True}},
            {"$inc": {"followers_count": -1}})
    follower_ids = [
        doc["user_id"]
        for doc in db[FOLLOWS_COLLECTION].find({"following": user_id})
        if doc.get("user_id") != user_id
    ]
    for follower_id in follower_ids:
        _set_pull(db, follower_id, "following", user_id)
        db.users.update_one(
            {"id": follower_id, "following_count": {"$exists": True}},
            {"$inc": {"following_count": -1}})
    for target_id in edge.get("pending_outgoing", []):
        _set_pull(db, target_id, "pending_incoming", user_id)
    for requester_id in edge.get("pending_incoming", []):
        _set_pull(db, requester_id, "pending_outgoing", user_id)
    db[FOLLOWS_COLLECTION].delete_one({"user_id": user_id})
