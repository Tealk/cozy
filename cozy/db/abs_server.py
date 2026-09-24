from peewee import CharField, ForeignKeyField, IntegerField

from cozy.db.book import Book
from cozy.db.model_base import ModelBase


class AudiobookshelfServer(ModelBase):
    name = CharField()
    url = CharField()
    library_id = CharField(default="")
    username = CharField(null=True)
    token = CharField(null=True)


class AudiobookshelfBook(ModelBase):
    server = ForeignKeyField(AudiobookshelfServer, backref="abs_books")
    book = ForeignKeyField(Book, unique=True)
    library_item_id = CharField()
    library_id = CharField(default="")
    updated_at = IntegerField(default=0)
