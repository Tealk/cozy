from peewee import BlobField, BooleanField, CharField, FloatField, IntegerField, TextField

from cozy.db.model_base import ModelBase


class Book(ModelBase):
    name = CharField()
    author = CharField()
    reader = CharField()
    position = IntegerField()
    rating = IntegerField()
    cover = BlobField(null=True)
    playback_speed = FloatField(default=1.0)
    last_played = IntegerField(default=0)
    offline = BooleanField(default=False)
    downloaded = BooleanField(default=False)
    hidden = BooleanField(default=False)
    series = CharField(null=True)
    series_part = FloatField(null=True)
    description = TextField(null=True)
    publisher = CharField(null=True)
    published_year = IntegerField(null=True)
    language = CharField(null=True)
    asin = CharField(null=True)
    metadata_json = TextField(null=True)
