"""审核图只携带会话消息与轮次；文件级成果在独立 Session/报告中维护。"""

from app.audit.session import SessionState

AuditState = SessionState
