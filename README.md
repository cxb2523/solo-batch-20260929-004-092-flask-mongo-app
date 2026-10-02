# Template for a basic web app using Python Flask and MongoDB

This is a template for a web app using Flask and MongoDB. It uses Flask-Login for authentication, Flask-Pymongo for the database connection, Flask-Bcrypt for password hashing and Flask-Talisman for security. The front end uses Bootstrap for styling. The app features user verification by email, basic note recording and messaging between users with email notifications.

## How to use the template

Simply insert your MongoDB database URI and database name in the ```configuration.ini``` file. You can also add SMTP server login details to support sending registration emails and message notifications to users.

Run the app using the terminal command: ```python run.py```

You can also use Flask's built-in discovery (the included `wsgi.py` exposes the app):

```
set MONGODB_URI=mongodb://localhost:27017/yourdb
set MONGO_DBNAME=yourdb
flask run
```

## Follow relationships

Users can follow each other. Relationships are stored in a dedicated
`follows` collection (one document per user, with a unique index on
`user_id`) rather than embedded inside user documents: edges are written on
every follow/unfollow and would bloat and contend against the user document,
they are separate entities with their own pending state, and an independent
collection keeps the feed/backfill queries simple. Follow/unfollow are
idempotent thanks to the unique index plus `$addToSet`/`$pull`; denormalised
`following_count`/`followers_count` fields move together with each edge via
`$inc`. Legacy users missing those counter fields cause the operation to fail
loudly instead of silently recreating a counter.

Private accounts receive follow requests first (`pending_outgoing` /
`pending_incoming`), which the account owner approves or denies from their
profile page. The home feed only shows notes from actively followed authors,
the follow button works via AJAX with a regular form fallback, and `/stats`
reports user/note/edge/pending totals.

## Backfill script

Older data with embedded follower lists can be projected into `follows`:

```
set MONGODB_URI=mongodb://localhost:27017/yourdb
set MONGO_DBNAME=yourdb
python scripts/backfill_follows.py          # add --dry-run to preview
```

The script is safe to run repeatedly: it only ever uses `$addToSet`, never
creates duplicate edges, and never modifies counter fields.

## Live demo

The app is live for demonstration purposes at [flask-mongodb-demo.chriswilson.app](https://flask-mongodb-demo.chriswilson.app).
