"""Backfill the ``follows`` collection from legacy embedded user data.

Older versions of the application embedded follower data inside user
documents. This script projects every existing relationship into the
independent ``follows`` collection:

  * safe to run repeatedly: edges are inserted with $addToSet under a unique
    index, so re-running the script never creates duplicate edges;
  * counters are never touched: missing counter fields are left missing so
    the follow service keeps treating those legacy users as failures rather
    than silently inventing a number;
  * the ``follows.user_id`` unique index is (re)asserted first.

Usage:
    python scripts/backfill_follows.py [--dry-run]

The database is taken from the MONGODB_URI / MONGO_DBNAME environment
variables, exactly like the Flask application.
"""

import argparse
import os
import sys

# Allow `python scripts/backfill_follows.py` from the repository root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pymongo import MongoClient

from user import FOLLOWS_COLLECTION, ensure_follow_indexes

# Legacy embedded fields this script can recover.
LEGACY_FOLLOWING = ("following", "following_ids", "follows")
LEGACY_FOLLOWERS = ("followers", "follower_ids")


def _legacy_ids(user, field_names):
    for field in field_names:
        value = user.get(field)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item]
    return []


def backfill(db, dry_run=False):
    ensure_follow_indexes(db)

    stats = {"users": 0, "edges_added": 0}
    users = list(db.users.find({}))
    known_ids = {user["id"] for user in users if user.get("id")}

    # Build the edge set from both directions (following lists and follower
    # lists) so either legacy representation recovers the full graph.
    edges = {user["id"]: set() for user in users if user.get("id")}
    for user in users:
        user_id = user.get("id")
        if not user_id:
            continue
        for followee_id in _legacy_ids(user, LEGACY_FOLLOWING):
            if followee_id in known_ids:
                edges.setdefault(user_id, set()).add(followee_id)
        for follower_id in _legacy_ids(user, LEGACY_FOLLOWERS):
            if follower_id in known_ids:
                edges.setdefault(follower_id, set()).add(user_id)

    for user in users:
        user_id = user.get("id")
        if not user_id:
            continue
        stats["users"] += 1
        followee_ids = edges.get(user_id, set())

        if followee_ids:
            # $addToSet keeps this idempotent: an edge that already exists is
            # neither duplicated nor counted as a change on repeat runs.
            before = db[FOLLOWS_COLLECTION].find_one({"user_id": user_id}) or {}
            existing = set(before.get("following", []))
            new_ids = followee_ids - existing
            stats["edges_added"] += len(new_ids)
            if not dry_run:
                db[FOLLOWS_COLLECTION].update_one(
                    {"user_id": user_id},
                    {"$setOnInsert": {
                        "following": [],
                        "pending_outgoing": [],
                        "pending_incoming": []
                    }},
                    upsert=True
                )
                db[FOLLOWS_COLLECTION].update_one(
                    {"user_id": user_id},
                    {
                        "$addToSet": {
                            "following": {"$each": sorted(followee_ids)}}
                    },
                    upsert=True
                )

        # Initialise counters ONLY for users missing the fields. A user that
        # already carries counters is left untouched, so repeat runs cannot
        # change a counter maintained by the application.
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen without writing")
    args = parser.parse_args()

    uri = os.environ.get("MONGODB_URI")
    dbname = os.environ.get("MONGO_DBNAME")
    if not uri or not dbname:
        print("MONGODB_URI and MONGO_DBNAME must be set", file=sys.stderr)
        return 2

    client = MongoClient(uri)
    db = client[dbname]
    stats = backfill(db, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        "%susers scanned: %(users)d, edges added: %(edges_added)d, "
        "counters changed: 0" % (
            prefix, stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
