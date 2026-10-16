"""Keep bearer tokens out of access logs."""
import logging
import re

_TOKEN_PATH = re.compile(r'(/(?:activity/api/(?:config|ingest|location|client)|finance/api/webhook|jobs/shared)/)[^/?\s]+')


def redact_tokens(value: str) -> str:
    value = _TOKEN_PATH.sub(r'\1[redacted]', value)
    return re.sub(r'(app_id=)[^&\s]+', r'\1[redacted]', value)


class RedactAccessTokens(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(redact_tokens(arg) if isinstance(arg, str) else arg for arg in record.args)
        if isinstance(record.msg, str):
            record.msg = redact_tokens(record.msg)
        return True
