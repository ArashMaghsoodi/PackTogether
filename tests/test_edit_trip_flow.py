import asyncio
from types import SimpleNamespace

import pytest

from packtogether.bot import parse_trip_edit_message, text_message


def test_parse_trip_edit_message_requires_three_lines():
    with pytest.raises(ValueError, match="فرمت ویرایش نادرست است"):
        parse_trip_edit_message("سفر مازندران 1405.11.6 12:00")


def test_parse_trip_edit_message_accepts_exact_three_lines():
    name, departure = parse_trip_edit_message("سفر تبریز\n1405.06.17\n14:30")
    assert name == "سفر تبریز"
    assert departure.endswith("+00:00")


class DummyMessage:
    def __init__(self, text):
        self.text = text
        self.sent = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))


class ServiceStub:
    def __init__(self):
        self.updated = []

    def setup(self, chat_id, user_id):
        return None

    def trip_by_id(self, trip_id):
        return {
            "id": trip_id,
            "chat_id": -100,
            "name": "تست ۶",
            "status": "packing",
            "message_id": None,
            "departure_at": "2099-01-01T00:00:00+00:00",
            "timezone": "Asia/Tehran",
        }

    def update_trip(self, trip_id, name, departure_at, actor_display_name):
        self.updated.append((trip_id, name, departure_at, actor_display_name))

    def page(self, trip_id, page=0):
        return [], 0, 1

    def progress(self, trip_id):
        return 0, 0

    def attach_message(self, trip_id, message_id):
        return


class DummyBot:
    async def send_message(self, chat_id, text, reply_markup=None):
        return SimpleNamespace(message_id=123)


class DummyUpdate:
    def __init__(self, text):
        self.effective_chat = SimpleNamespace(id=-100)
        self.effective_user = SimpleNamespace(id=1, first_name="علی")
        self.effective_message = DummyMessage(text)
        self.callback_query = None

    def get_bot(self):
        return DummyBot()


class DummyContext:
    def __init__(self, service):
        self.application = SimpleNamespace(bot_data={"service": service})
        self.user_data = {"edit_trip": 6}


def test_text_message_edit_trip_sends_success_message():
    service = ServiceStub()
    context = DummyContext(service)
    update = DummyUpdate("سفر تبریز\n1405.06.17\n14:30")

    asyncio.run(text_message(update, context))

    assert service.updated
    assert any("با موفقیت ویرایش شد" in sent[0] for sent in update.effective_message.sent)
    assert "edit_trip" not in context.user_data
