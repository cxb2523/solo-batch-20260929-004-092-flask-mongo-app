# Template for a basic web app using Python Flask and MongoDB

This is a template for a web app using Flask and MongoDB. It uses Flask-Login for authentication, Flask-Pymongo for the database connection, Flask-Bcrypt for password hashing and Flask-Talisman for security. The front end uses Bootstrap for styling. The app features user verification by email, basic note recording and messaging between users with email notifications.

## How to use the template

Simply insert your MongoDB database URI and database name in the ```configuration.ini``` file. You can also add SMTP server login details to support sending registration emails and message notifications to users.

Run the app using the terminal command: ```python run.py```

## Live demo

The app is live for demonstration purposes at [flask-mongodb-demo.chriswilson.app](https://flask-mongodb-demo.chriswilson.app).

## Follow relationships

Users can follow each other. Relationships are stored in a dedicated `follows`
collection (one document per user, holding `following`, `followers` and
`pending` arrays, with a unique index on `user_id`) instead of being embedded
in the user documents, because:

- follower lists are unbounded, so embedding them in user documents would
  eventually hit MongoDB's 16MB document limit for popular accounts;
- every follow/unfollow would rewrite the whole user document, creating write
  contention on hot user documents;
- the relationship gets its own unique index, which makes follow/unfollow
  idempotent (`$addToSet`/`$pull`) without touching the users collection.

Counters (`followers_count`, `following_count`) live on the user documents and
are updated with `$inc` only when the relationship write actually changed
something, so retries never double-count. Private accounts receive follow
requests into a `pending` list and approve or decline them from their profile
page. The home page feed only shows notes from authors the current user
follows, and `/stats` shows site-wide follow statistics.

Existing databases must be upgraded once with the idempotent backfill script
(safe to run repeatedly; it never duplicates edges and never modifies
existing counters):

```
MONGODB_URI=mongodb://localhost:27017/mydb MONGO_DBNAME=mydb python scripts/backfill_follows.py
```

User documents that predate the follow feature (missing `is_private`,
`followers_count` or `following_count`) cause follow operations to fail
loudly until the backfill has been run.

## Tests

```
python -m pytest -q tests/test_follow.py
```
