"""初始化结构化友好的应用日志并屏蔽敏感字段。"""

import logging


SENSITIVE_KEYS = ("api_key", "authorization", "token", "secret")


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for key in SENSITIVE_KEYS:
                if key in record.msg.lower():
                    record.msg = "[日志包含敏感字段，内容已屏蔽]"
                    record.args = ()
                    break
        return True


def configure_logging(debug: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    root = logging.getLogger()
    if not any(isinstance(item, SecretRedactionFilter) for item in root.filters):
        root.addFilter(SecretRedactionFilter())
