# MODULE IMPORTS

# Flask modules
from flask import Flask, render_template, request, url_for, request, redirect, abort, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_talisman import Talisman
from flask_pymongo import PyMongo
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect

# Other modules
from urllib.parse import urlparse, urljoin
from datetime import datetime
import configparser
import json
import sys
import os

# Local imports
from user import User, Anonymous
from message import Message
from note import Note
from email_utility import send_email, send_registration_email, send_message_email
from verification import confirm_token


# Create app
app = Flask(__name__)

# Configuration
config = configparser.ConfigParser()
config.read('configuration.ini')
default = config['DEFAULT']
app.secret_key = default['SECRET_KEY']
app.config['MONGO_DBNAME'] = os.environ.get('MONGO_DBNAME')
app.config['MONGO_URI'] = os.environ.get('MONGODB_URI') #default['MONGO_URI']
app.config['PREFERRED_URL_SCHEME'] = "https"

# Create Pymongo
mongo = PyMongo(app)

# Create Bcrypt
bc = Bcrypt(app)

# Create Talisman
csp = {
    'default-src': [
        '\'self\'',
        'https://stackpath.bootstrapcdn.com',
        'https://pro.fontawesome.com',
        'https://code.jquery.com',
        'https://cdnjs.cloudflare.com'
    ]
}
talisman = Talisman(app, content_security_policy=csp)

# Create CSRF protect
csrf = CSRFProtect()
csrf.init_app(app)

# Create login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.anonymous_user = Anonymous
login_manager.login_view = "login"


# FOLLOW RELATIONSHIPS

# Follow relationships live in a dedicated `follows` collection (one document
# per user: following/followers/pending arrays, unique index on user_id)
# rather than embedded in the user documents. See README.md for the rationale.

FOLLOW_NONE = "none"
FOLLOW_ACTIVE = "active"
FOLLOW_PENDING = "pending"

# Fields the follow feature requires on every user document. Legacy documents
# missing any of these cause operations to fail loudly (LegacyDataError) so
# the backfill script gets run instead of silently corrupting counts.
REQUIRED_USER_FIELDS = ("is_private", "followers_count", "following_count")


class FollowError(Exception):
    pass


class LegacyDataError(FollowError):
    pass


def ensure_follow_indexes():
    # Unique index guarantees one follows document per user
    mongo.db.follows.create_index([("user_id", 1)], unique=True)


def get_or_create_follows(user_id):
    # $setOnInsert + upsert makes this safe to call repeatedly
    mongo.db.follows.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"user_id": user_id, "following": [], "followers": [], "pending": []}},
        upsert=True)
    return mongo.db.follows.find_one({"user_id": user_id})


def require_current_user_fields(user_doc):
    if user_doc is None:
        raise FollowError("User not found")
    missing = [field for field in REQUIRED_USER_FIELDS if field not in user_doc]
    if missing:
        raise LegacyDataError(
            "User document is missing fields {} - run scripts/backfill_follows.py".format(missing))


def follow_user(actor_id, target_id):
    if actor_id == target_id:
        raise FollowError("Users cannot follow themselves")
    actor = mongo.db.users.find_one({"id": actor_id})
    target = mongo.db.users.find_one({"id": target_id})
    require_current_user_fields(actor)
    require_current_user_fields(target)
    ensure_follow_indexes()
    get_or_create_follows(actor_id)
    target_follows = get_or_create_follows(target_id)
    # Idempotent: already following is a no-op
    if actor_id in target_follows.get("followers", []):
        return FOLLOW_ACTIVE
    if target["is_private"]:
        # Private account: park a pending request for the owner to approve
        mongo.db.follows.update_one(
            {"user_id": target_id}, {"$addToSet": {"pending": actor_id}})
        return FOLLOW_PENDING
    actor_result = mongo.db.follows.update_one(
        {"user_id": actor_id}, {"$addToSet": {"following": target_id}})
    target_result = mongo.db.follows.update_one(
        {"user_id": target_id}, {"$addToSet": {"followers": actor_id}})
    # Counts only move when the relationship write actually changed something
    if actor_result.modified_count:
        mongo.db.users.update_one({"id": actor_id}, {"$inc": {"following_count": 1}})
    if target_result.modified_count:
        mongo.db.users.update_one({"id": target_id}, {"$inc": {"followers_count": 1}})
    return FOLLOW_ACTIVE


