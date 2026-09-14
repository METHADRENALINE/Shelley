from datetime import datetime

from ..db import Database


class BridgeStore:
    def __init__(self, db: Database, max_queue: int) -> None:
        self.db = db
        self.max_queue = max_queue

    def enqueue(self, route: str, node: str, direction: str, event_id: str, username: str, content: str, expires: datetime) -> bool:
        with self.db.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("chat_bridge:" + route,))
            existing = conn.execute(
                "SELECT 1 FROM shelley_chat_bridge WHERE route=%s AND node=%s AND direction=%s AND event_id=%s",
                (route, node, direction, event_id),
            ).fetchone()
            if existing:
                return True
            count = conn.execute(
                "SELECT count(*) AS n FROM shelley_chat_bridge WHERE route=%s AND delivered_at IS NULL AND expires_at>now()", (route,)
            ).fetchone()
            if count["n"] >= self.max_queue:
                return False
            conn.execute(
                """INSERT INTO shelley_chat_bridge (route,node,direction,event_id,username,content,expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (route, node, direction, event_id, username, content, expires),
            )
        return True

    def pending(self, route: str, direction: str, node: str | None = None) -> list[dict]:
        return self.db.fetchall(
            """SELECT sequence,event_id,username,content,node FROM shelley_chat_bridge
               WHERE route=%s AND direction=%s AND (%s::text IS NULL OR node=%s)
               AND delivered_at IS NULL AND expires_at>now() ORDER BY sequence LIMIT 25""",
            (route, direction, node, node),
        )

    def acknowledge(self, route: str, node: str, event_ids: list[str]) -> None:
        self.db.execute(
            """UPDATE shelley_chat_bridge SET delivered_at=now()
               WHERE route=%s AND node=%s AND direction='minecraft' AND event_id=ANY(%s) AND delivered_at IS NULL""",
            (route, node, event_ids),
        )

    def delivered(self, sequence: int, message_id: int) -> None:
        self.db.execute("UPDATE shelley_chat_bridge SET delivered_at=now(),discord_message_id=%s WHERE sequence=%s", (message_id, sequence))

    def cleanup(self, retention_hours: int) -> None:
        self.db.execute(
            "DELETE FROM shelley_chat_bridge WHERE expires_at<now() AND created_at<now()-(%s * interval '1 hour')",
            (retention_hours,),
        )
