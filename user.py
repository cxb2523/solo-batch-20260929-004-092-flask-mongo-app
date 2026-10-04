from datetime import datetime
import uuid


# User class
class User():
    def __init__(self, title, first_name, last_name, email, id="", verified=False,
                 is_private=False, followers_count=0, following_count=0):
        # Main initialiser
        self.title = title if title != "none" else ""
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.id = uuid.uuid4().hex if not id else id
        self.verified = verified
        self.is_private = is_private
        self.followers_count = followers_count
        self.following_count = following_count

    @classmethod
    def make_from_dict(cls, d):
        # Initialise User object from a dictionary
        # Accesses follow-related fields strictly: legacy documents that are
        # missing them raise KeyError, which callers treat as a failure (run
        # scripts/backfill_follows.py to upgrade old documents).
        return cls(d['title'], d['first_name'], d['last_name'], d['email'], d['id'],
                   d['verified'], d['is_private'], d['followers_count'], d['following_count'])

    def dict(self):
        # Return dictionary representation of the object
        return {
            "id": self.id,
            "title": self.title,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "verified": self.verified,
            "is_private": self.is_private,
            "followers_count": self.followers_count,
            "following_count": self.following_count
        }

    def display_name(self):
        # Return concatenation of name components
        if self.title:
            return self.title + " " + self.first_name + " " + self.last_name
        else:
            return self.first_name + " " + self.last_name

    def profile_name(self):
        # Return URL-friendly slug used by the /profile/<name> route
        return (self.first_name + "-" + self.last_name).lower().replace(" ", "-")

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