def unfollow_user(actor_id, target_id):
    if actor_id == target_id:
        raise FollowError("Users cannot unfollow themselves")
    actor = mongo.db.users.find_one({"id": actor_id})
    target = mongo.db.users.find_one({"id": target_id})
    require_current_user_fields(actor)
    require_current_user_fields(target)
    ensure_follow_indexes()
    get_or_create_follows(actor_id)
    get_or_create_follows(target_id)
    actor_result = mongo.db.follows.update_one(
        {"user_id": actor_id}, {"$pull": {"following": target_id}})
    target_result = mongo.db.follows.update_one(
        {"user_id": target_id}, {"$pull": {"followers": actor_id}})
    if actor_result.modified_count:
        mongo.db.users.update_one({"id": actor_id}, {"$inc": {"following_count": -1}})
    if target_result.modified_count:
        mongo.db.users.update_one({"id": target_id}, {"$inc": {"followers_count": -1}})
    # Also cancel any pending request (no count impact)
    mongo.db.follows.update_one(
        {"user_id": target_id}, {"$pull": {"pending": actor_id}})
    return FOLLOW_NONE


def approve_follower(owner_id, requester_id):
    owner = mongo.db.users.find_one({"id": owner_id})
    requester = mongo.db.users.find_one({"id": requester_id})
    require_current_user_fields(owner)
    require_current_user_fields(requester)
    ensure_follow_indexes()
    get_or_create_follows(requester_id)
    owner_follows = get_or_create_follows(owner_id)
    pulled = mongo.db.follows.update_one(
        {"user_id": owner_id}, {"$pull": {"pending": requester_id}})
    if not pulled.modified_count:
        # Idempotent: already an approved follower is a no-op
        if requester_id in owner_follows.get("followers", []):
            return FOLLOW_ACTIVE
        raise FollowError("No pending follow request from this user")
    owner_result = mongo.db.follows.update_one(
        {"user_id": owner_id}, {"$addToSet": {"followers": requester_id}})
    requester_result = mongo.db.follows.update_one(
        {"user_id": requester_id}, {"$addToSet": {"following": owner_id}})
    if owner_result.modified_count:
        mongo.db.users.update_one({"id": owner_id}, {"$inc": {"followers_count": 1}})
    if requester_result.modified_count:
        mongo.db.users.update_one({"id": requester_id}, {"$inc": {"following_count": 1}})
    return FOLLOW_ACTIVE


def decline_follower(owner_id, requester_id):
    owner = mongo.db.users.find_one({"id": owner_id})
    require_current_user_fields(owner)
    ensure_follow_indexes()
    get_or_create_follows(owner_id)
    mongo.db.follows.update_one(
        {"user_id": owner_id}, {"$pull": {"pending": requester_id}})
    return FOLLOW_NONE


def follow_state(viewer_id, target_id):
    # Four branches: self / already following / pending approval / none
    if viewer_id is not None and viewer_id == target_id:
        return "self"
    follows = mongo.db.follows.find_one({"user_id": target_id}) or {}
    if viewer_id in follows.get("followers", []):
        return FOLLOW_ACTIVE
    if viewer_id in follows.get("pending", []):
        return FOLLOW_PENDING
    return FOLLOW_NONE


def wants_json():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or \
        request.accept_mimetypes.best == "application/json"


def follow_response(user_id, status=None, error=None, code=200):
    target = mongo.db.users.find_one({"id": user_id}) or {}
    if wants_json():
        return jsonify({
            "user_id": user_id,
            "status": status,
            "followers_count": target.get("followers_count"),
            "error": error
        }), code
    if error:
        return "Error: " + error, code
    # Form fallback: plain POST redirects back to the page it came from
    return redirect(request.referrer or url_for('public_profile', name=user_id))


