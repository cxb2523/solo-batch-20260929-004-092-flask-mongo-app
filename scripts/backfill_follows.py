"""Backfill follow data for the flask-mongo-app.

Relationships live in a dedicated `follows` collection (one document per
user: `following`, `followers`, `pending` arrays) instead of being embedded
in the user documents, because:

* follower lists are unbounded - embedding them in user documents would
  eventually hit MongoDB's 16MB document limit for popular accounts;
* every follow/unfollow would rewrite the whole user document, creating
  write contention on hot documents;
* the relationship needs its own unique index (on user_id) to guarantee
  idempotent writes, which is cleaner on a dedicated collection.

This script is idempotent: it can be run repeatedly without creating
duplicate edges and without modifying existing counters. It:

1. creates the unique index on follows.user_id;
2. creates a follows document (empty arrays) for every user that lacks one;
3. de-duplicates the arrays of existing follows documents (only rewriting
   documents that actually change);
4. fills in missing user fields (is_private, followers_count,
   following_count) with defaults, never touching values that already exist.

Usage:
    MONGODB_URI=mongodb://localhost:27017/mydb MONGO_DBNAME=mydb \
        python scripts/backfill_follows.py
"""

import os
import sys

from pymongo import MongoClient

ARRAY_FIELDS = ("following", "followers", "pending")
USER_DEFAULTS = {
    "is_private": False,
    "followers_count": 0,
    "following_count": 0,
}


def backfill(db):
    """Run the backfill against a pymongo database. Returns a summary dict."""
    summary = {
        "follows_docs_created": 0,
        "follows_docs_deduplicated": 0,
        "user_fields_filled": 0,
    }

    # 1. Unique index: one follows document per user
    db.follows.create_index([("user_id", 1)], unique=True)

    # 2. Ensure every user has a follows document ($setOnInsert = no dupes)
    for user in db.users.find({}, {"id": 1}):
        result = db.follows.update_one(
            {"user_id": user["id"]},
            {"$setOnInsert": {"user_id": user["id"], "following": [],
                              "followers": [], "pending": []}},
            upsert=True)
        if result.upserted_id is not None:
            summary["follows_docs_created"] += 1

    # 3. De-duplicate arrays in existing follows documents; also drop
    # self-edges. Only documents that change are rewritten.
    for doc in db.follows.find():
        changed = False
        updates = {}
        for field in ARRAY_FIELDS:
            values = doc.get(field, [])
            cleaned = []
            for value in values:
                if value == doc.get("user_id"):
                    changed = True
                    continue
                if value not in cleaned:
                    cleaned.append(value)
                else:
                    changed = True
            if cleaned != values:
                updates[field] = cleaned
        if changed:
            db.follows.update_one({"_id": doc["_id"]}, {"$set": updates})
            summary["follows_docs_deduplicated"] += 1

    # 4. Fill missing user fields with defaults. Existing values (including
    # existing counters) are never modified.
    for field, default in USER_DEFAULTS.items():
        result = db.users.update_many(
            {field: {"$exists": False}}, {"$set": {field: default}})
        summary["user_fields_filled"] += result.modified_count

    return summary


def main():
    mongo_uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
    db_name = os.environ.get("MONGO_DBNAME")
    if not db_name:
        print("Error: set the MONGO_DBNAME environment variable")
        sys.exit(1)
    db = MongoClient(mongo_uri)[db_name]
    summary = backfill(db)
    print("Backfill complete:")
    for key, value in summary.items():
        print("  {}: {}".format(key, value))


if __name__ == "__main__":
    main()
