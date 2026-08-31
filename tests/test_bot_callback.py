import asyncio
from types import SimpleNamespace

from packtogether.bot import callback


class DummyMessage:
    def __init__(self):
        self.deleted = False
        self.replies = []

    async def delete(self):
        self.deleted = True

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class DummyQuery:
    def __init__(self, data: str):
        self.data = data
        self.answers = []
        self.edits = []
        self.message = DummyMessage()

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class ServiceStub:
    def __init__(self, chat_id: int = -100):
        self.chat_id = chat_id

    def trip_by_id(self, trip_id: int):
        return {"id": trip_id, "chat_id": self.chat_id, "name": "سفر تست", "status": "packing"}


class BrokenServiceStub(ServiceStub):
    def trip_by_id(self, trip_id: int):
        raise RuntimeError("boom")


def _context(service):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"service": service}), user_data={})


def _update(query, chat_id: int = -100):
    return SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=1, first_name="علی"),
    )


def test_callback_noop_is_answered_without_crashing():
    query = DummyQuery("noop")
    asyncio.run(callback(_update(query), _context(ServiceStub())))
    assert query.answers, "noop callback must be answered to stop Telegram loading spinner"


def test_cancel_start_has_context_specific_text():
    query = DummyQuery("cancel_start:1")
    asyncio.run(callback(_update(query), _context(ServiceStub())))
    assert query.edits
    assert query.edits[-1][0] == "↩️ شروع فوری سفر لغو شد."


def test_cancel_delete_trip_has_context_specific_text():
    query = DummyQuery("cancel_delete_trip:1")
    asyncio.run(callback(_update(query), _context(ServiceStub())))
    assert query.edits
    assert query.edits[-1][0] == "↩️ حذف سفر لغو شد."


def test_callback_unknown_failure_returns_alert():
    query = DummyQuery("item:1:99")
    asyncio.run(callback(_update(query), _context(BrokenServiceStub())))
    assert query.answers
    assert query.answers[-1]["show_alert"] is True
    assert "خطایی رخ داد" in (query.answers[-1]["text"] or "")
