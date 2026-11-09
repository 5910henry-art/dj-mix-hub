import os
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from functools import wraps

from flask import (
    Flask, render_template, jsonify, request,
    send_from_directory, redirect, url_for,
    flash, abort, session
)
from flask_login import (
    LoginManager, login_user, login_required,
    logout_user, current_user
)
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import or_
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename, safe_join

from config import Config
from extensions import db

login_manager = LoginManager()
csrf = CSRFProtect()


@login_manager.user_loader
def load_user(user_id):
    from models import DJ
    try:
        return db.session.get(DJ, int(user_id))
    except (TypeError, ValueError):
        return None


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped



def dj_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("dj_login"))

        if current_user.is_admin:
            return redirect(url_for("admin_dashboard"))

        if not current_user.is_approved:
            logout_user()
            flash("Your DJ account is suspended. Please contact the administrator.")
            return redirect(url_for("dj_login"))

        return view(*args, **kwargs)
    return wrapped


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    app.config["UPLOAD_FOLDER"] = os.environ.get(
        "UPLOAD_FOLDER",
        os.path.join(app.root_path, "uploads")
    )
    app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Load the private VAPID key from the protected local file
    # when it has not been supplied through the environment.
    if not app.config.get("VAPID_PRIVATE_KEY"):
        key_path = app.config.get("VAPID_PRIVATE_KEY_PATH")
        if key_path and os.path.isfile(key_path):
            try:
                with open(key_path, "r", encoding="utf-8") as key_file:
                    app.config["VAPID_PRIVATE_KEY"] = key_file.read().strip()
                app.logger.info("VAPID private key loaded from configured file.")
            except OSError:
                app.logger.exception("Could not read configured VAPID key file.")

    def send_mix_published_notification(mix):
        """Send a push notification to all subscribed browsers."""
        import json

        from models import PushSubscription

        private_key = app.config.get("VAPID_PRIVATE_KEY")
        subject = app.config.get("VAPID_SUBJECT")

        if not private_key or not subject:
            app.logger.warning(
                "Push notification skipped: VAPID configuration is incomplete."
            )
            return

        try:
            from pywebpush import webpush, WebPushException
        except Exception:
            app.logger.exception("Could not import the Web Push library.")
            return

        try:
            subscriptions = PushSubscription.query.all()
        except Exception:
            db.session.rollback()
            app.logger.exception(
                "Could not load push subscriptions; mix publication remains successful."
            )
            return

        expired_ids = []

        dj_name = "A DJ"
        try:
            if mix.dj:
                dj_name = mix.dj.display_name or mix.dj.username or dj_name
        except Exception:
            app.logger.exception("Could not read DJ details for push notification.")

        payload = {
            "title": "New mix on DJ MIX HUB",
            "body": f"{mix.title} by {dj_name} is now available.",
            "url": url_for("mix_detail", mix_id=mix.id)
        }

        sent = 0
        failed = 0

        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info={
                        "endpoint": subscription.endpoint,
                        "keys": {
                            "p256dh": subscription.p256dh,
                            "auth": subscription.auth
                        }
                    },
                    data=json.dumps(payload),
                    vapid_private_key=private_key,
                    vapid_claims={"sub": subject}
                )
                sent += 1

            except WebPushException as exc:
                failed += 1
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)

                if status_code in (404, 410):
                    expired_ids.append(subscription.id)

                app.logger.warning(
                    "Push delivery failed for subscription %s (HTTP %s).",
                    subscription.id,
                    status_code or "unknown"
                )

            except Exception:
                failed += 1
                app.logger.exception(
                    "Unexpected push delivery failure for subscription %s.",
                    subscription.id
                )

        if expired_ids:
            try:
                PushSubscription.query.filter(
                    PushSubscription.id.in_(expired_ids)
                ).delete(synchronize_session=False)
                db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception("Could not remove expired push subscriptions.")

        app.logger.info(
            "Mix publication push completed: %s sent, %s failed, %s expired.",
            sent,
            failed,
            len(expired_ids)
        )

    def send_booking_created_notification(booking):
        """Notify the selected DJ and subscribed admins about a new booking."""
        import json

        from models import DJ, PushSubscription

        private_key = app.config.get("VAPID_PRIVATE_KEY")
        subject = app.config.get("VAPID_SUBJECT")

        if not private_key or not subject:
            app.logger.warning("Booking push skipped: VAPID configuration is incomplete.")
            return

        try:
            from pywebpush import webpush, WebPushException
        except Exception:
            app.logger.exception("Could not import Web Push library for booking notification.")
            return

        try:
            recipients = {}

            if booking.dj_id:
                selected_dj = db.session.get(DJ, booking.dj_id)
                dj_subscriptions = PushSubscription.query.filter_by(
                    dj_id=booking.dj_id
                ).all()

                target = (
                    url_for("admin_manage_bookings")
                    if selected_dj and selected_dj.is_admin
                    else url_for("dj_manage_bookings")
                )

                for subscription in dj_subscriptions:
                    recipients[subscription.id] = (subscription, target)

            admin_subscriptions = (
                PushSubscription.query
                .join(DJ, PushSubscription.dj_id == DJ.id)
                .filter(DJ.is_admin.is_(True))
                .all()
            )

            for subscription in admin_subscriptions:
                recipients.setdefault(
                    subscription.id,
                    (subscription, url_for("admin_manage_bookings"))
                )

        except Exception:
            db.session.rollback()
            app.logger.exception("Could not load booking notification recipients.")
            return

        payload_base = {
            "title": "New booking request",
            "body": (
                f"{booking.client_name} requested {booking.event_type} "
                f"on {booking.event_date.isoformat()}."
            )
        }

        expired_ids = []
        sent = 0
        failed = 0

        for subscription, target_url in recipients.values():
            payload = {**payload_base, "url": target_url}

            try:
                webpush(
                    subscription_info={
                        "endpoint": subscription.endpoint,
                        "keys": {
                            "p256dh": subscription.p256dh,
                            "auth": subscription.auth
                        }
                    },
                    data=json.dumps(payload),
                    vapid_private_key=private_key,
                    vapid_claims={"sub": subject}
                )
                sent += 1

            except WebPushException as exc:
                failed += 1
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)

                if status_code in (404, 410):
                    expired_ids.append(subscription.id)

                app.logger.warning(
                    "Booking push failed for subscription %s (HTTP %s).",
                    subscription.id,
                    status_code or "unknown"
                )

            except Exception:
                failed += 1
                app.logger.exception(
                    "Unexpected booking push failure for subscription %s.",
                    subscription.id
                )

        if expired_ids:
            try:
                PushSubscription.query.filter(
                    PushSubscription.id.in_(expired_ids)
                ).delete(synchronize_session=False)
                db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception("Could not remove expired booking push subscriptions.")

        app.logger.info(
            "Booking push completed: %s sent, %s failed, %s expired.",
            sent, failed, len(expired_ids)
        )

    login_manager.login_view = "admin_login"
    login_manager.login_message = "Please log in first."


    @app.route("/account/login")
    def account_login_choice():
        return render_template("account_choice.html", mode="login")


    @app.route("/account/register")
    def account_register_choice():
        return render_template("account_choice.html", mode="register")


    @app.route("/listener/register", methods=["GET", "POST"])
    def listener_register():
        from models import Listener
        from sqlalchemy.exc import IntegrityError

        if session.get("listener_id"):
            return redirect(url_for("home"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            display_name = request.form.get("display_name", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not username or not email or not password:
                flash("Please complete all required fields.")
            elif len(username) > 80:
                flash("Username must not exceed 80 characters.")
            elif len(email) > 150 or "@" not in email or "." not in email.split("@")[-1]:
                flash("Please enter a valid email address.")
            elif len(display_name) > 120:
                flash("Display name must not exceed 120 characters.")
            elif len(password) < 8:
                flash("Password must contain at least 8 characters.")
            elif password != confirm_password:
                flash("Your passwords do not match.")
            elif Listener.query.filter(
                or_(
                    Listener.username.ilike(username),
                    Listener.email.ilike(email)
                )
            ).first():
                flash("That username or email is already registered.")
            else:
                listener = Listener(
                    username=username,
                    email=email,
                    display_name=display_name or username,
                    password_hash=generate_password_hash(password)
                )

                try:
                    db.session.add(listener)
                    db.session.commit()
                    session["listener_id"] = listener.id
                    flash("Your listener account has been created.")
                    return redirect(url_for("home"))
                except IntegrityError:
                    db.session.rollback()
                    flash("That username or email is already registered.")
                except Exception:
                    db.session.rollback()
                    app.logger.exception("Listener registration failed.")
                    flash("Registration failed. Please try again.")

        return render_template("listener/register.html")


    @app.route("/listener/login", methods=["GET", "POST"])
    def listener_login():
        from models import Listener

        if session.get("listener_id"):
            return redirect(url_for("home"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            listener = Listener.query.filter(
                or_(
                    Listener.username.ilike(username),
                    Listener.email.ilike(username)
                )
            ).first()

            if listener and check_password_hash(
                listener.password_hash, password
            ):
                session["listener_id"] = listener.id
                flash("Welcome back!")
                return redirect(url_for("home"))

            flash("Invalid username/email or password.")

        return render_template("listener/login.html")


    @app.route("/listener/profile")
    def listener_profile():
        from models import Listener

        listener_id = session.get("listener_id")
        if not listener_id:
            flash("Please log in to view your account.")
            return redirect(url_for("listener_login"))

        listener = db.session.get(Listener, listener_id)
        if listener is None:
            session.pop("listener_id", None)
            flash("Please log in again.")
            return redirect(url_for("listener_login"))

        return render_template(
            "listener/profile.html",
            listener=listener
        )


    @app.route("/listener/profile/edit", methods=["GET", "POST"])
    def listener_edit_profile():
        from models import Listener
        from sqlalchemy.exc import IntegrityError
        from audio_storage import (
            filebase_enabled, upload_image, delete_image
        )

        listener_id = session.get("listener_id")
        if not listener_id:
            flash("Please log in to edit your profile.")
            return redirect(url_for("listener_login"))

        listener = db.session.get(Listener, listener_id)
        if listener is None:
            session.pop("listener_id", None)
            flash("Please log in again.")
            return redirect(url_for("listener_login"))

        form_data = request.form if request.method == "POST" else None

        if request.method == "POST":
            display_name = request.form.get("display_name", "").strip()
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            bio = request.form.get("bio", "").strip()

            if not username or not email:
                flash("Username and email are required.")
                return render_template(
                    "listener/edit_profile.html",
                    listener=listener,
                    form_data=form_data
                )

            if len(username) > 80:
                flash("Username must not exceed 80 characters.")
            elif (
                len(email) > 150
                or "@" not in email
                or "." not in email.split("@")[-1]
            ):
                flash("Please enter a valid email address.")
            elif len(display_name) > 120:
                flash("Display name must not exceed 120 characters.")
            elif len(bio) > 1000:
                flash("Your biography must not exceed 1,000 characters.")
            elif Listener.query.filter(
                Listener.id != listener.id,
                Listener.username.ilike(username)
            ).first():
                flash("That username is already in use.")
            elif Listener.query.filter(
                Listener.id != listener.id,
                Listener.email.ilike(email)
            ).first():
                flash("That email address is already in use.")
            else:
                photo = request.files.get("profile_image")
                new_photo = None
                photo_uploaded_remote = False
                old_photo = listener.profile_image

                if photo and photo.filename:
                    from io import BytesIO

                    ext = os.path.splitext(
                        secure_filename(photo.filename)
                    )[1].lower()

                    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                        flash("Profile photo must be JPG, PNG or WEBP.")
                        return render_template(
                            "listener/edit_profile.html",
                            listener=listener,
                            form_data=form_data
                        )

                    photo_bytes = photo.stream.read(5 * 1024 * 1024 + 1)
                    photo.stream.seek(0)

                    if not photo_bytes:
                        flash("The selected photo is empty.")
                        return render_template(
                            "listener/edit_profile.html",
                            listener=listener,
                            form_data=form_data
                        )

                    if len(photo_bytes) > 5 * 1024 * 1024:
                        flash("Profile photo must be 5 MB or smaller.")
                        return render_template(
                            "listener/edit_profile.html",
                            listener=listener,
                            form_data=form_data
                        )

                    valid_image = (
                        photo_bytes.startswith(b"\xff\xd8\xff")
                        if ext in {".jpg", ".jpeg"}
                        else photo_bytes.startswith(b"\x89PNG\r\n\x1a\n")
                        if ext == ".png"
                        else (
                            len(photo_bytes) >= 12
                            and photo_bytes[:4] == b"RIFF"
                            and photo_bytes[8:12] == b"WEBP"
                        )
                    )

                    if not valid_image:
                        flash("The uploaded file is not a valid matching image.")
                        return render_template(
                            "listener/edit_profile.html",
                            listener=listener,
                            form_data=form_data
                        )

                    new_photo = os.path.join(
                        "listeners", uuid4().hex + ext
                    )

                try:
                    if new_photo:
                        if filebase_enabled():
                            upload_image(BytesIO(photo_bytes), new_photo)
                            photo_uploaded_remote = True
                        else:
                            listener_folder = os.path.join(
                                app.config["UPLOAD_FOLDER"], "listeners"
                            )
                            os.makedirs(listener_folder, exist_ok=True)
                            photo_path = os.path.join(
                                app.config["UPLOAD_FOLDER"], new_photo
                            )
                            with open(photo_path, "wb") as image_file:
                                image_file.write(photo_bytes)

                    listener.username = username
                    listener.email = email
                    listener.display_name = display_name or username
                    listener.bio = bio or None

                    if new_photo:
                        listener.profile_image = new_photo

                    db.session.commit()

                    # Remove the previous listener photo only after a
                    # successful update and only when no other listener uses it.
                    if (
                        new_photo
                        and old_photo
                        and old_photo.startswith("listeners/")
                        and old_photo != new_photo
                    ):
                        try:
                            still_used = Listener.query.filter(
                                Listener.profile_image == old_photo,
                                Listener.id != listener.id
                            ).first()

                            if not still_used:
                                if filebase_enabled():
                                    delete_image(old_photo)

                                upload_folder = os.path.realpath(
                                    app.config["UPLOAD_FOLDER"]
                                )
                                old_path = os.path.realpath(
                                    os.path.join(upload_folder, old_photo)
                                )

                                if (
                                    os.path.commonpath(
                                        [upload_folder, old_path]
                                    ) == upload_folder
                                    and os.path.isfile(old_path)
                                ):
                                    os.remove(old_path)
                        except Exception:
                            app.logger.exception(
                                "Could not remove previous listener photo."
                            )

                    flash("Your profile has been updated.")
                    return redirect(url_for("listener_profile"))

                except IntegrityError:
                    db.session.rollback()

                    # Remove the newly uploaded photo if the database
                    # rejects the profile update.
                    if new_photo:
                        if photo_uploaded_remote:
                            try:
                                delete_image(new_photo)
                            except Exception:
                                app.logger.exception(
                                    "Could not remove rejected listener photo upload."
                                )
                        else:
                            upload_folder = os.path.realpath(
                                app.config["UPLOAD_FOLDER"]
                            )
                            new_path = os.path.realpath(
                                os.path.join(upload_folder, new_photo)
                            )
                            if (
                                os.path.commonpath(
                                    [upload_folder, new_path]
                                ) == upload_folder
                                and os.path.isfile(new_path)
                            ):
                                os.remove(new_path)

                    flash("That username or email is already in use.")
                except Exception:
                    db.session.rollback()

                    if new_photo:
                        if photo_uploaded_remote:
                            try:
                                delete_image(new_photo)
                            except Exception:
                                app.logger.exception(
                                    "Could not remove failed listener photo upload."
                                )
                        else:
                            upload_folder = os.path.realpath(
                                app.config["UPLOAD_FOLDER"]
                            )
                            new_path = os.path.realpath(
                                os.path.join(upload_folder, new_photo)
                            )
                            if (
                                os.path.commonpath(
                                    [upload_folder, new_path]
                                ) == upload_folder
                                and os.path.isfile(new_path)
                            ):
                                os.remove(new_path)

                    app.logger.exception("Listener profile update failed.")
                    flash("Could not update your profile. Please try again.")

        return render_template(
            "listener/edit_profile.html",
            listener=listener,
            form_data=form_data
        )


    @app.route("/listener/favorites")
    def listener_favorites():
        from models import Listener, ListenerFavorite, Mix

        listener_id = session.get("listener_id")

        if not listener_id:
            flash("Please log in to view your favourites.", "warning")
            return redirect(url_for("listener_login"))

        listener = db.session.get(Listener, listener_id)

        if not listener:
            session.pop("listener_id", None)
            return redirect(url_for("listener_login"))

        favorites = ListenerFavorite.query.join(
            Mix, ListenerFavorite.mix_id == Mix.id
        ).filter(
            ListenerFavorite.listener_id == listener.id,
            Mix.is_published.is_(True)
        ).order_by(
            ListenerFavorite.created_at.desc()
        ).all()

        return render_template(
            "listener/favorites.html",
            listener=listener,
            favorites=favorites
        )


    def get_playlist_listener():
        from models import Listener

        listener_id = session.get("listener_id")

        if not listener_id:
            flash("Please log in to manage your playlists.", "warning")
            return None

        listener = db.session.get(Listener, listener_id)

        if listener is None:
            session.pop("listener_id", None)
            flash("Please log in again.", "warning")
            return None

        return listener


    @app.route("/listener/playlists")
    def listener_playlists():
        from models import ListenerPlaylist

        listener = get_playlist_listener()
        if listener is None:
            return redirect(url_for("listener_login"))

        playlists = ListenerPlaylist.query.filter_by(
            listener_id=listener.id
        ).order_by(
            ListenerPlaylist.updated_at.desc()
        ).all()

        return render_template(
            "listener/playlists.html",
            listener=listener,
            playlists=playlists
        )


    @app.route("/listener/playlists/new", methods=["POST"])
    def create_listener_playlist():
        from models import ListenerPlaylist

        listener = get_playlist_listener()
        if listener is None:
            return redirect(url_for("listener_login"))

        name = request.form.get("name", "").strip()

        if not name or len(name) > 100:
            flash("Enter a playlist name between 1 and 100 characters.", "warning")
            return redirect(url_for("listener_playlists"))

        existing = ListenerPlaylist.query.filter(
            ListenerPlaylist.listener_id == listener.id,
            db.func.lower(ListenerPlaylist.name) == name.lower()
        ).first()

        if existing:
            flash("You already have a playlist with that name.", "warning")
            return redirect(url_for("listener_playlists"))

        try:
            playlist = ListenerPlaylist(
                listener_id=listener.id,
                name=name
            )
            db.session.add(playlist)
            db.session.commit()
            flash("Playlist created successfully.", "success")
            return redirect(
                url_for("listener_playlist_detail", playlist_id=playlist.id)
            )
        except Exception:
            db.session.rollback()
            app.logger.exception("Playlist creation failed")
            flash("Could not create your playlist. Please try again.", "danger")
            return redirect(url_for("listener_playlists"))


    @app.route("/listener/playlists/<int:playlist_id>")
    def listener_playlist_detail(playlist_id):
        from models import ListenerPlaylist, ListenerPlaylistItem, Mix

        listener = get_playlist_listener()
        if listener is None:
            return redirect(url_for("listener_login"))

        playlist = ListenerPlaylist.query.filter_by(
            id=playlist_id,
            listener_id=listener.id
        ).first_or_404()

        items = db.session.query(
            ListenerPlaylistItem,
            Mix
        ).join(
            Mix,
            ListenerPlaylistItem.mix_id == Mix.id
        ).filter(
            ListenerPlaylistItem.playlist_id == playlist.id,
            Mix.is_published.is_(True)
        ).order_by(
            ListenerPlaylistItem.position.asc(),
            ListenerPlaylistItem.added_at.asc()
        ).all()

        return render_template(
            "listener/playlist_detail.html",
            listener=listener,
            playlist=playlist,
            items=items,
            tracks=[
                {
                    "title": mix.title,
                    "dj": mix.dj.display_name or mix.dj.username,
                    "url": url_for("stream_mix", mix_id=mix.id)
                }
                for item, mix in items
            ]
        )


    @app.route(
        "/listener/playlists/<int:playlist_id>/rename",
        methods=["POST"]
    )
    def rename_listener_playlist(playlist_id):
        from models import ListenerPlaylist

        listener = get_playlist_listener()
        if listener is None:
            return redirect(url_for("listener_login"))

        playlist = ListenerPlaylist.query.filter_by(
            id=playlist_id,
            listener_id=listener.id
        ).first_or_404()

        name = request.form.get("name", "").strip()

        if not name or len(name) > 100:
            flash("Enter a playlist name between 1 and 100 characters.", "warning")
            return redirect(
                url_for("listener_playlist_detail", playlist_id=playlist.id)
            )

        duplicate = ListenerPlaylist.query.filter(
            ListenerPlaylist.listener_id == listener.id,
            ListenerPlaylist.id != playlist.id,
            db.func.lower(ListenerPlaylist.name) == name.lower()
        ).first()

        if duplicate:
            flash("You already have another playlist with that name.", "warning")
            return redirect(
                url_for("listener_playlist_detail", playlist_id=playlist.id)
            )

        try:
            playlist.name = name
            db.session.commit()
            flash("Playlist renamed successfully.", "success")
        except Exception:
            db.session.rollback()
            app.logger.exception("Playlist rename failed")
            flash("Could not rename your playlist.", "danger")

        return redirect(
            url_for("listener_playlist_detail", playlist_id=playlist.id)
        )


    @app.route(
        "/listener/playlists/<int:playlist_id>/delete",
        methods=["POST"]
    )
    def delete_listener_playlist(playlist_id):
        from models import ListenerPlaylist

        listener = get_playlist_listener()
        if listener is None:
            return redirect(url_for("listener_login"))

        playlist = ListenerPlaylist.query.filter_by(
            id=playlist_id,
            listener_id=listener.id
        ).first_or_404()

        try:
            db.session.delete(playlist)
            db.session.commit()
            flash("Playlist deleted successfully.", "success")
        except Exception:
            db.session.rollback()
            app.logger.exception("Playlist deletion failed")
            flash("Could not delete your playlist.", "danger")

        return redirect(url_for("listener_playlists"))


    @app.route(
        "/listener/playlists/<int:playlist_id>/add/<int:mix_id>",
        methods=["POST"]
    )
    def add_mix_to_listener_playlist(playlist_id, mix_id):
        from models import ListenerPlaylist, ListenerPlaylistItem, Mix

        listener = get_playlist_listener()
        if listener is None:
            return redirect(url_for("listener_login"))

        playlist = ListenerPlaylist.query.filter_by(
            id=playlist_id,
            listener_id=listener.id
        ).first_or_404()

        mix = Mix.query.filter_by(
            id=mix_id,
            is_published=True
        ).first_or_404()

        existing = ListenerPlaylistItem.query.filter_by(
            playlist_id=playlist.id,
            mix_id=mix.id
        ).first()

        if existing:
            flash("That mix is already in this playlist.", "info")
        else:
            try:
                last_position = db.session.query(
                    db.func.max(ListenerPlaylistItem.position)
                ).filter_by(
                    playlist_id=playlist.id
                ).scalar()

                db.session.add(ListenerPlaylistItem(
                    playlist_id=playlist.id,
                    mix_id=mix.id,
                    position=(last_position if last_position is not None else -1) + 1
                ))
                db.session.commit()
                flash("Mix added to your playlist.", "success")
            except Exception:
                db.session.rollback()
                app.logger.exception("Adding mix to playlist failed")
                flash("Could not add this mix to your playlist.", "danger")

        return redirect(
            url_for("listener_playlist_detail", playlist_id=playlist.id)
        )


    @app.route(
        "/listener/playlists/<int:playlist_id>/remove/<int:mix_id>",
        methods=["POST"]
    )
    def remove_mix_from_listener_playlist(playlist_id, mix_id):
        from models import ListenerPlaylist, ListenerPlaylistItem

        listener = get_playlist_listener()
        if listener is None:
            return redirect(url_for("listener_login"))

        playlist = ListenerPlaylist.query.filter_by(
            id=playlist_id,
            listener_id=listener.id
        ).first_or_404()

        item = ListenerPlaylistItem.query.filter_by(
            playlist_id=playlist.id,
            mix_id=mix_id
        ).first_or_404()

        try:
            db.session.delete(item)
            db.session.commit()
            flash("Mix removed from your playlist.", "success")
        except Exception:
            db.session.rollback()
            app.logger.exception("Removing mix from playlist failed")
            flash("Could not remove this mix.", "danger")

        return redirect(
            url_for("listener_playlist_detail", playlist_id=playlist.id)
        )


    @app.route("/listener/logout", methods=["POST"])
    def listener_logout():
        session.pop("listener_id", None)
        flash("You have been logged out.")
        return redirect(url_for("home"))


    @app.route("/api/push/public-key", methods=["GET"])
    def push_public_key():
        public_key = app.config.get("VAPID_PUBLIC_KEY")
        if not public_key:
            return jsonify({"error": "Push notifications are not configured."}), 503
        return jsonify({"publicKey": public_key})


    @app.route("/api/push/subscribe", methods=["POST"])
    def push_subscribe():
        from models import PushSubscription, Listener

        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")
        keys = data.get("keys") or {}
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")

        if (
            not isinstance(endpoint, str)
            or not endpoint.startswith("https://")
            or len(endpoint) > 4096
            or not isinstance(p256dh, str)
            or not p256dh
            or len(p256dh) > 512
            or not isinstance(auth, str)
            or not auth
            or len(auth) > 512
        ):
            return jsonify({"error": "Invalid push subscription data."}), 400

        listener_id = session.get("listener_id")
        if listener_id and not db.session.get(Listener, listener_id):
            session.pop("listener_id", None)
            listener_id = None

        dj_id = (
            current_user.id
            if current_user.is_authenticated
            else None
        )

        try:
            subscription = PushSubscription.query.filter_by(
                endpoint=endpoint
            ).first()

            if subscription is None:
                subscription = PushSubscription(endpoint=endpoint)
                db.session.add(subscription)

            subscription.p256dh = p256dh
            subscription.auth = auth
            subscription.listener_id = listener_id
            subscription.dj_id = dj_id

            db.session.commit()

            return jsonify({
                "success": True,
                "message": "Push subscription saved."
            }), 201

        except Exception:
            db.session.rollback()
            app.logger.exception("Saving push subscription failed")
            return jsonify({"error": "Could not save push subscription."}), 500


    @app.route("/api/push/unsubscribe", methods=["POST"])
    def push_unsubscribe():
        from models import PushSubscription, Listener

        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")

        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            return jsonify({"error": "Invalid subscription endpoint."}), 400

        listener_id = session.get("listener_id")
        if listener_id and not db.session.get(Listener, listener_id):
            session.pop("listener_id", None)
            listener_id = None

        dj_id = (
            current_user.id
            if current_user.is_authenticated
            else None
        )

        try:
            subscription = PushSubscription.query.filter_by(
                endpoint=endpoint
            ).first()

            if subscription is None:
                return jsonify({"success": True}), 200

            # The browser presents its own subscription endpoint.
            # Allow removal even if the user has since logged in or out.
            db.session.delete(subscription)
            db.session.commit()

            return jsonify({
                "success": True,
                "message": "Push subscription removed."
            })

        except Exception:
            db.session.rollback()
            app.logger.exception("Removing push subscription failed")
            return jsonify({"error": "Could not remove push subscription."}), 500


    @app.route("/service-worker.js")
    def service_worker():
        response = send_from_directory(
            app.static_folder,
            "service-worker.js",
            mimetype="application/javascript"
        )
        response.headers["Cache-Control"] = "no-cache"
        return response


    @app.route("/")
    def home():
        from models import Mix, Genre, DJ, Advertisement, Listener

        listener = None
        listener_id = session.get("listener_id")

        if listener_id:
            listener = db.session.get(Listener, listener_id)
            if listener is None:
                session.pop("listener_id", None)

        search = request.args.get("q", "").strip()
        genre_id = request.args.get("genre", type=int)

        query = Mix.query.filter_by(is_published=True)

        if search:
            query = query.join(Mix.dj).filter(
                or_(
                    Mix.title.ilike(f"%{search}%"),
                    DJ.username.ilike(f"%{search}%"),
                    DJ.display_name.ilike(f"%{search}%")
                )
            )

        if genre_id:
            query = query.filter(Mix.genre_id == genre_id)

        mixes = query.order_by(
            Mix.created_at.desc()
        ).limit(30).all()

        genres = Genre.query.order_by(Genre.name).all()

        today = date.today()
        ads = Advertisement.query.filter(
            Advertisement.is_active.is_(True),
            or_(
                Advertisement.start_date.is_(None),
                Advertisement.start_date <= today
            ),
            or_(
                Advertisement.end_date.is_(None),
                Advertisement.end_date >= today
            )
        ).order_by(Advertisement.created_at.desc()).all()

        # Count each banner displayed on this homepage request.
        try:
            for ad in ads:
                ad.impressions += 1
            if ads:
                db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Advertisement impression tracking failed")

        return render_template(
            "index.html",
            mixes=mixes,
            genres=genres,
            search=search,
            selected_genre=genre_id,
            ads=ads,
            listener=listener
        )

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        from models import Mix, DJ, Advertisement, Listener
        from audio_storage import (
            filebase_enabled, stream_cover, stream_image
        )

        local_path = safe_join(app.config["UPLOAD_FOLDER"], filename)
        if local_path and os.path.isfile(local_path):
            return send_from_directory(
                app.config["UPLOAD_FOLDER"], filename
            )

        if not filebase_enabled():
            return send_from_directory(
                app.config["UPLOAD_FOLDER"], filename
            )

        # Only database-linked mix covers may be read from Filebase.
        if Mix.query.filter_by(cover_image=filename).first():
            return stream_cover(filename)

        # Only database-linked DJ profile photos may be read remotely.
        if (
            filename.startswith("djs/")
            and DJ.query.filter_by(profile_image=filename).first()
        ):
            return stream_image(filename)

        # Only database-linked advertisement images may be read remotely.
        if (
            filename.startswith("ads/")
            and Advertisement.query.filter_by(image_file=filename).first()
        ):
            return stream_image(filename)

        # Only database-linked listener profile photos may be read remotely.
        if (
            filename.startswith("listeners/")
            and Listener.query.filter_by(profile_image=filename).first()
        ):
            return stream_image(filename)

        abort(404)


    @app.route("/djs")
    def public_dj_directory():
        from models import DJ, Mix

        search = request.args.get("q", "").strip()

        query = DJ.query.filter(
            or_(
                DJ.is_admin.is_(True),
                DJ.is_approved.is_(True)
            )
        )

        if search:
            query = query.filter(
                or_(
                    DJ.username.ilike(f"%{search}%"),
                    DJ.display_name.ilike(f"%{search}%"),
                    DJ.bio.ilike(f"%{search}%")
                )
            )

        djs = query.distinct().order_by(
            DJ.display_name.asc(),
            DJ.username.asc()
        ).all()

        return render_template(
            "djs.html",
            djs=djs,
            search=search
        )


    @app.route("/dj/<int:dj_id>")
    def public_dj_profile(dj_id):
        from models import DJ, Mix

        dj = DJ.query.get_or_404(dj_id)

        mixes = Mix.query.filter_by(
            dj_id=dj.id,
            is_published=True
        ).order_by(
            Mix.created_at.desc()
        ).all()

        return render_template(
            "dj_profile.html",
            dj=dj,
            mixes=mixes
        )


    @app.route("/mix/<int:mix_id>")
    def mix_detail(mix_id):
        from models import Mix, Listener, ListenerFavorite, ListenerPlaylist

        mix = Mix.query.filter_by(
            id=mix_id,
            is_published=True
        ).first_or_404()

        listener = None
        is_favorite = False
        listener_playlists = []

        listener_id = session.get("listener_id")

        if listener_id:
            listener = db.session.get(Listener, listener_id)

            if listener:
                is_favorite = ListenerFavorite.query.filter_by(
                    listener_id=listener.id,
                    mix_id=mix.id
                ).first() is not None
                listener_playlists = ListenerPlaylist.query.filter_by(
                    listener_id=listener.id
                ).order_by(ListenerPlaylist.name.asc()).all()
            else:
                session.pop("listener_id", None)

        related_mixes = Mix.query.filter(
            Mix.is_published.is_(True),
            Mix.id != mix.id,
            Mix.genre_id == mix.genre_id
        ).order_by(
            Mix.created_at.desc()
        ).limit(4).all() if mix.genre_id else []

        return render_template(
            "mix_detail.html",
            mix=mix,
            related_mixes=related_mixes,
            listener=listener,
            is_favorite=is_favorite,
            listener_playlists=listener_playlists
        )


    @app.route("/mix/<int:mix_id>/favorite", methods=["POST"])
    def toggle_favorite(mix_id):
        from models import Mix, Listener, ListenerFavorite

        listener_id = session.get("listener_id")

        if not listener_id:
            flash("Please log in to save favourite mixes.", "warning")
            return redirect(url_for("listener_login"))

        listener = db.session.get(Listener, listener_id)

        if not listener:
            session.pop("listener_id", None)
            flash("Please log in again.", "warning")
            return redirect(url_for("listener_login"))

        mix = Mix.query.filter_by(
            id=mix_id,
            is_published=True
        ).first_or_404()

        favorite = ListenerFavorite.query.filter_by(
            listener_id=listener.id,
            mix_id=mix.id
        ).first()

        try:
            if favorite:
                db.session.delete(favorite)
                flash("Mix removed from your favourites.", "success")
            else:
                db.session.add(ListenerFavorite(
                    listener_id=listener.id,
                    mix_id=mix.id
                ))
                flash("Mix added to your favourites!", "success")

            db.session.commit()

        except Exception:
            db.session.rollback()
            app.logger.exception("Favourite update failed")
            flash("Could not update your favourites. Please try again.", "danger")

        return redirect(url_for("mix_detail", mix_id=mix.id))


    @app.route("/mix/<int:mix_id>/stream")
    def stream_mix(mix_id):
        from models import Mix, MixEvent
        from audio_storage import filebase_enabled, stream_audio

        mix = Mix.query.filter_by(
            id=mix_id, is_published=True
        ).first_or_404()

        mix.plays += 1
        db.session.add(MixEvent(
            mix_id=mix.id,
            event_type="play"
        ))
        db.session.commit()

        if filebase_enabled():
            return stream_audio(mix.audio_file)

        return send_from_directory(
            app.config["UPLOAD_FOLDER"],
            mix.audio_file,
            conditional=True
        )


    @app.route("/mix/<int:mix_id>/download")
    def download_mix(mix_id):
        from models import Mix, MixEvent
        from audio_storage import filebase_enabled, stream_audio

        mix = Mix.query.filter_by(
            id=mix_id, is_published=True
        ).first_or_404()

        mix.downloads += 1
        db.session.add(MixEvent(
            mix_id=mix.id,
            event_type="download"
        ))
        db.session.commit()

        if filebase_enabled():
            return stream_audio(mix.audio_file, as_attachment=True)

        return send_from_directory(
            app.config["UPLOAD_FOLDER"],
            mix.audio_file,
            as_attachment=True
        )


    @app.route("/book-dj", methods=["GET", "POST"])
    def public_booking():
        from models import Booking, DJ

        djs = DJ.query.filter(
            or_(
                DJ.is_admin.is_(True),
                DJ.is_approved.is_(True)
            )
        ).order_by(DJ.display_name, DJ.username).all()

        selected_dj_id = request.args.get("dj_id", type=int)

        def render_booking():
            return render_template(
                "booking.html",
                today=date.today().isoformat(),
                djs=djs,
                selected_dj_id=selected_dj_id
            )

        if request.method == "POST":
            client_name = request.form.get("client_name", "").strip()
            phone = request.form.get("phone", "").strip()
            email = request.form.get("email", "").strip()
            event_type = request.form.get("event_type", "").strip()
            event_date_raw = request.form.get("event_date", "").strip()
            venue = request.form.get("venue", "").strip()
            budget_raw = request.form.get("budget", "").strip()
            message = request.form.get("message", "").strip()

            dj_id_raw = request.form.get("dj_id", "").strip()
            selected_dj_id = None

            if dj_id_raw:
                try:
                    selected_dj_id = int(dj_id_raw)
                except ValueError:
                    flash("Please select a valid DJ.", "error")
                    return render_booking()

                selected_dj = next(
                    (dj for dj in djs if dj.id == selected_dj_id),
                    None
                )

                if selected_dj is None:
                    selected_dj_id = None
                    flash(
                        "That DJ is unavailable. Please select an active DJ.",
                        "error"
                    )
                    return render_booking()
            else:
                selected_dj = None

            if not all([
                client_name, phone, event_type,
                event_date_raw, venue
            ]):
                flash("Please complete all required fields.", "error")
                return render_booking()

            if len(client_name) > 120 or len(phone) > 30:
                flash("Name or phone number is too long.", "error")
                return render_booking()

            if email and len(email) > 150:
                flash("Email address is too long.", "error")
                return render_booking()

            if len(event_type) > 80 or len(venue) > 200:
                flash("Event type or venue is too long.", "error")
                return render_booking()

            try:
                event_date = date.fromisoformat(event_date_raw)
            except ValueError:
                flash("Please enter a valid event date.", "error")
                return render_booking()

            if event_date < date.today():
                flash("Event date cannot be in the past.", "error")
                return render_booking()

            budget = None
            if budget_raw:
                try:
                    budget = Decimal(budget_raw)
                    if not budget.is_finite() or budget < 0:
                        raise InvalidOperation
                except InvalidOperation:
                    flash("Please enter a valid budget.", "error")
                    return render_booking()

            booking = Booking(
                dj_id=selected_dj.id if selected_dj else None,
                client_name=client_name,
                phone=phone,
                email=email or None,
                event_type=event_type,
                event_date=event_date,
                venue=venue,
                budget=budget,
                message=message,
                status="pending"
            )

            db.session.add(booking)
            db.session.commit()

            try:
                send_booking_created_notification(booking)
            except Exception:
                app.logger.exception(
                    "Booking was saved, but its push notification failed."
                )

            if selected_dj:
                confirmation = (
                    f"Your booking request for "
                    f"{selected_dj.display_name or selected_dj.username} "
                    "has been submitted! We will contact you soon."
                )
            else:
                confirmation = (
                    "Your booking request has been submitted! "
                    "We will contact you soon."
                )

            flash(confirmation, "success")
            return redirect(url_for("public_booking"))

        if selected_dj_id and not any(
            dj.id == selected_dj_id for dj in djs
        ):
            selected_dj_id = None

        return render_booking()


    @app.route("/dj/register", methods=["GET", "POST"])
    def dj_register():
        from models import DJ
        from sqlalchemy.exc import IntegrityError

        if current_user.is_authenticated:
            if current_user.is_admin:
                return redirect(url_for("admin_dashboard"))
            if current_user.is_approved:
                return redirect(url_for("dj_dashboard"))
            logout_user()
            flash("Your DJ account is suspended. Please contact the administrator.")

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            display_name = request.form.get("display_name", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")

            if not username or not email or not display_name or not password:
                flash("Please complete all required fields.")
            elif len(username) > 80 or len(email) > 150:
                flash("Username or email is too long.")
            elif len(display_name) > 120:
                flash("Display name must not exceed 120 characters.")
            elif "@" not in email or "." not in email.split("@")[-1]:
                flash("Please enter a valid email address.")
            elif len(password) < 8:
                flash("Password must contain at least 8 characters.")
            elif password != confirm:
                flash("Passwords do not match.")
            else:
                duplicate = DJ.query.filter(
                    or_(
                        db.func.lower(DJ.username) == username.lower(),
                        db.func.lower(DJ.email) == email
                    )
                ).first()

                if duplicate:
                    flash("That username or email is already registered.")
                else:
                    dj = DJ(
                        username=username,
                        email=email,
                        display_name=display_name,
                        password_hash=generate_password_hash(password),
                        is_admin=False,
                        is_approved=True
                    )

                    try:
                        db.session.add(dj)
                        db.session.commit()
                        flash("Registration successful! Your DJ account is active. You can now log in.")
                        return redirect(url_for("dj_login"))
                    except IntegrityError:
                        db.session.rollback()
                        flash("That username or email is already in use.")
                    except Exception:
                        db.session.rollback()
                        app.logger.exception("DJ registration failed")
                        flash("Registration failed. Please try again.")

        return render_template("dj/register.html")


    @app.route("/dj/login", methods=["GET", "POST"])
    def dj_login():
        from models import DJ

        if current_user.is_authenticated:
            if current_user.is_admin:
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("dj_dashboard"))

        if request.method == "POST":
            identity = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            dj = DJ.query.filter(
                or_(
                    db.func.lower(DJ.username) == identity.lower(),
                    db.func.lower(DJ.email) == identity.lower()
                )
            ).first()

            if not dj or not check_password_hash(dj.password_hash, password):
                flash("Invalid username/email or password.")
            elif not dj.is_admin and not dj.is_approved:
                flash("This DJ account is suspended. Please contact the administrator.")
            else:
                login_user(dj)

                if dj.is_admin:
                    return redirect(url_for("admin_dashboard"))

                return redirect(url_for("dj_dashboard"))

        return render_template("dj/login.html")


    @app.route("/dj/logout", methods=["POST"])
    @dj_required
    def dj_logout():
        logout_user()
        flash("You have been logged out.")
        return redirect(url_for("dj_login"))


    @app.route("/dj/dashboard")
    @dj_required
    def dj_dashboard():
        from models import Mix, Booking

        mixes = Mix.query.filter_by(
            dj_id=current_user.id
        ).order_by(Mix.created_at.desc()).all()

        booking_query = Booking.query.filter_by(dj_id=current_user.id)

        stats = {
            "total": len(mixes),
            "published": sum(1 for mix in mixes if mix.is_published),
            "plays": sum(mix.plays for mix in mixes),
            "downloads": sum(mix.downloads for mix in mixes),
            "bookings": booking_query.count(),
            "pending_bookings": booking_query.filter_by(status="pending").count(),
            "confirmed_bookings": booking_query.filter_by(status="confirmed").count()
        }

        return render_template(
            "dj/dashboard.html",
            dj=current_user,
            mixes=mixes,
            stats=stats
        )


    @app.route("/dj/mixes/new", methods=["GET", "POST"])
    @dj_required
    def dj_upload_mix():
        from models import Mix, Genre

        genres = Genre.query.order_by(Genre.name).all()

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            audio = request.files.get("audio_file")
            cover = request.files.get("cover_image")
            genre_value = request.form.get("genre_id", "").strip()

            if not title or len(title) > 180:
                flash("Enter a valid mix title.")
                return render_template("dj/upload_mix.html", genres=genres)

            if not audio or not audio.filename:
                flash("Please select an MP3 file.")
                return render_template("dj/upload_mix.html", genres=genres)

            audio_name = secure_filename(audio.filename)
            if os.path.splitext(audio_name)[1].lower() != ".mp3":
                flash("Only MP3 files are allowed.")
                return render_template("dj/upload_mix.html", genres=genres)

            cover_ext = None
            if cover and cover.filename:
                cover_name = secure_filename(cover.filename)
                cover_ext = os.path.splitext(cover_name)[1].lower()
                if cover_ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                    flash("Cover must be JPG, PNG or WEBP.")
                    return render_template("dj/upload_mix.html", genres=genres)

            genre_id = None
            if genre_value:
                try:
                    genre_id = int(genre_value)
                except ValueError:
                    flash("Invalid genre selected.")
                    return render_template("dj/upload_mix.html", genres=genres)

                if not db.session.get(Genre, genre_id):
                    flash("Selected genre does not exist.")
                    return render_template("dj/upload_mix.html", genres=genres)

            audio_filename = uuid4().hex + ".mp3"
            cover_filename = uuid4().hex + cover_ext if cover_ext else None

            audio_path = os.path.join(app.config["UPLOAD_FOLDER"], audio_filename)
            cover_path = (
                os.path.join(app.config["UPLOAD_FOLDER"], cover_filename)
                if cover_filename else None
            )

            from audio_storage import (
                filebase_enabled, upload_audio, delete_audio,
                upload_cover, delete_cover
            )
            audio_uploaded_remote = False
            cover_uploaded_remote = False

            try:
                if filebase_enabled():
                    upload_audio(audio, audio_filename)
                    audio_uploaded_remote = True
                else:
                    audio.save(audio_path)

                if cover_path:
                    if filebase_enabled():
                        upload_cover(cover, cover_filename)
                        cover_uploaded_remote = True
                    else:
                        cover.save(cover_path)

                mix = Mix(
                    title=title,
                    description=description,
                    audio_file=audio_filename,
                    cover_image=cover_filename,
                    dj_id=current_user.id,
                    genre_id=genre_id,
                    is_published=False
                )

                db.session.add(mix)
                db.session.commit()

                flash("Mix uploaded successfully and submitted for review.")
                return redirect(url_for("dj_dashboard"))

            except Exception:
                db.session.rollback()

                if audio_uploaded_remote:
                    try:
                        delete_audio(audio_filename)
                    except Exception:
                        app.logger.exception(
                            "Could not clean up Filebase audio"
                        )
                if cover_uploaded_remote:
                    try:
                        delete_cover(cover_filename)
                    except Exception:
                        app.logger.exception(
                            "Could not clean up Filebase cover"
                        )

                for saved_path in (audio_path, cover_path):
                    if saved_path and os.path.exists(saved_path):
                        os.remove(saved_path)
                app.logger.exception("DJ mix upload failed")
                flash("Upload failed. Please try again.")

        return render_template("dj/upload_mix.html", genres=genres)



    @app.route("/dj/profile", methods=["GET", "POST"])
    @dj_required
    def dj_edit_profile():
        from models import DJ
        from urllib.parse import urlparse

        dj = db.session.get(DJ, current_user.id)
        if not dj or dj.is_admin:
            abort(403)

        if request.method == "POST":
            display_name = request.form.get("display_name", "").strip()
            bio = request.form.get("bio", "").strip()

            social_fields = [
                "youtube_url", "instagram_url",
                "tiktok_url", "facebook_url"
            ]
            social_values = {}

            if not display_name or len(display_name) > 120:
                flash("Enter a valid display name.")
                return render_template("dj/profile.html", dj=dj)

            invalid = False
            for field in social_fields:
                value = request.form.get(field, "").strip()
                if value:
                    parsed = urlparse(value)
                    if len(value) > 500 or parsed.scheme not in ("http", "https") or not parsed.netloc:
                        flash("Please enter valid social media URLs.")
                        invalid = True
                        break
                social_values[field] = value or None

            if invalid:
                return render_template("dj/profile.html", dj=dj)

            from audio_storage import (
                filebase_enabled, upload_image, delete_image
            )

            photo = request.files.get("profile_image")
            new_photo = None
            photo_uploaded_remote = False

            if photo and photo.filename:
                ext = os.path.splitext(
                    secure_filename(photo.filename)
                )[1].lower()

                if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                    flash("Profile photo must be JPG, PNG or WEBP.")
                    return render_template("dj/profile.html", dj=dj)

                new_photo = os.path.join("djs", uuid4().hex + ext)

            old_photo = dj.profile_image

            try:
                if new_photo:
                    if filebase_enabled():
                        upload_image(photo.stream, new_photo)
                        photo_uploaded_remote = True
                    else:
                        os.makedirs(
                            os.path.join(app.config["UPLOAD_FOLDER"], "djs"),
                            exist_ok=True
                        )
                        photo_path = os.path.join(
                            app.config["UPLOAD_FOLDER"], new_photo
                        )
                        photo.save(photo_path)

                dj.display_name = display_name
                dj.bio = bio

                for field, value in social_values.items():
                    setattr(dj, field, value)

                if new_photo:
                    dj.profile_image = new_photo

                db.session.commit()

                if new_photo and old_photo and old_photo != new_photo:
                    if old_photo.startswith("djs/") and filebase_enabled():
                        try:
                            image_in_use = DJ.query.filter(
                                DJ.profile_image == old_photo,
                                DJ.id != dj.id,
                            ).first()

                            if not image_in_use:
                                delete_image(old_photo)
                        except Exception:
                            app.logger.exception(
                                "Could not remove old DJ photo from Filebase"
                            )

                    upload_folder = os.path.realpath(
                        app.config["UPLOAD_FOLDER"]
                    )
                    old_path = os.path.realpath(
                        os.path.join(upload_folder, old_photo)
                    )

                    if (
                        os.path.commonpath([upload_folder, old_path])
                        == upload_folder
                        and os.path.isfile(old_path)
                    ):
                        try:
                            os.remove(old_path)
                        except OSError:
                            app.logger.exception(
                                "Could not remove old DJ photo"
                            )

                flash("Your DJ profile has been updated.")
                return redirect(url_for("dj_dashboard"))

            except Exception:
                db.session.rollback()

                if new_photo:
                    if photo_uploaded_remote:
                        try:
                            delete_image(new_photo)
                        except Exception:
                            app.logger.exception(
                                "Could not remove failed DJ photo upload"
                            )
                    else:
                        new_path = os.path.realpath(
                            os.path.join(
                                app.config["UPLOAD_FOLDER"], new_photo
                            )
                        )
                        upload_folder = os.path.realpath(
                            app.config["UPLOAD_FOLDER"]
                        )

                        if (
                            os.path.commonpath([upload_folder, new_path])
                            == upload_folder
                            and os.path.isfile(new_path)
                        ):
                            os.remove(new_path)

                app.logger.exception("DJ profile update failed")
                flash("Could not update your profile.")

        return render_template("dj/profile.html", dj=dj)



    @app.route("/dj/mixes/<int:mix_id>/edit", methods=["GET", "POST"])
    @dj_required
    def dj_edit_mix(mix_id):
        from models import Mix, Genre

        mix = Mix.query.filter_by(
            id=mix_id, dj_id=current_user.id
        ).first_or_404()

        genres = Genre.query.order_by(Genre.name).all()

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            genre_value = request.form.get("genre_id", "").strip()

            if not title or len(title) > 180:
                flash("Enter a valid mix title.")
                return render_template("dj/edit_mix.html", mix=mix, genres=genres)

            genre_id = None
            if genre_value:
                try:
                    genre_id = int(genre_value)
                except ValueError:
                    flash("Invalid genre selected.")
                    return render_template("dj/edit_mix.html", mix=mix, genres=genres)

                if not db.session.get(Genre, genre_id):
                    flash("Selected genre does not exist.")
                    return render_template("dj/edit_mix.html", mix=mix, genres=genres)

            mix.title = title
            mix.description = description
            mix.genre_id = genre_id

            try:
                db.session.commit()
                flash("Your mix details have been updated.")
                return redirect(url_for("dj_dashboard"))
            except Exception:
                db.session.rollback()
                app.logger.exception("DJ mix edit failed")
                flash("Could not update your mix.")

        return render_template("dj/edit_mix.html", mix=mix, genres=genres)


    @app.route("/dj/mixes/<int:mix_id>/toggle", methods=["POST"])
    @dj_required
    def dj_toggle_mix(mix_id):
        from models import Mix

        mix = Mix.query.filter_by(
            id=mix_id, dj_id=current_user.id
        ).first_or_404()

        was_published = bool(mix.is_published)
        mix.is_published = not was_published

        try:
            db.session.commit()

            if not was_published and mix.is_published:
                try:
                    send_mix_published_notification(mix)
                except Exception:
                    app.logger.exception(
                        "Push notification failed after DJ mix publication."
                    )

            flash("Your mix is now published." if mix.is_published else "Your mix is now unpublished.")
        except Exception:
            db.session.rollback()
            app.logger.exception("DJ mix publishing update failed")
            flash("Could not change publication status.")

        return redirect(url_for("dj_dashboard"))


    @app.route("/dj/mixes/<int:mix_id>/delete", methods=["POST"])
    @dj_required
    def dj_delete_mix(mix_id):
        from models import Mix, MixEvent

        mix = Mix.query.filter_by(
            id=mix_id, dj_id=current_user.id
        ).first_or_404()

        from audio_storage import filebase_enabled, delete_audio, delete_cover

        audio_filename = mix.audio_file
        cover_filename = mix.cover_image

        try:
            MixEvent.query.filter_by(
                mix_id=mix.id
            ).delete(synchronize_session=False)

            db.session.expire(mix, ["analytics_events"])
            db.session.delete(mix)
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Mix deletion failed")
            flash("Could not delete the mix.")
            return redirect(url_for("dj_dashboard"))

        def still_used(filename):
            return Mix.query.filter(
                or_(
                    Mix.audio_file == filename,
                    Mix.cover_image == filename
                )
            ).first() is not None

        def remove_local_file(filename):
            if not filename or still_used(filename):
                return

            upload_folder = os.path.realpath(
                app.config["UPLOAD_FOLDER"]
            )
            file_path = os.path.realpath(
                os.path.join(
                    upload_folder,
                    os.path.basename(filename)
                )
            )

            if os.path.commonpath(
                [upload_folder, file_path]
            ) != upload_folder:
                return

            if os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    app.logger.exception(
                        "Could not remove uploaded file: %s",
                        filename
                    )

        # Remove the MP3 from its actual storage location.
        if audio_filename and not still_used(audio_filename):
            if filebase_enabled():
                try:
                    delete_audio(audio_filename)
                except Exception:
                    app.logger.exception(
                        "Could not delete Filebase audio: %s",
                        audio_filename
                    )
            else:
                remove_local_file(audio_filename)

        # Prefer deleting a legacy local cover; new covers are in Filebase.
        if cover_filename and not still_used(cover_filename):
            local_cover_path = os.path.join(
                app.config["UPLOAD_FOLDER"],
                os.path.basename(cover_filename)
            )
            if os.path.isfile(local_cover_path):
                remove_local_file(cover_filename)
            elif filebase_enabled():
                try:
                    delete_cover(cover_filename)
                except Exception:
                    app.logger.exception(
                        "Could not delete Filebase cover: %s",
                        cover_filename
                    )
            else:
                remove_local_file(cover_filename)

        flash("Your mix has been deleted.")
        return redirect(url_for("dj_dashboard"))


    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        from models import DJ

        if current_user.is_authenticated:
            if current_user.is_admin:
                return redirect(url_for("admin_dashboard"))
            logout_user()

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            user = DJ.query.filter_by(username=username).first()

            if (
                user
                and user.is_admin
                and check_password_hash(user.password_hash, password)
            ):
                login_user(user)
                return redirect(url_for("admin_dashboard"))

            flash("Invalid administrator username or password.")

        return render_template("admin/login.html")

    @admin_required

    @app.route("/admin/profile", methods=["GET", "POST"])
    @admin_required
    def admin_edit_profile():
        from models import DJ

        if request.method == "POST":
            display_name = request.form.get("display_name", "").strip()
            bio = request.form.get("bio", "").strip()
            image = request.files.get("profile_image")

            from urllib.parse import urlparse

            social_fields = {
                "youtube_url": "YouTube",
                "instagram_url": "Instagram",
                "tiktok_url": "TikTok",
                "facebook_url": "Facebook",
            }

            social_urls = {}

            for field, label in social_fields.items():
                value = request.form.get(field, "").strip()

                if value:
                    parsed = urlparse(value)

                    if (
                        len(value) > 500
                        or parsed.scheme not in ("http", "https")
                        or not parsed.netloc
                    ):
                        flash(f"Please enter a valid {label} URL.")
                        return redirect(url_for("admin_edit_profile"))

                social_urls[field] = value or None

            if not display_name:
                flash("Please enter your DJ display name.")
                return redirect(url_for("admin_edit_profile"))

            if len(display_name) > 120:
                flash("Display name must be 120 characters or fewer.")
                return redirect(url_for("admin_edit_profile"))

            from audio_storage import (
                filebase_enabled, upload_image, delete_image
            )

            new_full_path = None
            new_relative_path = None
            new_image_uploaded_remote = False
            old_image = current_user.profile_image

            try:
                if image and image.filename:
                    safe_name = secure_filename(image.filename)
                    extension = os.path.splitext(safe_name)[1].lower()

                    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                        flash("Profile photo must be JPG, JPEG, PNG or WEBP.")
                        return redirect(url_for("admin_edit_profile"))

                    filename = f"{uuid4().hex}{extension}"
                    new_relative_path = f"djs/{filename}"

                    if filebase_enabled():
                        upload_image(image.stream, new_relative_path)
                        new_image_uploaded_remote = True
                    else:
                        folder = os.path.join(
                            app.config["UPLOAD_FOLDER"], "djs"
                        )
                        os.makedirs(folder, exist_ok=True)
                        new_full_path = os.path.join(folder, filename)
                        image.save(new_full_path)

                    current_user.profile_image = new_relative_path

                current_user.display_name = display_name
                current_user.bio = bio or None

                for field, value in social_urls.items():
                    setattr(current_user, field, value)

                db.session.commit()

            except Exception:
                db.session.rollback()

                if new_image_uploaded_remote and new_relative_path:
                    try:
                        delete_image(new_relative_path)
                    except Exception:
                        app.logger.exception(
                            "Could not remove failed admin profile photo upload"
                        )

                if new_full_path and os.path.isfile(new_full_path):
                    try:
                        os.remove(new_full_path)
                    except OSError:
                        app.logger.exception(
                            "Could not remove failed admin profile photo"
                        )

                app.logger.exception("DJ profile update failed")
                flash("Could not update your DJ profile.")
                return redirect(url_for("admin_edit_profile"))

            if new_relative_path and old_image and old_image != new_relative_path:
                if old_image.startswith("djs/") and filebase_enabled():
                    try:
                        image_in_use = DJ.query.filter(
                            DJ.profile_image == old_image,
                            DJ.id != current_user.id,
                        ).first()

                        if not image_in_use:
                            delete_image(old_image)
                    except Exception:
                        app.logger.exception(
                            "Could not remove old admin profile photo from Filebase"
                        )

                upload_folder = os.path.realpath(
                    app.config["UPLOAD_FOLDER"]
                )
                old_full_path = os.path.realpath(
                    os.path.join(upload_folder, old_image)
                )

                if (
                    os.path.commonpath([upload_folder, old_full_path])
                    == upload_folder
                    and os.path.isfile(old_full_path)
                ):
                    try:
                        os.remove(old_full_path)
                    except OSError:
                        app.logger.warning(
                            "Could not remove old DJ profile image."
                        )

            flash("DJ profile updated successfully.")
            return redirect(url_for("admin_edit_profile"))

        return render_template(
            "admin/profile.html",
            dj=current_user
        )


    @app.route("/admin/djs")
    @admin_required
    def admin_manage_djs():
        from models import DJ

        djs = DJ.query.filter(
            DJ.is_admin.is_(False)
        ).order_by(
            DJ.created_at.desc(), DJ.id.desc()
        ).all()

        return render_template("admin/djs.html", djs=djs)


    @app.route("/admin/djs/<int:dj_id>/toggle-status", methods=["POST"])
    @admin_required
    def admin_toggle_dj_status(dj_id):
        from models import DJ

        dj = DJ.query.filter(
            DJ.id == dj_id,
            DJ.is_admin.is_(False)
        ).first_or_404()

        dj.is_approved = not dj.is_approved

        try:
            db.session.commit()
            status = "active" if dj.is_approved else "suspended"
            flash(f"{dj.display_name or dj.username}'s account is now {status}.")
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not update DJ account status")
            flash("Could not update the DJ account. Please try again.")

        return redirect(url_for("admin_manage_djs"))


    @app.route("/admin")
    def admin_dashboard():
        from models import DJ, Genre, Mix, MixEvent
        from datetime import datetime, timezone, timedelta
        from zoneinfo import ZoneInfo

        kenya = ZoneInfo("Africa/Nairobi")
        local_now = datetime.now(kenya)

        today_start = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_start = today_start - timedelta(
            days=today_start.weekday()
        )
        month_start = today_start.replace(day=1)
        chart_start = today_start - timedelta(days=6)

        def utc_naive(local_datetime):
            return local_datetime.astimezone(
                timezone.utc
            ).replace(tzinfo=None)

        def event_count(event_type, start):
            return MixEvent.query.filter(
                MixEvent.event_type == event_type,
                MixEvent.created_at >= utc_naive(start)
            ).count()

        stats = {
            "total_mixes": Mix.query.count(),
            "published_mixes": Mix.query.filter_by(
                is_published=True
            ).count(),
            "total_djs": DJ.query.count(),
            "total_genres": Genre.query.count(),
            "total_plays": db.session.query(
                db.func.coalesce(db.func.sum(Mix.plays), 0)
            ).scalar(),
            "total_downloads": db.session.query(
                db.func.coalesce(db.func.sum(Mix.downloads), 0)
            ).scalar(),
            "plays_today": event_count("play", today_start),
            "downloads_today": event_count("download", today_start),
            "plays_week": event_count("play", week_start),
            "downloads_week": event_count("download", week_start),
            "plays_month": event_count("play", month_start),
            "downloads_month": event_count("download", month_start)
        }

        daily_activity = []
        activity = {}

        for day_offset in range(7):
            day = chart_start.date() + timedelta(days=day_offset)
            activity[day] = {"plays": 0, "downloads": 0}

        events = MixEvent.query.filter(
            MixEvent.created_at >= utc_naive(chart_start)
        ).all()

        for event in events:
            event_time = event.created_at.replace(
                tzinfo=timezone.utc
            ).astimezone(kenya)

            event_day = event_time.date()

            if event_day in activity:
                if event.event_type in ("play", "download"):
                    activity[event_day][
                        "plays" if event.event_type == "play"
                        else "downloads"
                    ] += 1

        for day, counts in activity.items():
            daily_activity.append({
                "label": day.strftime("%a"),
                "date": day.strftime("%d %b"),
                "plays": counts["plays"],
                "downloads": counts["downloads"]
            })

        latest_mixes = Mix.query.order_by(
            Mix.created_at.desc()
        ).limit(10).all()

        return render_template(
            "admin/dashboard.html",
            stats=stats,
            latest_mixes=latest_mixes,
            daily_activity=daily_activity,
            max_activity=max(
                (item["plays"] + item["downloads"]
                 for item in daily_activity),
                default=1
            ) or 1
        )

    @app.route("/admin/logout", methods=["POST"])
    @admin_required
    def admin_logout():
        logout_user()
        flash("You have been logged out.")
        return redirect(url_for("admin_login"))


    @app.route("/admin/mixes/new", methods=["GET", "POST"])
    @admin_required
    def admin_upload_mix():
        from models import Mix, Genre

        genres = Genre.query.order_by(Genre.name).all()

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            audio = request.files.get("audio_file")
            cover = request.files.get("cover_image")
            genre_value = request.form.get("genre_id", "").strip()

            if not title:
                flash("Please enter a mix title.")
                return render_template("admin/upload_mix.html", genres=genres)

            if not audio or not audio.filename:
                flash("Please select an MP3 file.")
                return render_template("admin/upload_mix.html", genres=genres)

            audio_name = secure_filename(audio.filename)
            audio_ext = os.path.splitext(audio_name)[1].lower()

            if audio_ext != ".mp3":
                flash("Only MP3 audio files are allowed.")
                return render_template("admin/upload_mix.html", genres=genres)

            cover_ext = None
            if cover and cover.filename:
                cover_name = secure_filename(cover.filename)
                cover_ext = os.path.splitext(cover_name)[1].lower()

                if cover_ext not in {".jpg", ".jpeg", ".png", ".webp"}:
                    flash("Cover must be JPG, JPEG, PNG or WEBP.")
                    return render_template("admin/upload_mix.html", genres=genres)

            genre_id = None
            if genre_value:
                try:
                    genre_id = int(genre_value)
                except ValueError:
                    flash("Invalid genre selected.")
                    return render_template("admin/upload_mix.html", genres=genres)

                if not db.session.get(Genre, genre_id):
                    flash("Selected genre does not exist.")
                    return render_template("admin/upload_mix.html", genres=genres)

            audio_filename = uuid4().hex + ".mp3"
            cover_filename = (
                uuid4().hex + cover_ext if cover_ext else None
            )

            audio_path = os.path.join(
                app.config["UPLOAD_FOLDER"], audio_filename
            )
            cover_path = (
                os.path.join(app.config["UPLOAD_FOLDER"], cover_filename)
                if cover_filename else None
            )

            from audio_storage import (
                filebase_enabled, upload_audio, delete_audio,
                upload_cover, delete_cover
            )
            audio_uploaded_remote = False
            cover_uploaded_remote = False

            try:
                if filebase_enabled():
                    upload_audio(audio, audio_filename)
                    audio_uploaded_remote = True
                else:
                    audio.save(audio_path)

                if cover_path:
                    if filebase_enabled():
                        upload_cover(cover, cover_filename)
                        cover_uploaded_remote = True
                    else:
                        cover.save(cover_path)

                mix = Mix(
                    title=title,
                    description=description,
                    audio_file=audio_filename,
                    cover_image=cover_filename,
                    dj_id=current_user.id,
                    genre_id=genre_id,
                    is_published=request.form.get("is_published") == "on"
                )

                db.session.add(mix)
                db.session.commit()

                if mix.is_published:
                    try:
                        send_mix_published_notification(mix)
                    except Exception:
                        app.logger.exception(
                            "Push notification failed after admin mix upload."
                        )

                flash("Mix uploaded successfully.")
                return redirect(url_for("admin_dashboard"))

            except Exception:
                db.session.rollback()

                if audio_uploaded_remote:
                    try:
                        delete_audio(audio_filename)
                    except Exception:
                        app.logger.exception(
                            "Could not clean up Filebase audio"
                        )

                if cover_uploaded_remote:
                    try:
                        delete_cover(cover_filename)
                    except Exception:
                        app.logger.exception(
                            "Could not clean up Filebase cover"
                        )

                for saved_path in (audio_path, cover_path):
                    if saved_path and os.path.exists(saved_path):
                        os.remove(saved_path)

                app.logger.exception("Mix upload failed")
                flash("Upload failed. Please try again.")

        return render_template("admin/upload_mix.html", genres=genres)


    @app.route("/admin/mixes")
    @admin_required
    def admin_manage_mixes():
        from models import Mix, DJ, Genre

        search = request.args.get("q", "").strip()
        status = request.args.get("status", "all")

        query = Mix.query.join(Mix.dj)

        if search:
            query = query.filter(
                or_(
                    Mix.title.ilike(f"%{search}%"),
                    DJ.username.ilike(f"%{search}%"),
                    DJ.display_name.ilike(f"%{search}%")
                )
            )

        if status == "published":
            query = query.filter_by(is_published=True)
        elif status == "draft":
            query = query.filter_by(is_published=False)

        mixes = query.order_by(Mix.created_at.desc()).all()
        genres = Genre.query.order_by(Genre.name).all()

        return render_template(
            "admin/manage_mixes.html",
            mixes=mixes,
            genres=genres,
            search=search,
            status=status
        )

    @app.route("/admin/mixes/<int:mix_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_edit_mix(mix_id):
        from models import Mix, Genre

        mix = db.session.get(Mix, mix_id)
        if not mix:
            abort(404)

        genres = Genre.query.order_by(Genre.name).all()

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            genre_value = request.form.get("genre_id", "").strip()

            if not title:
                flash("Mix title is required.")
                return render_template(
                    "admin/edit_mix.html", mix=mix, genres=genres
                )

            genre_id = None
            if genre_value:
                try:
                    genre_id = int(genre_value)
                except ValueError:
                    flash("Invalid genre selected.")
                    return render_template(
                        "admin/edit_mix.html", mix=mix, genres=genres
                    )

                if not db.session.get(Genre, genre_id):
                    flash("Selected genre does not exist.")
                    return render_template(
                        "admin/edit_mix.html", mix=mix, genres=genres
                    )

            mix.title = title
            mix.description = description
            mix.genre_id = genre_id

            try:
                db.session.commit()
                flash("Mix details updated successfully.")
                return redirect(url_for("admin_manage_mixes"))
            except Exception:
                db.session.rollback()
                app.logger.exception("Mix update failed")
                flash("Could not update the mix.")

        return render_template(
            "admin/edit_mix.html", mix=mix, genres=genres
        )

    @app.route("/admin/mixes/<int:mix_id>/toggle", methods=["POST"])
    @admin_required
    def admin_toggle_mix(mix_id):
        from models import Mix

        mix = db.session.get(Mix, mix_id)
        if not mix:
            abort(404)

        was_published = bool(mix.is_published)
        mix.is_published = not was_published
        db.session.commit()

        if not was_published and mix.is_published:
            try:
                send_mix_published_notification(mix)
            except Exception:
                app.logger.exception(
                    "Push notification failed after admin mix publication."
                )

        flash(
            "Mix published." if mix.is_published
            else "Mix unpublished."
        )
        return redirect(url_for("admin_manage_mixes"))

    @app.route("/admin/mixes/<int:mix_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_mix(mix_id):
        from models import Mix, MixEvent

        mix = db.session.get(Mix, mix_id)
        if not mix:
            abort(404)

        from audio_storage import filebase_enabled, delete_audio, delete_cover

        audio_filename = mix.audio_file
        cover_filename = mix.cover_image

        try:
            MixEvent.query.filter_by(
                mix_id=mix.id
            ).delete(synchronize_session=False)

            db.session.expire(mix, ["analytics_events"])
            db.session.delete(mix)
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Mix deletion failed")
            flash("Could not delete the mix.")
            return redirect(url_for("admin_manage_mixes"))

        def still_used(filename):
            return Mix.query.filter(
                or_(
                    Mix.audio_file == filename,
                    Mix.cover_image == filename
                )
            ).first() is not None

        def remove_local_file(filename):
            if not filename or still_used(filename):
                return

            upload_folder = os.path.realpath(
                app.config["UPLOAD_FOLDER"]
            )
            file_path = os.path.realpath(
                os.path.join(
                    upload_folder,
                    os.path.basename(filename)
                )
            )

            if os.path.commonpath(
                [upload_folder, file_path]
            ) != upload_folder:
                return

            if os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    app.logger.exception(
                        "Could not remove uploaded file: %s",
                        filename
                    )

        # Remove the MP3 from its actual storage location.
        if audio_filename and not still_used(audio_filename):
            if filebase_enabled():
                try:
                    delete_audio(audio_filename)
                except Exception:
                    app.logger.exception(
                        "Could not delete Filebase audio: %s",
                        audio_filename
                    )
            else:
                remove_local_file(audio_filename)

        # Prefer deleting a legacy local cover; new covers are in Filebase.
        if cover_filename and not still_used(cover_filename):
            local_cover_path = os.path.join(
                app.config["UPLOAD_FOLDER"],
                os.path.basename(cover_filename)
            )
            if os.path.isfile(local_cover_path):
                remove_local_file(cover_filename)
            elif filebase_enabled():
                try:
                    delete_cover(cover_filename)
                except Exception:
                    app.logger.exception(
                        "Could not delete Filebase cover: %s",
                        cover_filename
                    )
            else:
                remove_local_file(cover_filename)

        flash("Mix deleted successfully.")
        return redirect(url_for("admin_manage_mixes"))


    @app.route("/admin/bookings")
    @admin_required
    def admin_manage_bookings():
        from models import Booking, DJ

        status = request.args.get("status", "all")
        query = Booking.query

        allowed_statuses = [
            "pending", "confirmed", "completed", "cancelled"
        ]

        if status in allowed_statuses:
            query = query.filter_by(status=status)
        else:
            status = "all"

        bookings = query.order_by(
            Booking.created_at.desc()
        ).all()

        counts = {
            item: Booking.query.filter_by(status=item).count()
            for item in allowed_statuses
        }

        djs = DJ.query.filter(
            or_(
                DJ.is_admin.is_(True),
                DJ.is_approved.is_(True)
            )
        ).order_by(DJ.display_name, DJ.username).all()

        return render_template(
            "admin/bookings.html",
            bookings=bookings,
            status=status,
            counts=counts,
            djs=djs
        )


    @app.route("/admin/bookings/<int:booking_id>/assign", methods=["POST"])
    @admin_required
    def admin_assign_booking(booking_id):
        from models import Booking, DJ

        booking = db.session.get(Booking, booking_id)
        if not booking:
            abort(404)

        dj_value = request.form.get("dj_id", "").strip()

        if not dj_value:
            booking.dj_id = None
        else:
            try:
                dj_id = int(dj_value)
            except ValueError:
                flash("Invalid DJ selection.")
                return redirect(url_for("admin_manage_bookings"))

            dj = DJ.query.filter(
                DJ.id == dj_id,
                or_(
                    DJ.is_admin.is_(True),
                    DJ.is_approved.is_(True)
                )
            ).first()

            if not dj:
                flash("That DJ is unavailable or suspended.")
                return redirect(url_for("admin_manage_bookings"))

            booking.dj_id = dj.id

        db.session.commit()
        flash("Booking assignment updated successfully.")
        return redirect(url_for("admin_manage_bookings"))


    @app.route(
        "/admin/bookings/<int:booking_id>/status",
        methods=["POST"]
    )
    @admin_required
    def admin_update_booking_status(booking_id):
        from models import Booking

        booking = db.session.get(Booking, booking_id)

        if not booking:
            abort(404)

        new_status = request.form.get("status", "").strip().lower()

        allowed_statuses = {
            "pending", "confirmed", "completed", "cancelled"
        }

        if new_status not in allowed_statuses:
            flash("Invalid booking status.")
        else:
            booking.status = new_status
            db.session.commit()
            flash("Booking status updated successfully.")

        return redirect(url_for("admin_manage_bookings"))


    @app.route("/admin/calendar")
    @admin_required
    def admin_booking_calendar():
        import calendar
        from models import Booking

        today = date.today()

        try:
            year = int(request.args.get("year", today.year))
            month = int(request.args.get("month", today.month))
            if not 1 <= month <= 12 or not 2000 <= year <= 2100:
                raise ValueError
        except (ValueError, TypeError):
            year, month = today.year, today.month

        first_day = date(year, month, 1)

        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)

        last_day = next_month - __import__("datetime").timedelta(days=1)

        previous_month = (
            date(year - 1, 12, 1) if month == 1
            else date(year, month - 1, 1)
        )

        bookings = Booking.query.filter(
            Booking.status == "confirmed",
            Booking.event_date >= first_day,
            Booking.event_date < next_month
        ).order_by(Booking.event_date.asc()).all()

        events_by_day = {}
        for booking in bookings:
            events_by_day.setdefault(booking.event_date, []).append(booking)

        weeks = []
        for week in calendar.monthcalendar(year, month):
            weeks.append([
                date(year, month, day) if day else None
                for day in week
            ])

        return render_template(
            "admin/calendar.html",
            year=year,
            month=month,
            month_name=calendar.month_name[month],
            weeks=weeks,
            events_by_day=events_by_day,
            today=today,
            previous_month=previous_month,
            next_month=next_month,
            booking_count=len(bookings)
        )


    @app.route("/dj/bookings")
    @dj_required
    def dj_manage_bookings():
        from models import Booking

        status = request.args.get("status", "all")
        allowed_statuses = [
            "pending", "confirmed", "completed", "cancelled"
        ]

        query = Booking.query.filter_by(dj_id=current_user.id)

        if status in allowed_statuses:
            query = query.filter_by(status=status)
        else:
            status = "all"

        bookings = query.order_by(Booking.created_at.desc()).all()

        counts = {
            item: Booking.query.filter_by(
                dj_id=current_user.id,
                status=item
            ).count()
            for item in allowed_statuses
        }

        return render_template(
            "dj/bookings.html",
            bookings=bookings,
            status=status,
            counts=counts,
            dj=current_user
        )


    @app.route("/dj/bookings/<int:booking_id>/status", methods=["POST"])
    @dj_required
    def dj_update_booking_status(booking_id):
        from models import Booking

        booking = Booking.query.filter_by(
            id=booking_id,
            dj_id=current_user.id
        ).first_or_404()

        new_status = request.form.get("status", "").strip().lower()
        allowed_statuses = {
            "pending", "confirmed", "completed", "cancelled"
        }

        if new_status not in allowed_statuses:
            flash("Invalid booking status.")
        else:
            booking.status = new_status
            db.session.commit()
            flash("Booking status updated successfully.")

        return redirect(url_for("dj_manage_bookings"))


    @app.route("/dj/calendar")
    @dj_required
    def dj_booking_calendar():
        import calendar
        from models import Booking

        today = date.today()

        try:
            year = int(request.args.get("year", today.year))
            month = int(request.args.get("month", today.month))
            if not 1 <= month <= 12 or not 2000 <= year <= 2100:
                raise ValueError
        except (ValueError, TypeError):
            year, month = today.year, today.month

        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)

        previous_month = (
            date(year - 1, 12, 1) if month == 1
            else date(year, month - 1, 1)
        )

        bookings = Booking.query.filter(
            Booking.dj_id == current_user.id,
            Booking.status == "confirmed",
            Booking.event_date >= date(year, month, 1),
            Booking.event_date < next_month
        ).order_by(Booking.event_date.asc()).all()

        events_by_day = {}
        for booking in bookings:
            events_by_day.setdefault(booking.event_date, []).append(booking)

        weeks = [
            [
                date(year, month, day) if day else None
                for day in week
            ]
            for week in calendar.monthcalendar(year, month)
        ]

        return render_template(
            "dj/calendar.html",
            year=year,
            month=month,
            month_name=calendar.month_name[month],
            weeks=weeks,
            events_by_day=events_by_day,
            today=today,
            previous_month=previous_month,
            next_month=next_month,
            booking_count=len(bookings),
            dj=current_user
        )


    @app.route("/admin/ads")
    @admin_required
    def admin_manage_ads():
        from models import Advertisement

        ads = Advertisement.query.order_by(
            Advertisement.created_at.desc()
        ).all()

        return render_template(
            "admin/ads.html",
            ads=ads,
            today=date.today()
        )


    @app.route("/admin/ads/new", methods=["GET", "POST"])
    @admin_required
    def admin_create_ad():
        from models import Advertisement
        from urllib.parse import urlparse

        if request.method == "POST":
            advertiser = request.form.get(
                "advertiser_name", ""
            ).strip()
            title = request.form.get("title", "").strip()
            target_url = request.form.get("target_url", "").strip()
            image = request.files.get("image")
            start_value = request.form.get("start_date", "").strip()
            end_value = request.form.get("end_date", "").strip()

            if not advertiser or not title:
                flash("Advertiser name and title are required.")
                return redirect(url_for("admin_create_ad"))

            parsed_url = urlparse(target_url)
            if (
                parsed_url.scheme not in ("http", "https")
                or not parsed_url.netloc
            ):
                flash("Enter a valid HTTP or HTTPS promotional URL.")
                return redirect(url_for("admin_create_ad"))

            if not image or not image.filename:
                flash("Please select a banner image.")
                return redirect(url_for("admin_create_ad"))

            safe_name = secure_filename(image.filename)
            extension = os.path.splitext(safe_name)[1].lower()

            if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                flash("Banner must be JPG, JPEG, PNG or WEBP.")
                return redirect(url_for("admin_create_ad"))

            try:
                start_date = date.fromisoformat(start_value) if start_value else None
                end_date = date.fromisoformat(end_value) if end_value else None
            except ValueError:
                flash("Please enter valid campaign dates.")
                return redirect(url_for("admin_create_ad"))

            if start_date and end_date and end_date < start_date:
                flash("End date cannot be before start date.")
                return redirect(url_for("admin_create_ad"))

            from audio_storage import (
                filebase_enabled, upload_image, delete_image
            )

            filename = f"{uuid4().hex}{extension}"
            relative_path = f"ads/{filename}"
            full_path = None
            image_uploaded_remote = False

            try:
                if filebase_enabled():
                    upload_image(image.stream, relative_path)
                    image_uploaded_remote = True
                else:
                    ads_folder = os.path.join(
                        app.config["UPLOAD_FOLDER"], "ads"
                    )
                    os.makedirs(ads_folder, exist_ok=True)
                    full_path = os.path.join(ads_folder, filename)
                    image.save(full_path)

                ad = Advertisement(
                    advertiser_name=advertiser,
                    title=title,
                    image_file=relative_path,
                    target_url=target_url,
                    start_date=start_date,
                    end_date=end_date,
                    is_active=True
                )

                db.session.add(ad)
                db.session.commit()

                flash("Advertisement created successfully.")
                return redirect(url_for("admin_manage_ads"))

            except Exception:
                db.session.rollback()

                if image_uploaded_remote:
                    try:
                        delete_image(relative_path)
                    except Exception:
                        app.logger.exception(
                            "Could not remove failed advertisement image upload"
                        )

                if full_path and os.path.isfile(full_path):
                    try:
                        os.remove(full_path)
                    except OSError:
                        app.logger.exception(
                            "Could not remove failed advertisement image"
                        )

                app.logger.exception("Advertisement creation failed")
                flash("Could not save the advertisement.")

        return render_template(
            "admin/ad_form.html",
            ad=None,
            is_edit=False
        )



    @app.route("/admin/ads/<int:ad_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_edit_ad(ad_id):
        from models import Advertisement
        from urllib.parse import urlparse

        ad = db.session.get(Advertisement, ad_id)
        if not ad:
            abort(404)

        if request.method == "POST":
            advertiser = request.form.get("advertiser_name", "").strip()
            title = request.form.get("title", "").strip()
            target_url = request.form.get("target_url", "").strip()
            image = request.files.get("image")
            start_value = request.form.get("start_date", "").strip()
            end_value = request.form.get("end_date", "").strip()

            if not advertiser or not title:
                flash("Advertiser name and title are required.")
                return redirect(url_for("admin_edit_ad", ad_id=ad_id))

            parsed_url = urlparse(target_url)
            if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
                flash("Enter a valid HTTP or HTTPS promotional URL.")
                return redirect(url_for("admin_edit_ad", ad_id=ad_id))

            try:
                start_date = date.fromisoformat(start_value) if start_value else None
                end_date = date.fromisoformat(end_value) if end_value else None
            except ValueError:
                flash("Please enter valid campaign dates.")
                return redirect(url_for("admin_edit_ad", ad_id=ad_id))

            if start_date and end_date and end_date < start_date:
                flash("End date cannot be before start date.")
                return redirect(url_for("admin_edit_ad", ad_id=ad_id))

            from audio_storage import (
                filebase_enabled, upload_image, delete_image
            )

            new_path = None
            new_full_path = None
            new_image_uploaded_remote = False
            old_image = ad.image_file

            try:
                if image and image.filename:
                    safe_name = secure_filename(image.filename)
                    extension = os.path.splitext(safe_name)[1].lower()

                    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                        flash("Banner must be JPG, JPEG, PNG or WEBP.")
                        return redirect(url_for("admin_edit_ad", ad_id=ad_id))

                    filename = f"{uuid4().hex}{extension}"
                    new_path = f"ads/{filename}"

                    if filebase_enabled():
                        upload_image(image.stream, new_path)
                        new_image_uploaded_remote = True
                    else:
                        ads_folder = os.path.join(
                            app.config["UPLOAD_FOLDER"], "ads"
                        )
                        os.makedirs(ads_folder, exist_ok=True)
                        new_full_path = os.path.join(ads_folder, filename)
                        image.save(new_full_path)

                    ad.image_file = new_path

                ad.advertiser_name = advertiser
                ad.title = title
                ad.target_url = target_url
                ad.start_date = start_date
                ad.end_date = end_date

                db.session.commit()

            except Exception:
                db.session.rollback()

                if new_image_uploaded_remote and new_path:
                    try:
                        delete_image(new_path)
                    except Exception:
                        app.logger.exception(
                            "Could not remove failed advertisement image upload"
                        )

                if new_full_path and os.path.isfile(new_full_path):
                    try:
                        os.remove(new_full_path)
                    except OSError:
                        app.logger.exception(
                            "Could not remove failed advertisement image"
                        )

                app.logger.exception("Advertisement update failed")
                flash("Could not update the advertisement.")
                return redirect(url_for("admin_edit_ad", ad_id=ad_id))

            if new_path and old_image != new_path:
                if old_image and old_image.startswith("ads/") and filebase_enabled():
                    try:
                        image_in_use = Advertisement.query.filter(
                            Advertisement.image_file == old_image,
                            Advertisement.id != ad.id,
                        ).first()

                        if not image_in_use:
                            delete_image(old_image)
                    except Exception:
                        app.logger.exception(
                            "Could not remove old advertisement image from Filebase"
                        )

                upload_folder = os.path.realpath(
                    app.config["UPLOAD_FOLDER"]
                )
                old_full_path = os.path.realpath(
                    os.path.join(upload_folder, old_image or "")
                )

                if (
                    os.path.commonpath([upload_folder, old_full_path])
                    == upload_folder
                    and os.path.isfile(old_full_path)
                ):
                    try:
                        os.remove(old_full_path)
                    except OSError:
                        app.logger.exception(
                            "Could not remove old advertisement image"
                        )

            flash("Advertisement updated successfully.")
            return redirect(url_for("admin_manage_ads"))

        return render_template(
            "admin/ad_form.html",
            ad=ad,
            is_edit=True
        )


    @app.route("/admin/ads/<int:ad_id>/toggle", methods=["POST"])
    @admin_required
    def admin_toggle_ad(ad_id):
        from models import Advertisement

        ad = db.session.get(Advertisement, ad_id)
        if not ad:
            abort(404)

        ad.is_active = not ad.is_active
        db.session.commit()

        flash("Advertisement status updated.")
        return redirect(url_for("admin_manage_ads"))


    @app.route("/admin/ads/<int:ad_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_ad(ad_id):
        from models import Advertisement
        from audio_storage import filebase_enabled, delete_image

        ad = db.session.get(Advertisement, ad_id)
        if not ad:
            abort(404)

        filename = ad.image_file

        try:
            db.session.delete(ad)
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Advertisement deletion failed")
            flash("Could not delete the advertisement.")
            return redirect(url_for("admin_manage_ads"))

        if filename and filename.startswith("ads/") and filebase_enabled():
            try:
                image_in_use = Advertisement.query.filter(
                    Advertisement.image_file == filename,
                    Advertisement.id != ad_id,
                ).first()

                if not image_in_use:
                    delete_image(filename)
            except Exception:
                app.logger.exception(
                    "Could not remove advertisement image from Filebase"
                )

        upload_folder = os.path.realpath(app.config["UPLOAD_FOLDER"])
        file_path = os.path.realpath(
            os.path.join(upload_folder, filename or "")
        )

        if (
            os.path.commonpath([upload_folder, file_path]) == upload_folder
            and os.path.isfile(file_path)
        ):
            try:
                os.remove(file_path)
            except OSError:
                app.logger.exception("Could not remove advertisement image")

        flash("Advertisement deleted.")
        return redirect(url_for("admin_manage_ads"))


    @app.route("/ad/<int:ad_id>/click")
    def advertisement_click(ad_id):
        from models import Advertisement

        ad = db.session.get(Advertisement, ad_id)
        if not ad or not ad.is_active:
            abort(404)

        today = date.today()
        if (
            (ad.start_date and today < ad.start_date)
            or (ad.end_date and today > ad.end_date)
        ):
            abort(404)

        ad.clicks += 1
        db.session.commit()

        return redirect(ad.target_url)



    @app.route("/admin/revenue")
    @admin_required
    def admin_revenue():
        from models import (
            Advertisement,
            AdvertisingContract,
            AdvertisingPayment
        )
        from decimal import Decimal

        ads = Advertisement.query.order_by(
            Advertisement.created_at.desc()
        ).all()

        contracts = {
            item.ad_id: item
            for item in AdvertisingContract.query.all()
        }

        payment_rows = AdvertisingPayment.query.order_by(
            AdvertisingPayment.paid_on.desc(),
            AdvertisingPayment.id.desc()
        ).all()

        payments = {}
        received_by_ad = {}

        for payment in payment_rows:
            payments.setdefault(payment.ad_id, []).append(payment)
            received_by_ad[payment.ad_id] = (
                received_by_ad.get(payment.ad_id, Decimal("0.00"))
                + payment.amount
            )

        total_agreed = sum(
            (item.agreed_amount for item in contracts.values()),
            Decimal("0.00")
        )
        total_received = sum(
            received_by_ad.values(),
            Decimal("0.00")
        )

        return render_template(
            "admin/revenue.html",
            ads=ads,
            contracts=contracts,
            payments=payments,
            received_by_ad=received_by_ad,
            total_agreed=total_agreed,
            total_received=total_received,
            total_outstanding=total_agreed - total_received,
            today=date.today()
        )


    @app.route(
        "/admin/revenue/campaign/<int:ad_id>/fee",
        methods=["POST"]
    )
    @admin_required
    def admin_revenue_set_fee(ad_id):
        from models import Advertisement, AdvertisingContract
        from decimal import Decimal, InvalidOperation

        ad = db.session.get(Advertisement, ad_id)
        if not ad:
            abort(404)

        try:
            amount = Decimal(
                request.form.get("agreed_amount", "").strip()
            )
            if not amount.is_finite() or amount < 0:
                raise ValueError()
        except (InvalidOperation, ValueError):
            flash("Enter a valid agreed amount of zero or more.")
            return redirect(url_for("admin_revenue"))

        contract = AdvertisingContract.query.filter_by(
            ad_id=ad_id
        ).first()

        if contract is None:
            contract = AdvertisingContract(
                ad_id=ad_id,
                agreed_amount=amount
            )
            db.session.add(contract)
        else:
            contract.agreed_amount = amount

        try:
            db.session.commit()
            flash("Campaign agreed fee saved.")
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not save advertising fee")
            flash("Could not save the agreed fee.")

        return redirect(url_for("admin_revenue"))


    @app.route(
        "/admin/revenue/campaign/<int:ad_id>/payment",
        methods=["POST"]
    )
    @admin_required
    def admin_revenue_add_payment(ad_id):
        from models import (
            Advertisement,
            AdvertisingContract,
            AdvertisingPayment
        )
        from decimal import Decimal, InvalidOperation
        from datetime import date

        ad = db.session.get(Advertisement, ad_id)
        if not ad:
            abort(404)

        contract = AdvertisingContract.query.filter_by(
            ad_id=ad_id
        ).first()

        if contract is None:
            flash("Set the agreed campaign fee before recording a payment.")
            return redirect(url_for("admin_revenue"))

        try:
            amount = Decimal(request.form.get("amount", "").strip())
            if not amount.is_finite() or amount <= 0:
                raise ValueError()

            paid_on_value = request.form.get("paid_on", "").strip()
            paid_on = (
                date.fromisoformat(paid_on_value)
                if paid_on_value else date.today()
            )
        except (InvalidOperation, ValueError):
            flash("Enter a valid positive amount and payment date.")
            return redirect(url_for("admin_revenue"))

        method = request.form.get("method", "").strip()
        allowed_methods = {"M-PESA", "Bank", "Cash", "Other"}

        if method not in allowed_methods:
            flash("Select a valid payment method.")
            return redirect(url_for("admin_revenue"))

        received = db.session.query(
            db.func.coalesce(
                db.func.sum(AdvertisingPayment.amount), 0
            )
        ).filter(
            AdvertisingPayment.ad_id == ad_id
        ).scalar()

        outstanding = contract.agreed_amount - Decimal(str(received))

        if amount > outstanding:
            flash(
                "Payment exceeds the outstanding balance. "
                "Check the agreed fee and previous payments."
            )
            return redirect(url_for("admin_revenue"))

        payment = AdvertisingPayment(
            ad_id=ad_id,
            amount=amount,
            method=method,
            reference=request.form.get("reference", "").strip()[:120] or None,
            notes=request.form.get("notes", "").strip()[:500] or None,
            paid_on=paid_on
        )

        try:
            db.session.add(payment)
            db.session.commit()
            flash("Advertiser payment recorded successfully.")
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not record advertiser payment")
            flash("Could not record the payment.")

        return redirect(url_for("admin_revenue"))


    @app.route("/health")
    def health():
        try:
            with db.engine.connect() as connection:
                schema = connection.exec_driver_sql(
                    "SELECT current_schema()"
                ).scalar()

            return jsonify({
                "status": "ok",
                "database": "connected",
                "schema": schema
            })
        except Exception:
            app.logger.exception("Database connection failed")
            return jsonify({
                "status": "error",
                "database": "disconnected"
            }), 500

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
