from datetime import datetime
from flask_login import UserMixin
from sqlalchemy import Index
from extensions import db


class DJ(UserMixin, db.Model):
    __tablename__ = "djs"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(120))
    bio = db.Column(db.Text)
    profile_image = db.Column(db.String(255))
    youtube_url = db.Column(db.String(500))
    instagram_url = db.Column(db.String(500))
    tiktok_url = db.Column(db.String(500))
    facebook_url = db.Column(db.String(500))

    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_approved = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    mixes = db.relationship("Mix", back_populates="dj")
    bookings = db.relationship("Booking", back_populates="dj")


class Genre(db.Model):
    __tablename__ = "genres"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

    mixes = db.relationship("Mix", back_populates="genre")


class Mix(db.Model):
    __tablename__ = "mixes"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text)
    audio_file = db.Column(db.String(255), nullable=False)
    cover_image = db.Column(db.String(255))
    duration_seconds = db.Column(db.Integer)
    dj_id = db.Column(db.Integer, db.ForeignKey("djs.id"), nullable=False)
    genre_id = db.Column(db.Integer, db.ForeignKey("genres.id"))
    plays = db.Column(db.Integer, default=0, nullable=False)
    downloads = db.Column(db.Integer, default=0, nullable=False)
    is_published = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    dj = db.relationship("DJ", back_populates="mixes")
    genre = db.relationship("Genre", back_populates="mixes")

    __table_args__ = (
        Index("ix_mixes_created_at", "created_at"),
        Index("ix_mixes_title", "title"),
    )


class MixEvent(db.Model):
    __tablename__ = "mix_events"

    id = db.Column(db.Integer, primary_key=True)
    mix_id = db.Column(
        db.Integer,
        db.ForeignKey("mixes.id"),
        nullable=False,
        index=True
    )
    event_type = db.Column(
        db.String(20),
        nullable=False,
        index=True
    )
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )

    mix = db.relationship("Mix", backref="analytics_events")


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    dj_id = db.Column(
        db.Integer,
        db.ForeignKey("djs.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    dj = db.relationship("DJ", back_populates="bookings")

    client_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), nullable=False)
    email = db.Column(db.String(150))
    event_type = db.Column(db.String(80), nullable=False)
    event_date = db.Column(db.Date, nullable=False)
    venue = db.Column(db.String(200), nullable=False)
    budget = db.Column(db.Numeric(10, 2))
    message = db.Column(db.Text)
    status = db.Column(
        db.String(20),
        default="pending",
        nullable=False,
        index=True
    )
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )


class Advertisement(db.Model):
    __tablename__ = "advertisements"

    id = db.Column(db.Integer, primary_key=True)
    advertiser_name = db.Column(db.String(120), nullable=False)
    title = db.Column(db.String(180), nullable=False)
    image_file = db.Column(db.String(255), nullable=False)
    target_url = db.Column(db.String(1000), nullable=False)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    impressions = db.Column(db.Integer, default=0, nullable=False)
    clicks = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class Listener(db.Model):
    __tablename__ = "listeners"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(120))
    bio = db.Column(db.Text)
    profile_image = db.Column(db.String(255))
    is_public = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )
    instagram_url = db.Column(db.String(500))
    tiktok_url = db.Column(db.String(500))
    facebook_url = db.Column(db.String(500))
    youtube_url = db.Column(db.String(500))
    x_url = db.Column(db.String(500))
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )


class ListenerFavorite(db.Model):
    __tablename__ = "listener_favorites"

    id = db.Column(db.Integer, primary_key=True)
    listener_id = db.Column(
        db.Integer,
        db.ForeignKey("listeners.id", ondelete="CASCADE"),
        nullable=False
    )
    mix_id = db.Column(
        db.Integer,
        db.ForeignKey("mixes.id", ondelete="CASCADE"),
        nullable=False
    )
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    listener = db.relationship("Listener")
    mix = db.relationship("Mix")

    __table_args__ = (
        db.UniqueConstraint(
            "listener_id",
            "mix_id",
            name="uq_listener_favorite"
        ),
        Index("ix_listener_favorites_listener", "listener_id"),
    )


class AdvertisingContract(db.Model):
    __tablename__ = "advertising_contracts"

    id = db.Column(db.Integer, primary_key=True)
    ad_id = db.Column(
        db.Integer,
        db.ForeignKey("advertisements.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True
    )
    agreed_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )


class AdvertisingPayment(db.Model):
    __tablename__ = "advertising_payments"

    id = db.Column(db.Integer, primary_key=True)
    ad_id = db.Column(
        db.Integer,
        db.ForeignKey("advertisements.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    method = db.Column(db.String(30), nullable=False)
    reference = db.Column(db.String(120))
    notes = db.Column(db.String(500))
    paid_on = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PushSubscription(db.Model):
    __tablename__ = "push_subscriptions"

    id = db.Column(db.Integer, primary_key=True)

    endpoint = db.Column(
        db.Text,
        unique=True,
        nullable=False
    )

    p256dh = db.Column(db.Text, nullable=False)
    auth = db.Column(db.Text, nullable=False)

    listener_id = db.Column(
        db.Integer,
        db.ForeignKey("listeners.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )

    dj_id = db.Column(
        db.Integer,
        db.ForeignKey("djs.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )


class ListenerPlaylist(db.Model):
    __tablename__ = "listener_playlists"

    id = db.Column(db.Integer, primary_key=True)

    listener_id = db.Column(
        db.Integer,
        db.ForeignKey("listeners.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    name = db.Column(db.String(100), nullable=False)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    listener = db.relationship("Listener")

    items = db.relationship(
        "ListenerPlaylistItem",
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="ListenerPlaylistItem.position"
    )


class ListenerPlaylistItem(db.Model):
    __tablename__ = "listener_playlist_items"

    id = db.Column(db.Integer, primary_key=True)

    playlist_id = db.Column(
        db.Integer,
        db.ForeignKey("listener_playlists.id", ondelete="CASCADE"),
        nullable=False
    )

    mix_id = db.Column(
        db.Integer,
        db.ForeignKey("mixes.id", ondelete="CASCADE"),
        nullable=False
    )

    position = db.Column(db.Integer, default=0, nullable=False)

    added_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    playlist = db.relationship(
        "ListenerPlaylist",
        back_populates="items"
    )

    mix = db.relationship("Mix")

    __table_args__ = (
        db.UniqueConstraint(
            "playlist_id",
            "mix_id",
            name="uq_listener_playlist_mix"
        ),
        Index("ix_listener_playlist_items_playlist", "playlist_id"),
    )
