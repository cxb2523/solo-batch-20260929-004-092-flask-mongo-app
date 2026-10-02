from datetime import datetime
import uuid


# Note class
class Note():

    def __init__(self, title, body, user_id, user_name, id="", deleted=False):
        self.title = title
        self.body = body
        self.user_id = user_id
        self.user_name = user_name
        self.timestamp = datetime.utcnow()
        # Plain day (no zero padding) keeps this portable across platforms;
        # "%-d" is a glibc extension and fails on Windows.
        self.date_string = "%d %s %d" % (
            self.timestamp.day, self.timestamp.strftime("%b"), self.timestamp.year)
        self.id = uuid.uuid4().hex if not id else id
        self.deleted = deleted

    # Return dictionary representation of object
    def dict(self):
        return {
            "title": self.title,
            "body": self.body,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "timestamp": self.timestamp,
            "date_string": self.date_string,
            "id": self.id,
            "deleted": self.deleted
        }


def feed_notes(db, user_id):
    """Home page feed: notes written by users this user actively follows.

    The author list comes from the independent ``follows`` collection (via the
    user service) rather than an embedded array, so the feed reads the same
    source of truth that follow/unfollow writes.
    """
    # Imported here to avoid a circular import at module load time.
    from user import get_following

    author_ids = get_following(db, user_id)
    if not author_ids:
        return []
    return list(
        db.notes.find(
            {"user_id": {"$in": author_ids}, "deleted": False}
        ).sort("timestamp", -1)
    )
