#!/usr/bin/env python3
"""Sample chat DB rows and report role mapping (participant_type → nature → details)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
from psycopg2.extras import RealDictCursor

from app.agents.responder_eval.chat_source import resolve_message_role
from app.agents.responder_eval.constants import CHAT_ROLE_AGENT, CHAT_ROLE_BOT, CHAT_ROLE_USER


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate responder eval chat role mapping")
    parser.add_argument("--db-url", required=True, help="Sync Postgres URL for chat DB")
    parser.add_argument("--limit", type=int, default=100, help="Rows to sample")
    parser.add_argument("--conversation-id", type=int, help="Dump one conversation with roles")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db_url, connect_timeout=15)
    try:
        if args.conversation_id is not None:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        m.participant_id,
                        m.nature,
                        m.details,
                        m.text,
                        m.created_at,
                        p.participant_type,
                        p.name AS participant_name
                    FROM messages m
                    LEFT JOIN conversation_participants cp ON cp.id = m.participant_id
                    LEFT JOIN participants p ON p.id = cp.participant_id
                    WHERE m.conversation_id = %s
                    ORDER BY m.created_at ASC
                    """,
                    (args.conversation_id,),
                )
                rows = [dict(r) for r in cur.fetchall()]
            for i, row in enumerate(rows):
                role = resolve_message_role(row)
                print(
                    json.dumps(
                        {
                            "turn": i,
                            "role": role,
                            "participant_id": row.get("participant_id"),
                            "participant_type": row.get("participant_type"),
                            "participant_name": row.get("participant_name"),
                            "nature": row.get("nature"),
                            "details": row.get("details"),
                            "text": (row.get("text") or "")[:120],
                        },
                        default=str,
                    )
                )
            return 0

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    m.conversation_id,
                    m.participant_id,
                    m.nature,
                    m.details,
                    LEFT(m.text, 80) AS text_preview,
                    p.participant_type
                FROM messages m
                LEFT JOIN conversation_participants cp ON cp.id = m.participant_id
                LEFT JOIN participants p ON p.id = cp.participant_id
                ORDER BY m.id DESC
                LIMIT %s
                """,
                (args.limit,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    role_counts = {CHAT_ROLE_USER: 0, CHAT_ROLE_BOT: 0, CHAT_ROLE_AGENT: 0, "other": 0}
    samples: list[dict[str, str]] = []
    for row in rows:
        role = resolve_message_role(dict(row))
        key = role if role in role_counts else "other"
        role_counts[key] += 1
        if len(samples) < 10:
            samples.append(
                {
                    "role": role,
                    "participant_type": str(row.get("participant_type") or ""),
                    "details": json.dumps(row["details"], default=str)[:120],
                    "text": (row.get("text_preview") or "")[:80],
                }
            )

    print(
        json.dumps(
            {
                "sampled": len(rows),
                "user": role_counts[CHAT_ROLE_USER],
                "bot": role_counts[CHAT_ROLE_BOT],
                "agent": role_counts[CHAT_ROLE_AGENT],
                "other": role_counts["other"],
                "samples": samples,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
