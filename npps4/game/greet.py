import hashlib
import json
import unicodedata

import pydantic

from .. import idol
from ..config import config
from ..system import mail

from typing import Any, Self, override

MAX_ID = 2**63 - 1


class MailRequest(pydantic.BaseModel):
    @classmethod
    @override
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        # Form validation precedes the router's conversion to a client business error
        try:
            return super().model_validate(obj, **kwargs)
        except pydantic.ValidationError:
            invalid = cls.model_construct()
            object.__setattr__(invalid, "_mail_invalid", True)
            return invalid

    def check(self) -> None:
        if getattr(self, "_mail_invalid", False):
            mail.reject("mail:invalid_request", idol.error.ERROR_CODE_UNAVAILABLE_WORDS)
        if not config.CONFIG_DATA.gameplay.friend_mail:
            mail.reject("mail:disabled")


class SendRequest(MailRequest):
    to_user_id: pydantic.StrictInt = pydantic.Field(gt=0, le=MAX_ID)
    message: pydantic.StrictStr = pydantic.Field(max_length=1024)
    replied_notice_id: pydantic.StrictInt | None = pydantic.Field(default=0, ge=0, le=MAX_ID)


class DeleteRequest(MailRequest):
    mail_notice_id: pydantic.StrictInt = pydantic.Field(gt=0, le=MAX_ID)
    is_send_mail: bool


def request_identity(context: idol.SchoolIdolUserParams, request: SendRequest) -> tuple[str, str]:
    invalid = idol.error.ERROR_CODE_UNAVAILABLE_WORDS
    try:
        body = json.loads(context.raw_request_data)
    except (ValueError, UnicodeError):
        mail.reject("mail:invalid_request_identity", invalid)
    if not isinstance(body, dict):
        mail.reject("mail:invalid_request_identity", invalid)
    stamp = body.get("timeStamp")
    command = body.get("commandNum")
    if type(stamp) is not int or not 0 <= stamp <= MAX_ID:
        mail.reject("mail:invalid_request_identity", invalid)
    if type(command) is str:
        if not 1 <= len(command) <= 512 or any(
            character.isspace() or unicodedata.category(character).startswith("C") for character in command
        ):
            mail.reject("mail:invalid_request_identity", invalid)
    elif type(command) is not int or not 0 <= command <= MAX_ID:
        mail.reject("mail:invalid_request_identity", invalid)
    return f"{stamp}:{command}", hashlib.sha256(request.model_dump_json().encode()).hexdigest()


@idol.register("greet", "user", batchable=False)
async def greet_user(context: idol.SchoolIdolUserParams, request: SendRequest) -> None:
    request.check()
    key, digest = request_identity(context, request)
    await mail.send(context, request.to_user_id, request.message, request.replied_notice_id or 0, key, digest)


@idol.register("greet", "delete", batchable=False)
async def greet_delete(context: idol.SchoolIdolUserParams, request: DeleteRequest) -> None:
    request.check()
    await mail.delete(context, request.mail_notice_id, request.is_send_mail)