# ROUTES

# Index
@app.route('/')
def index():
    feed = None
    if current_user.is_authenticated:
        feed = Note.feed_for_user(mongo.db, current_user.id)
    return render_template('index.html', feed=feed)


# Login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if current_user.is_authenticated:
            # Redirect to index if already authenticated
            return redirect(url_for('/index'))
        # Render login page
        return render_template('login.html', error=request.args.get("error"))
    # Retrieve user from database
    users = mongo.db.users
    user_data = users.find_one({'email': request.form['email']}, {'_id': 0})
    if user_data:
        # Check password hash
        if bc.check_password_hash(user_data['password'], request.form['pass']):
            # Create user object to login (note password hash not stored in session)
            user = User.make_from_dict(user_data)
            login_user(user)

            # Check for next argument (direct user to protected page they wanted)
            next = request.args.get('next')
            if not is_safe_url(next):
                return abort(400)

            # Go to profile page after login
            return redirect(next or url_for('profile'))

    # Redirect to login page on error
    return redirect(url_for('login', error=1))


# Register
@app.route('/register', methods=['POST', 'GET'])
def register():
    if request.method == 'POST':
        # Trim input data
        email = request.form['email'].strip()
        title = request.form['title'].strip()
        first_name = request.form['first_name'].strip()
        last_name = request.form['last_name'].strip()
        password = request.form['pass'].strip()

        users = mongo.db.users
        # Check if email address already exists
        existing_user = users.find_one(
            {'email': email}, {'_id': 0})

        if existing_user is None:
            logout_user()
            # Hash password
            hashpass = bc.generate_password_hash(password).decode('utf-8')
            # Create user object (note password hash not stored in session)
            new_user = User(title, first_name, last_name, email)
            # Create dictionary data to save to database
            user_data_to_save = new_user.dict()
            user_data_to_save['password'] = hashpass

            # Insert user record to database
            if users.insert_one(user_data_to_save):
                login_user(new_user)
                send_registration_email(new_user)
                return redirect(url_for('profile'))
            else:
                # Handle database error
                return redirect(url_for('register', error=2))

        # Handle duplicate email
        return redirect(url_for('register', error=1))

    # Return template for registration page if GET request
    return render_template('register.html', error=request.args.get("error"))


# Confirm email
@app.route('/confirm/<token>', methods=['GET'])
def confirm_email(token):
    logout_user()
    try:
        email = confirm_token(token)
        if email:
            if mongo.db.users.update_one({"email": email}, {"$set": {"verified": True}}):
                return render_template('confirm.html', success=True)
    except:
        return render_template('confirm.html', success=False)
    else:
        return render_template('confirm.html', success=False)


# Verification email
@app.route('/verify', methods=['POST'])
@login_required
def send_verification_email():
    if current_user.verified == False:
        send_registration_email(current_user)
        return "Verification email sent"
    else:
        return "Your email address is already verified"


# Profile
@app.route('/profile', methods=['GET'])
@login_required
def profile():
    notes = mongo.db.notes.find(
        {"user_id": current_user.id, "deleted": False}).sort("timestamp", -1)
    follows = get_or_create_follows(current_user.id)
    pending_users = []
    for requester_id in follows.get("pending", []):
        requester = mongo.db.users.find_one({"id": requester_id})
        if requester:
            pending_users.append(requester)
    return render_template('profile.html', notes=notes, title=current_user.title,
                           pending_users=pending_users)


