import re

from .. import data
from .. import idol

_WHITESPACE = re.compile(r"\s+")


async def has_badwords(text: str, context: idol.BasicSchoolIdolContext) -> bool:
    normalized_text = _WHITESPACE.sub("", text.lower())
    return any(word in normalized_text for word in data.get().badwords)
