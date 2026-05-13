import csv
import io


EXPORT_FIELDS = [
    "id",
    "title",
    "source_name",
    "source_category",
    "published_at",
    "importance_score",
    "verification_status",
    "newsroom_status",
    "assignee",
    "desk_notes",
    "url",
]


def articles_to_csv(articles: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for article in articles:
        writer.writerow(article)
    return buffer.getvalue()


def articles_to_cuesheet(articles: list[dict]) -> str:
    lines = ["# Newsroom Cue Sheet Export", ""]
    for index, article in enumerate(articles, start=1):
        lines.extend(
            [
                f"{index}. {article.get('title')}",
                f"   Source: {article.get('source_name')}",
                f"   Category: {article.get('source_category') or '-'}",
                f"   Score: {article.get('importance_score')} / Status: {article.get('newsroom_status')}",
                f"   Anchor: {article.get('ai_summary') or article.get('summary') or '요약 필요'}",
                f"   Notes: {article.get('desk_notes') or '-'}",
                f"   URL: {article.get('url')}",
                "",
            ]
        )
    return "\n".join(lines)