# Public profile
@app.route('/profile/<name>', methods=['GET'])
def public_profile(name):
    user_data = mongo.db.users.find_one({"id": name})
    if user_data is None:
        for candidate in mongo.db.users.find():
            try:
                if User.make_from_dict(candidate).profile_name() == name:
                    user_data = candidate
                    break
            except KeyError:
                continue
    if user_data is None:
        abort(404)
    try:
        profile_user = User.make_from_dict(user_data)
    except KeyError:
        # Legacy document missing follow fields
        abort(500)
    viewer_id = current_user.get_id()
    state = follow_state(viewer_id, profile_user.id)
    can_view_notes = (not profile_user.is_private) or state in (FOLLOW_ACTIVE, "self")
    notes = []
    if can_view_notes:
        notes = mongo.db.notes.find(
            {"user_id": profile_user.id, "deleted": False}).sort("timestamp", -1)
    return render_template('public_profile.html', profile_user=profile_user,
                           follow_state=state, notes=notes)


# Stats
@app.route('/stats', methods=['GET'])
def stats():
    users_count = mongo.db.users.count_documents({})
    notes_count = mongo.db.notes.count_documents({"deleted": False})
    messages_count = mongo.db.messages.count_documents({"deleted": False})
    follow_edges = 0
    pending_count = 0
    leaderboard = []
    for doc in mongo.db.follows.find():
        follow_edges += len(doc.get("following", []))
        pending_count += len(doc.get("pending", []))
        leaderboard.append((doc.get("user_id"), len(doc.get("followers", []))))
    leaderboard.sort(key=lambda entry: entry[1], reverse=True)
    top_followed = []
    for user_id, followers in leaderboard[:5]:
        user_data = mongo.db.users.find_one({"id": user_id})
        if user_data:
            top_followed.append({
                "name": user_data["first_name"] + " " + user_data["last_name"],
                "followers": followers
            })
    return render_template('stats.html', users_count=users_count,
                           notes_count=notes_count, messages_count=messages_count,
                           follow_edges=follow_edges, pending_count=pending_count,
                           top_followed=top_followed)


# Messages
@app.route('/messages', methods=['GET'])
@login_required
def messages():
    all_users = mongo.db.users.find(
        {"id": {"$ne": current_user.id}}, {'_id': 0})
    inbox_messages = mongo.db.messages.find(
        {"to_id": current_user.id, "deleted": False}).sort("timestamp", -1)
    sent_messages = mongo.db.messages.find(
        {"from_id": current_user.id, "deleted": False, "hidden_for_sender": False}).sort("timestamp", -1)
    return render_template('messages.html', users=all_users, inbox_messages=inbox_messages, sent_messages=sent_messages)


