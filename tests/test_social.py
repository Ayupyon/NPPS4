import asyncio
import contextlib
import hashlib
import importlib
import json
import pathlib
import sqlite3
import tempfile
import types
import unittest
import unittest.mock

import alembic.migration
import alembic.operations
import fastapi
import httpx
import sqlalchemy
import sqlalchemy.ext.asyncio

import npps4.app.app as app
import npps4.config.config as config
import npps4.const as const
import npps4.db.main as main
import npps4.idol.core as core
import npps4.idol.error as error
import npps4.idol.session as session
import npps4.release_key as release_key
import npps4.system.museum as museum
import npps4.system.unit as unit
import npps4.system.unit_model as unit_model
import npps4.system.user as user
import npps4.util as util

REVISION = importlib.import_module("npps4.alembic.versions.2026_11_12_0400-6bfb0354a091_friends_and_mail")
NOW = 1794254400
LEGACY_SCHEMA = """
CREATE TABLE destiny_friend_mutex (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
INSERT INTO destiny_friend_mutex VALUES(1,7);
CREATE TABLE destiny_friend_pair (
    user_a INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    user_b INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    requester INTEGER NOT NULL, accepted INTEGER NOT NULL DEFAULT 0 CHECK(accepted IN (0,1)),
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    new_a INTEGER NOT NULL DEFAULT 0 CHECK(new_a IN (0,1)),
    new_b INTEGER NOT NULL DEFAULT 0 CHECK(new_b IN (0,1)),
    PRIMARY KEY(user_a,user_b), CHECK(user_a<user_b), CHECK(requester IN (user_a,user_b))
);
CREATE INDEX destiny_friend_b ON destiny_friend_pair(user_b,accepted);
CREATE TABLE destiny_mail(
    notice_id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL,
    message TEXT NOT NULL, created_at INTEGER NOT NULL, replied_notice_id INTEGER NOT NULL DEFAULT 0,
    read_at INTEGER, reply_flag INTEGER NOT NULL DEFAULT 0, sender_deleted INTEGER NOT NULL DEFAULT 0,
    receiver_deleted INTEGER NOT NULL DEFAULT 0, sender_profile TEXT NOT NULL, receiver_profile TEXT NOT NULL,
    request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, UNIQUE(sender_id,request_key)
);
CREATE INDEX destiny_mail_inbox ON destiny_mail(receiver_id,receiver_deleted,notice_id);
CREATE INDEX destiny_mail_sent ON destiny_mail(sender_id,sender_deleted,notice_id);
CREATE INDEX destiny_mail_rate ON destiny_mail(sender_id,created_at);
"""


class SocialTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = pathlib.Path(directory) / "main.sqlite3"
        with contextlib.closing(sqlite3.connect(self.path, isolation_level=None)) as database:
            database.execute("PRAGMA journal_mode=WAL")
        self.engine = sqlalchemy.ext.asyncio.create_async_engine(
            f"sqlite+aiosqlite:///{self.path}", connect_args={"autocommit": False, "timeout": 5}
        )
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = sqlalchemy.ext.asyncio.async_sessionmaker(self.engine, expire_on_commit=False)
        self.enterContext(unittest.mock.patch.object(main, "sessionmaker", self.sessions))
        self.enterContext(unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "friend_mail", True))
        self.enterContext(unittest.mock.patch.object(config, "contains_badwords", return_value=False))
        self.enterContext(unittest.mock.patch.object(core, "client_check", return_value=None))
        self.enterContext(unittest.mock.patch.object(release_key, "formatted", return_value=[]))
        self.enterContext(unittest.mock.patch.object(session, "try_cleanup_tokens"))
        self.enterContext(unittest.mock.patch.object(util, "log"))
        self.clock = self.enterContext(unittest.mock.patch.object(util, "time", return_value=NOW))
        self.enterContext(
            unittest.mock.patch.object(
                unit,
                "get_unit_info",
                return_value=types.SimpleNamespace(
                    attribute_id=1, rarity=1, rank_max=2, max_removable_skill_capacity=1
                ),
            )
        )
        self.enterContext(
            unittest.mock.patch.object(unit, "get_unit_rarity", return_value=types.SimpleNamespace(before_level_max=10))
        )
        self.enterContext(
            unittest.mock.patch.object(
                museum,
                "get_museum_info_data",
                return_value=museum.MuseumInfoData(parameter=museum.MuseumParameterData(), contents_id_list=[]),
            )
        )

        async def get_unit_data(context: session.BasicSchoolIdolContext, card: main.Unit):
            info = unit_model.UnitInfoData(
                unit_id=card.unit_id,
                unit_owning_user_id=card.id,
                exp=0,
                next_exp=10,
                level=1,
                level_limit_id=0,
                max_level=10,
                rank=1,
                max_rank=2,
                love=0,
                max_love=20,
                unit_skill_level=1,
                max_hp=3,
                favorite_flag=False,
                display_rank=1,
                unit_skill_exp=0,
                unit_removable_skill_capacity=1,
                is_love_max=False,
                is_level_max=False,
                is_rank_max=False,
                is_signed=False,
                is_skill_level_max=False,
                is_removable_skill_capacity_max=True,
            )
            return info, types.SimpleNamespace(smile=10, pure=10, cool=10, level=1, next_exp=10)

        self.enterContext(unittest.mock.patch.object(unit, "get_unit_data_full_info", get_unit_data))
        async with self.engine.begin() as connection:
            metadata = main.User.metadata
            excluded = {REVISION.MUTEX, REVISION.PAIR, REVISION.MAIL}
            await connection.run_sync(
                metadata.create_all, tables=[table for table in metadata.sorted_tables if table.name not in excluded]
            )
        async with self.sessions.begin() as database:
            players = [
                main.User(
                    name=f"Player {index}",
                    invite_code=f"{index:09d}",
                    friend_max=1 if index == 2 else 10,
                    update_date=NOW - 3600,
                )
                for index in (1, 2, 3)
            ]
            database.add_all(players)
            await database.flush()
            self.alice, self.bob, self.carol = (player.id for player in players)
            for player in players:
                card = main.Unit(
                    user_id=player.id,
                    unit_id=1,
                    active=True,
                    max_level=10,
                    rank=1,
                    display_rank=1,
                    unit_removable_skill_capacity=1,
                )
                database.add(card)
                await database.flush()
                player.center_unit_owning_user_id = card.id
                database.add(
                    main.UnitDeck(user_id=player.id, name="Deck", deck_number=1, unit_owning_user_id_5=card.id)
                )
            await database.flush()
            await database.execute(sqlalchemy.update(main.User).values(update_date=NOW - 3600))
        self.tokens: dict[int, str] = {}
        for player in players:
            async with session.BasicSchoolIdolContext() as context:
                self.tokens[player.id] = await session.encapsulate_token(context, b"s" * 32, b"c" * 32, player.id)
                await context.db.main.execute(
                    sqlalchemy.update(main.Session)
                    .where(main.Session.user_id == player.id)
                    .values(last_accessed=NOW - 60)
                )

        importlib.import_module("npps4.game")
        application = fastapi.FastAPI()
        application.include_router(app.main)
        self.client = await self.enterAsyncContext(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://test/main.php/",
                headers={"Client-Version": "59.4", "LANG": "jp", "Platform-Type": "2", "X-Message-Code": "test"},
            )
        )

    async def migrate(self) -> None:
        def upgrade(connection: sqlalchemy.Connection) -> None:
            context = alembic.migration.MigrationContext.configure(connection)
            with alembic.operations.Operations.context(context):
                REVISION.upgrade()

        async with self.engine.begin() as connection:
            await connection.run_sync(upgrade)

    async def post(self, endpoint: str, actor: int, **request: object):
        data = {"request_data": json.dumps(request)} if request else None
        response = await self.client.post(
            endpoint, data=data, headers={"Authorize": f"consumerKey=lovelive_test&token={self.tokens[actor]}"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_friend_capacity_new_flags_and_consistent_profile(self):
        await self.migrate()
        for actor in (self.alice, self.carol):
            result = await self.post("friend/request", actor, user_id=self.bob)
            self.assertEqual(result["response_data"], {"is_friend": False})
        top = (await self.post("login/topInfo", self.bob))["response_data"]
        self.assertEqual((top["friends_approval_wait_cnt"], top["friend_action_cnt"]), (2, 2))
        self.clock.return_value += 1
        results = await asyncio.gather(
            self.post("friend/response", self.bob, user_id=self.alice, status=2),
            self.post("friend/response", self.bob, user_id=self.carol, status=2),
        )
        self.assertEqual(sorted(result["status_code"] for result in results), [200, 600])
        self.assertIn(
            error.ERROR_CODE_FRIEND_COUNT_OVER_LIMIT,
            [result["response_data"].get("error_code") for result in results if result["status_code"] != 200],
        )
        first = (await self.post("friend/list", self.bob, type=0, sort=10, page=0))["response_data"]
        accepted = first["friend_list"][0]
        self.assertTrue(accepted["is_new"])
        self.assertEqual(first["item_count"], 1)
        second = (await self.post("friend/list", self.bob, type=0, sort=10, page=0))["response_data"]
        self.assertFalse(second["friend_list"][0]["is_new"])
        winner = accepted["user_data"]["user_id"]
        search = (await self.post("friend/search", winner, invite_code="000000002"))["response_data"]
        profile = (await self.post("profile/profileInfo", winner, user_id=self.bob))["response_data"]
        self.assertEqual(search["user_info"]["user_id"], self.bob)
        self.assertEqual(search["friend_status"], const.FRIEND_STATUS.FRIEND)
        self.assertEqual(profile["friend_status"], search["friend_status"])
        self.assertEqual(profile["user_info"]["elapsed_time_from_login"], "1 hr ago")
        self.assertEqual(
            search["user_info"]["elapsed_time_from_login"], profile["user_info"]["elapsed_time_from_login"]
        )
        self.assertEqual((await self.post("friend/expel", winner, user_id=self.bob))["response_data"], [])
        self.assertEqual((await self.post("friend/expel", winner, user_id=self.bob))["response_data"], [])

    async def test_mail_retry_ownership_reply_read_and_soft_delete(self):
        await self.migrate()
        await self.post("friend/request", self.alice, user_id=self.bob)
        await self.post("friend/request", self.bob, user_id=self.alice)
        self.assertEqual(
            (await self.post("greet/user", self.carol, to_user_id=self.bob, message="Hi", timeStamp=NOW, commandNum=1))[
                "response_data"
            ]["error_code"],
            error.ERROR_CODE_FRIEND_SPECIFIED_USER_IS_NOT_A_FRIEND,
        )
        request = {"to_user_id": self.bob, "message": "Hello\r\nWorld", "timeStamp": NOW, "commandNum": "test.123.1"}
        sends = await asyncio.gather(
            self.post("greet/user", self.alice, **request), self.post("greet/user", self.alice, **request)
        )
        self.assertEqual([result["status_code"] for result in sends], [200, 200])
        changed = (await self.post("greet/user", self.alice, **(request | {"message": "Different"})))["response_data"]
        self.assertEqual(changed["detail"], "Send identity reused for different message")
        limited = (await self.post("greet/user", self.alice, **(request | {"commandNum": 2})))["response_data"]
        self.assertEqual(limited["error_code"], error.ERROR_CODE_FRIEND_OVER_REQUEST_COUNT)
        top = (await self.post("login/topInfo", self.bob))["response_data"]
        self.assertEqual(top["friend_greet_cnt"], 1)
        self.assertEqual(top["notice_mail_datetime"], util.timestamp_to_datetime(NOW))

        inbox = (await self.post("notice/noticeFriendGreeting", self.bob, next_id=0))["response_data"]["notice_list"]
        self.assertEqual(len(inbox), 1)
        self.assertFalse(inbox[0]["readed"])
        self.assertEqual(inbox[0]["message"], "Hello\nWorld")
        self.assertEqual(inbox[0]["affector"]["user_data"]["user_id"], self.alice)
        notice_id = inbox[0]["notice_id"]
        delete = (await self.post("greet/delete", self.carol, mail_notice_id=notice_id, is_send_mail=False))[
            "response_data"
        ]
        self.assertEqual(delete["error_code"], error.ERROR_CODE_FRIEND_SPECIFIED_USER_IS_NOT_A_FRIEND)
        self.assertEqual(
            (
                await self.post(
                    "greet/user",
                    self.bob,
                    to_user_id=self.alice,
                    message="Reply",
                    timeStamp=NOW,
                    commandNum=1,
                    replied_notice_id=notice_id,
                )
            )["status_code"],
            200,
        )
        inbox = (await self.post("notice/noticeFriendGreeting", self.bob, next_id=0))["response_data"]["notice_list"]
        self.assertTrue(inbox[0]["readed"])
        self.assertTrue(inbox[0]["reply_flag"])
        await self.post("greet/delete", self.bob, mail_notice_id=notice_id, is_send_mail=False)
        self.assertEqual(
            (await self.post("notice/noticeFriendGreeting", self.bob, next_id=0))["response_data"]["notice_list"], []
        )
        sent = (await self.post("notice/noticeUserGreetingHistory", self.alice, notice_id=0))["response_data"][
            "notice_list"
        ]
        self.assertEqual(sent[0]["notice_id"], notice_id)
        self.assertEqual(sent[0]["receiver"]["user_data"]["user_id"], self.bob)
        await self.post("greet/delete", self.alice, mail_notice_id=notice_id, is_send_mail=True)
        self.assertEqual(
            (await self.post("notice/noticeUserGreetingHistory", self.alice, notice_id=0))["response_data"][
                "notice_list"
            ],
            [],
        )
        async with self.sessions() as database:
            stored = await database.get_one(main.DestinyMail, notice_id)
            self.assertEqual((stored.sender_deleted, stored.receiver_deleted, stored.read_at), (1, 1, NOW))

    async def test_legacy_upgrade_preserves_mail_history_identity_and_cleanup(self):
        message = "历史邮件"
        payload = {"to_user_id": self.bob, "message": message, "replied_notice_id": 0}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        with contextlib.closing(sqlite3.connect(self.path)) as database, database:
            database.executescript(LEGACY_SCHEMA)
            database.execute(
                "INSERT INTO destiny_friend_pair VALUES (?, ?, ?, 1, ?, ?, 1, 0)",
                (self.alice, self.bob, self.bob, NOW, NOW),
            )
            database.execute(
                "INSERT INTO destiny_friend_pair VALUES (?, ?, ?, 0, ?, ?, 0, 0)",
                (self.bob, self.carol, self.carol, NOW, NOW),
            )
            database.execute(
                """INSERT INTO destiny_mail(notice_id,sender_id,receiver_id,message,created_at,read_at,reply_flag,
                 sender_deleted,sender_profile,receiver_profile,request_key,payload_hash)
                 VALUES(62,?,?,?,?,?,1,1,'{"saved":"old"}','{}',?,?)""",
                (self.alice, self.bob, message, NOW - 20, NOW - 10, f"{NOW}:old.123.1", digest),
            )
            database.execute(
                """INSERT INTO destiny_mail(notice_id,sender_id,receiver_id,message,created_at,sender_profile,receiver_profile,request_key,payload_hash)
                 VALUES(63,?,?,?,?,'{}','{}','earlier','earlier')""",
                (self.bob, self.alice, "to Alice", NOW - 60),
            )
        await self.migrate()
        await self.migrate()
        alice_top = (await self.post("login/topInfo", self.alice))["response_data"]
        bob_top = (await self.post("login/topInfo", self.bob))["response_data"]
        self.assertEqual((alice_top["friend_new_cnt"], bob_top["friends_approval_wait_cnt"]), (1, 1))
        inbox = (await self.post("notice/noticeFriendGreeting", self.bob, next_id=0))["response_data"]["notice_list"]
        self.assertEqual(
            (inbox[0]["notice_id"], inbox[0]["affector"], inbox[0]["readed"], inbox[0]["reply_flag"]),
            (62, {"saved": "old"}, True, True),
        )
        retry = await self.post("greet/user", self.alice, **payload, timeStamp=NOW, commandNum="old.123.1")
        self.assertEqual(retry["status_code"], 200)
        async with self.sessions() as database:
            self.assertEqual(
                await database.scalar(sqlalchemy.select(sqlalchemy.func.count()).select_from(main.DestinyMail)), 2
            )
            self.assertEqual((await database.get_one(main.DestinyFriendMutex, 1)).revision, 7)
        async with session.BasicSchoolIdolContext() as context:
            await user.delete_user(context, self.bob)
        async with self.sessions() as database:
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.DestinyMail)))
            self.assertIsNone(await database.scalar(sqlalchemy.select(main.DestinyFriendPair)))
            self.assertIsNotNone(await database.get(main.User, self.alice))

    async def test_mail_opt_in_and_native_validation(self):
        await self.migrate()
        bad = await self.post("greet/user", self.alice, to_user_id="2", message="Invalid", timeStamp=NOW, commandNum=1)
        self.assertEqual(bad["response_data"]["error_code"], error.ERROR_CODE_UNAVAILABLE_WORDS)
        await self.post("friend/request", self.alice, user_id=self.bob)
        await self.post("friend/response", self.bob, user_id=self.alice, status=2)
        bad_unicode = await self.post(
            "greet/user", self.alice, to_user_id=self.bob, message="Hi\0", timeStamp=NOW, commandNum=2
        )
        self.assertEqual(bad_unicode["response_data"]["error_code"], error.ERROR_CODE_UNAVAILABLE_WORDS)
        with unittest.mock.patch.object(config.CONFIG_DATA.gameplay, "friend_mail", False):
            disabled = await self.post("notice/noticeFriendGreeting", self.alice, next_id=0)
        self.assertEqual(disabled["response_data"]["detail"], "mail:disabled")

    async def test_mail_pagination_and_hourly_limit(self):
        await self.migrate()
        await self.post("friend/request", self.alice, user_id=self.bob)
        await self.post("friend/response", self.bob, user_id=self.alice, status=2)
        async with self.sessions.begin() as database:
            database.add_all(
                main.DestinyMail(
                    sender_id=self.alice,
                    receiver_id=self.bob,
                    message=f"Mail {index}",
                    created_at=NOW - 900 + 20 * index,
                    sender_profile="{}",
                    receiver_profile="{}",
                    request_key=f"fixture:{index}",
                    payload_hash="fixture",
                )
                for index in range(42)
            )
        rate_limited = await self.post(
            "greet/user", self.alice, to_user_id=self.bob, message="Hi", timeStamp=NOW, commandNum=1
        )
        self.assertEqual(rate_limited["response_data"]["error_code"], error.ERROR_CODE_FRIEND_OVER_REQUEST_COUNT)

        first = (await self.post("notice/noticeFriendGreeting", self.bob, next_id=0))["response_data"]
        self.assertTrue(first["has_next"])
        self.assertEqual([row["notice_id"] for row in first["notice_list"]], list(range(42, 2, -1)))
        self.assertEqual((await self.post("login/topInfo", self.bob))["response_data"]["friend_greet_cnt"], 2)
        last = (await self.post("notice/noticeFriendGreeting", self.bob, next_id=first["next_id"]))["response_data"]
        self.assertEqual([row["notice_id"] for row in last["notice_list"]], [2, 1])
        self.assertEqual((last["has_next"], last["next_id"]), (False, 0))
        self.assertEqual((await self.post("login/topInfo", self.bob))["response_data"]["friend_greet_cnt"], 0)

        newer = (await self.post("notice/noticeUserGreetingHistory", self.alice, notice_id=1, is_previous=False))[
            "response_data"
        ]
        self.assertTrue(newer["has_next"])
        self.assertEqual([row["notice_id"] for row in newer["notice_list"]], list(range(41, 1, -1)))
