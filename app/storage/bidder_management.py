"""资料库管理状态独立于材料内容；人工来源只保存在材料 JSON 中。"""

import json
import logging

from app.storage.database import open_db
from app.storage.repositories import BidderRepository, utc_now

logger = logging.getLogger(__name__)


def management(bidder_id):
    with open_db() as db:
        row = db.execute(
            "SELECT payload FROM bidder_management WHERE bidder_id=?", (bidder_id,)
        ).fetchone()
    return json.loads(row["payload"]) if row else {}


def update_management(bidder_id, **changes):
    with open_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT payload FROM bidder_management WHERE bidder_id=?", (bidder_id,)
        ).fetchone()
        payload = json.loads(row["payload"]) if row else {}
        payload.update(changes, updated_at=utc_now())
        db.execute(
            "INSERT INTO bidder_management VALUES (?,?) ON CONFLICT(bidder_id) DO UPDATE SET payload=excluded.payload",
            (bidder_id, json.dumps(payload, ensure_ascii=False)),
        )
    return payload


def note_saved(bidder_id, file_id):
    """旁路记录真实保存顺序；失败不能把已落盘的摘要向模型谎报为未保存。"""
    try:
        previous = management(bidder_id)
        order = [fid for fid in previous.get("saved_order", []) if fid != file_id]
        update_management(bidder_id, saved_order=[*order, file_id])
    except Exception:
        logger.warning("资料保存顺序未记录 bidder=%s", bidder_id, exc_info=True)


def mark_interrupted_bidders():
    with open_db() as db:
        ids = [
            row[0]
            for row in db.execute(
                "SELECT bidder_id FROM bidders WHERE status IN ('uploading_evidence','processing_evidence')"
            )
        ]
        db.execute("UPDATE bidder_uploads SET state='failed' WHERE state='receiving'")
    BidderRepository().mark_interrupted_ingestions_failed()
    from app.evidence.local_trace import interrupt_trace

    for bidder_id in ids:
        previous = management(bidder_id)
        update_management(
            bidder_id,
            phase="failed",
            failed_phase=previous.get("phase", "received"),
            trace_status="interrupted",
        )
        interrupt_trace(bidder_id)