# Logout
@app.route('/logout', methods=['GET'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# POST REQUEST ROUTES

# Follow a user
@app.route('/follow/<user_id>', methods=['POST'])
@login_required
def follow(user_id):
    try:
        status = follow_user(current_user.id, user_id)
    except LegacyDataError as error:
        return follow_response(user_id, error=str(error), code=500)
    except FollowError as error:
        return follow_response(user_id, error=str(error), code=400)
    return follow_response(user_id, status=status)


# Unfollow a user (also cancels a pending request)
@app.route('/unfollow/<user_id>', methods=['POST'])
@login_required
def unfollow(user_id):
    try:
        status = unfollow_user(current_user.id, user_id)
    except LegacyDataError as error:
        return follow_response(user_id, error=str(error), code=500)
    except FollowError as error:
        return follow_response(user_id, error=str(error), code=400)
    return follow_response(user_id, status=status)


# Approve a pending follow request (private accounts)
@app.route('/follow_requests/approve/<requester_id>', methods=['POST'])
@login_required
def approve_follow_request(requester_id):
    try:
        status = approve_follower(current_user.id, requester_id)
    except LegacyDataError as error:
        return follow_response(requester_id, error=str(error), code=500)
    except FollowError as error:
        return follow_response(requester_id, error=str(error), code=400)
    return follow_response(requester_id, status=status)


# Decline a pending follow request
@app.route('/follow_requests/decline/<requester_id>', methods=['POST'])
@login_required
def decline_follow_request(requester_id):
    try:
        status = decline_follower(current_user.id, requester_id)
    except LegacyDataError as error:
        return follow_response(requester_id, error=str(error), code=500)
    except FollowError as error:
        return follow_response(requester_id, error=str(error), code=400)
    return follow_response(requester_id, status=status)


# Add note
@app.route('/add_note', methods=['POST'])
@login_required
def add_note():
    title = request.form.get("title")
    body = request.form.get("body")
    user_id = current_user.id
    user_name = current_user.display_name()
    note = Note(title, body, user_id, user_name)
    if mongo.db.notes.insert_one(note.dict()):
        return "Success! Note added: " + title
    else:
        return "Error! Could not add note"


# Delete note
@app.route('/delete_note', methods=['POST'])
@login_required
def delete_note():
    note_id = request.form.get("note_id")
    if mongo.db.notes.update_one({"id": note_id}, {"$set": {"deleted": True}}):
        return "Success! Note deleted"
    else:
        return "Error! Could not delete note"


# Send message
@app.route('/send_message', methods=['POST'])
@login_required
def send_message():
    title = request.form.get("title")
    body = request.form.get("body")
    from_id = current_user.id
    from_name = current_user.display_name()
    to_id = request.form.get("user")
    to_user_dict = mongo.db.users.find_one({"id": to_id})
    to_user = User.make_from_dict(to_user_dict)
    to_name = to_user.display_name()
    message = Message(title, body, from_id, from_name, to_id, to_name)
    if mongo.db.messages.insert_one(message.dict()):
        send_message_email(from_user=current_user,
                           to_user=to_user, message=message)
        return "Success! Message sent to " + to_name + ": " + title
    else:
        return "Error! Could not send message"


# Delete message
@app.route('/delete_message', methods=['POST'])
@login_required
def delete_message():
    message_id = request.form.get("message_id")
    if mongo.db.messages.update_one({"id": message_id}, {"$set": {"deleted": True}}):
        return "Success! Message deleted"
    else:
        return "Error! Could not delete message"


# Hide sent message
@app.route('/hide_sent_message', methods=['POST'])
@login_required
def hide_sent_message():
    message_id = request.form.get("message_id")
    if mongo.db.messages.update_one({"id": message_id}, {"$set": {"hidden_for_sender": True}}):
        return "Success! Message hidden from sender"
    else:
        return "Error! Could not hide message"


# Change Name
@app.route('/change_name', methods=['POST'])
@login_required
def change_name():
    title = request.form['title'].strip()
    first_name = request.form['first_name'].strip()
    last_name = request.form['last_name'].strip()

    if mongo.db.users.update_one({"email": current_user.email}, {"$set": {"title": title, "first_name": first_name, "last_name": last_name}}):
        return "User name updated successfully"
    else:
        return "Error! Could not update user name"


# Delete Account
@app.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    user_id = current_user.id

    # Deletion flags
    user_deleted = False
    notes_deleted = False
    messages_deleted = False

    # Delete user details
    if mongo.db.users.delete_one({"id": user_id}):
        user_deleted = True
        logout_user()

    # Delete notes
    if mongo.db.notes.delete_many({"user_id": user_id}):
        notes_deleted = True

    # Delete messages
    if mongo.db.messages.delete_many({"$or": [{"from_id": user_id}, {"to_id": user_id}]}):
        messages_deleted = True

    return {"user_deleted": user_deleted, "notes_deleted": notes_deleted, "messages_deleted": messages_deleted}


# LOGIN MANAGER REQUIREMENTS

# Load user from user ID
@login_manager.user_loader
def load_user(userid):
    # Return user object or none
    users = mongo.db.users
    user = users.find_one({'id': userid}, {'_id': 0})
    if user:
        return User.make_from_dict(user)
    return None


# Safe URL
def is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and \
        ref_url.netloc == test_url.netloc


if __name__ == '__main__':
    # Heroku environment
    if os.environ.get('APP_LOCATION') == 'heroku':
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
    else:
        app.run(host='localhost', port=8080, debug=True)
