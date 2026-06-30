"""Google Docs API wrapper.

Archy writes a daily brief — a friendly summary of today's schedule,
mood, and recommendations — as a Google Doc.
"""
from __future__ import annotations

from typing import Any

from loguru import logger


class DocsClient:
    """Wraps Google Docs API for Archy's daily brief."""

    def __init__(self, auth):
        self._auth = auth
        self._service: Any = None

    def _get_service(self):
        if self._service is None:
            from googleapiclient.discovery import build
            creds = self._auth.get_credentials()
            self._service = build("docs", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def create_brief(self, title: str, sections: list[dict]) -> str:
        """Create a new Google Doc with the given title and sections.

        Args:
            title: Document title
            sections: list of {heading: str, body: str} dicts

        Returns: the doc ID (also the URL fragment for opening).
        """
        service = self._get_service()
        body = {"title": title}
        doc = service.documents().create(body=body).execute()
        doc_id = doc["documentId"]
        logger.info(f"Created Google Doc '{title}' (id={doc_id})")

        # Build batch update requests for all sections at once
        requests: list[dict] = []
        for section in sections:
            requests.append({
                "insertText": {
                    "location": {"index": 1},
                    "text": f"{section['heading']}\n",
                }
            })
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": 1, "endIndex": len(section['heading']) + 1},
                    "textStyle": {"bold": True, "fontSize": {"magnitude": 14, "unit": "PT"}},
                    "fields": "bold,fontSize",
                }
            })
            requests.append({
                "insertText": {
                    "location": {"index": 1},
                    "text": f"\n{section['body']}\n\n",
                }
            })

        if requests:
            service.documents().batchUpdate(
                documentId=doc_id, body={"requests": list(reversed(requests))}
            ).execute()

        return doc_id

    @staticmethod
    def doc_url(doc_id: str) -> str:
        return f"https://docs.google.com/document/d/{doc_id}/edit"
