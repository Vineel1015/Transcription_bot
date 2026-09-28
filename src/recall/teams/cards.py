"""Adaptive Cards (schema 1.5). Citations are Action.Submit buttons: clicking one asks the bot for that
transcript excerpt, so users verify answers without leaving Teams."""

from __future__ import annotations

from typing import Any

from recall.domain import Answer, Citation

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
MAX_CITATIONS = 6


def _card(body: list[dict[str, Any]], actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {"type": "AdaptiveCard", "$schema": SCHEMA, "version": "1.5", "body": body}
    if actions:
        card["actions"] = actions
    return card


def _text(text: str, **kw: Any) -> dict[str, Any]:
    return {"type": "TextBlock", "text": text, "wrap": True, **kw}


def _heading(text: str) -> dict[str, Any]:
    return _text(text, weight="Bolder", size="Medium")


def _cite_action(c: dict[str, Any] | Citation) -> dict[str, Any]:
    data = c.model_dump(mode="json") if isinstance(c, Citation) else c
    return {
        "type": "Action.Submit",
        "title": data["label"],
        "data": {"action": "excerpt", "meeting_id": data["meeting_id"], "start_ms": data["start_ms"]},
    }


def _dedupe(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for c in citations:
        key = (c["meeting_id"], c["start_ms"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out[:MAX_CITATIONS]


def thinking_card() -> dict[str, Any]:
    return _card([_text("Thinking…", isSubtle=True)])


def answer_card(answer: Answer) -> dict[str, Any]:
    cites = _dedupe([c.model_dump(mode="json") for c in answer.citations])
    return _card([_text(answer.answer)], [_cite_action(c) for c in cites])


def brief_card(content: dict[str, Any]) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        _heading(f"Standup brief — {content['team']}"),
        _text(f"Covering {content['covers_label']}", isSubtle=True, spacing="None"),
    ]
    cites: list[dict[str, Any]] = []
    if not content["people"] and not content["due_items"]:
        body.append(_text("Nothing was recorded for that day."))
    for person in content["people"]:
        lines = []
        if person["done"]:
            lines.append("**Done:** " + "; ".join(person["done"]))
        if person["next"]:
            lines.append("**Next:** " + "; ".join(person["next"]))
        if person["blockers"]:
            lines.append("**Blocked:** " + "; ".join(person["blockers"]))
        body.append(_text(f"**{person['name']}**", spacing="Medium"))
        body.append(_text("\n\n".join(lines) or "_No update recorded._", spacing="None"))
        cites.append(person["citation"])
    if content["due_items"]:
        body.append(_heading("Due today or overdue"))
        for item in content["due_items"]:
            flag = "⚠️ overdue" if item["overdue"] else "due today"
            body.append(
                _text(f"- {item['description']} — {item['owner'] or 'unassigned'} ({flag}, {item['due']})")
            )
            cites.append(item["citation"])
    if content["blockers"]:
        body.append(_heading("Unresolved blockers"))
        body.extend(_text(f"- {b['name']}: {b['blocker']}") for b in content["blockers"])
    return _card(body, [_cite_action(c) for c in _dedupe(cites)])


def recap_card(recap: dict[str, Any], citation: Citation) -> dict[str, Any]:
    meeting = recap["meeting"]
    body = [
        _heading(meeting.title),
        _text(citation.label.rsplit(" · ", 1)[0], isSubtle=True, spacing="None"),
        _text(recap["overview"]),
    ]
    cites = [citation.model_dump(mode="json")]
    if recap["decisions"]:
        body.append(_heading("Decisions"))
        body.extend(_text(f"- {d['decision']}") for d in recap["decisions"])
        cites.extend(meeting.cite(d.get("start_ms", 0)).model_dump(mode="json") for d in recap["decisions"])
    if recap["action_items"]:
        body.append(_heading("Action items"))
        for item in recap["action_items"]:
            due = f", due {item['due']}" if item["due"] else ""
            body.append(_text(f"- {item['description']} — {item['owner'] or 'unassigned'}{due}"))
    if recap["open_questions"]:
        body.append(_heading("Open questions"))
        body.extend(_text(f"- {q}") for q in recap["open_questions"])
    return _card(body, [_cite_action(c) for c in _dedupe(cites)])


def list_card(title: str, lines: list[str], citations: list[Citation]) -> dict[str, Any]:
    body = [_heading(title)] + [_text(line) for line in lines]
    return _card(body, [_cite_action(c) for c in _dedupe([c.model_dump(mode="json") for c in citations])])


def excerpt_card(title: str, label: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    from recall.query.format import ms_label

    body = [_heading(title), _text(label, isSubtle=True, spacing="None")]
    body.extend(_text(f"`{ms_label(s['start_ms'])}` **{s['speaker']}:** {s['text']}") for s in segments)
    return _card(body)


def message_card(text: str) -> dict[str, Any]:
    return _card([_text(text)])
