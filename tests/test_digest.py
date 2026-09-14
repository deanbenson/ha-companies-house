"""Tests for the report renderer and its helpers, on hand-built data."""

from __future__ import annotations

from custom_components.companies_house.digest import (
    _control_words,
    _due,
    _normalise_person,
    _pretty_date,
    logo_for,
    render_html,
    render_text,
    score_change,
)

CHANGE = {
    "at": "2026-09-12T09:00:00+00:00",
    "kind": "charge",
    "event_type": "created",
    "title": "ACME LTD: new charge registered",
    "message": "A charge in favour of Big Bank plc was registered on 2026-09-11.",
    "link": "https://example.invalid/company/12345678/charges",
    "score": 24,
    "document_id": None,
    "transaction_id": None,
    "links": [
        {"text": "Charges", "href": "https://example.invalid/company/12345678/charges"}
    ],
}

DIGEST = {
    "generated_at": "2026-09-14T07:30:00+00:00",
    "since": "2026-09-07T07:30:00+00:00",
    "days": 7,
    "summary": {
        "companies": 3,
        "people": 2,
        "changes": 3,
        "by_kind": {"charge": 1, "psc": 1, "appointment": 1},
        "companies_with_changes": 1,
        "people_with_changes": 1,
        "new_issues": 1,
        "still_open": 1,
        "deadlines_soon": 2,
        "new_companies": 1,
    },
    "top": [
        {
            **CHANGE,
            "subject": "ACME LTD",
            "number": "12345678",
            "logo": "https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https://acme.co.uk&size=128",
            "initials": "AL",
            "subject_link": "https://example.invalid/company/12345678",
        },
        {
            **CHANGE,
            "kind": "appointment",
            "event_type": "appointed",
            "title": "Jane Smith: new role",
            "message": "Appointed director at NEW CO LTD on 2026-09-10.",
            "link": "",
            "subject": "Jane Smith",
            "number": "",
            "logo": "",
            "initials": "JS",
            "subject_link": "https://example.invalid/officers/x/appointments",
        },
    ],
    "needs_attention": [
        {
            "company": "ACME LTD",
            "number": "12345678",
            "link": "https://example.invalid/company/12345678",
            "issues": ["Strike-off proposed (compulsory) — 41 days to object"],
            "links": [
                {
                    "text": "Gazette notice",
                    "href": "https://example.invalid/company/12345678/filing-history/gaz1/document?format=pdf&download=0",
                },
                # Already one of the standard pages: shown once, not twice.
                {
                    "text": "Gazette notices",
                    "href": "https://example.invalid/company/12345678/filing-history?category=gazette",
                },
            ],
        }
    ],
    "still_open": [
        {
            "company": "OLD PIG LTD",
            "number": "22222222",
            "link": "https://example.invalid/company/22222222",
            "issues": ["In liquidation", "Accounts overdue"],
        }
    ],
    "deadlines": [
        {
            "what": "accounts",
            "date": "2026-09-30",
            "days": 16,
            "company": "ACME LTD",
            "number": "12345678",
            "link": "https://example.invalid/company/12345678",
        },
        {
            "what": "confirmation_statement",
            "date": "2026-09-14",
            "days": 0,
            "company": "NEW CO LTD",
            "number": "17000001",
            "link": "https://example.invalid/company/17000001",
        },
        {
            "what": "object to strike-off",
            "date": "2026-10-25",
            "days": 41,
            "company": "ACME LTD",
            "number": "12345678",
            "link": "https://example.invalid/company/12345678",
            "document": "https://example.invalid/company/12345678/filing-history/gaz1/document?format=pdf&download=0",
        },
    ],
    "new_companies": [
        {
            "company": "NEW CO LTD",
            "number": "17000001",
            "link": "https://example.invalid/company/17000001",
            "incorporated": "2026-09-01",
            "new_this_period": True,
            "people": [{"name": "Jane Smith", "role": "director"}],
        }
    ],
    "companies": [
        {
            "name": "ACME LTD",
            "number": "12345678",
            "status": "active",
            "label": "Ours",
            "website": "acme.co.uk",
            "logo": "https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https://acme.co.uk&size=128",
            "initials": "AL",
            "link": "https://example.invalid/company/12345678",
            "close_watch": True,
            "pages": [
                {
                    "text": "Filing history",
                    "href": "https://example.invalid/company/12345678/filing-history",
                },
                {
                    "text": "Insolvency",
                    "href": "https://example.invalid/company/12345678/insolvency",
                },
            ],
            "weight": 3,
            "incorporated": "2015-01-01",
            "next_deadline": {"what": "accounts", "date": "2026-09-30", "days": 16},
            "strike_off": {
                "kind": "compulsory",
                "notice_on": "2026-09-08",
                "earliest_on": "2026-11-08",
                "objection_deadline": "2026-10-25",
                "days_to_object": 41,
                "days_to_strike_off": 55,
                "suspended_on": None,
                "transaction_id": "gaz1",
                "caveat": "Assumes two months.",
                "summary": "Strike-off proposed (compulsory) — 41 days to object",
                "link": "https://example.invalid/company/12345678/filing-history/gaz1/document?format=pdf&download=0",
            },
            "attention": [
                {
                    "issue": "Strike-off proposed (compulsory) — 41 days to object",
                    "new": True,
                    "since": "2026-09-08",
                    "short": "Strike-off proposed",
                    "links": [],
                },
                {"issue": "Accounts overdue", "new": False, "since": "2026-01-01"},
            ],
            "changes": [
                CHANGE,
                {
                    **CHANGE,
                    "kind": "psc",
                    "event_type": "notified",
                    "title": "ACME LTD: new controlling person",
                    "message": "Mark Taylor now controls 75 to 100% of the shares.",
                },
            ],
            "score": 24,
        }
    ],
    "people": [
        {
            "name": "Jane Smith",
            "officer_id": "x",
            "records": 2,
            "initials": "JS",
            "link": "https://example.invalid/officers/x/appointments",
            "companies": [
                {
                    "name": f"COMPANY {i} LTD",
                    "number": str(i),
                    "link": f"https://example.invalid/company/{i}",
                    "role": "director",
                    "appointed_on": None,
                }
                for i in range(8)
            ],
            "changes": [
                {
                    **CHANGE,
                    "kind": "appointment",
                    "event_type": "appointed",
                    "title": "Jane Smith: new role",
                    "message": "Appointed director at NEW CO LTD on 2026-09-10.",
                    "link": "",
                    "score": 6,
                }
            ],
            "score": 6,
        }
    ],
    "ownership": [
        {
            "name": "Mark Taylor",
            "followed": True,
            "kind": "individual",
            "holdings": [
                {
                    "company": "ACME LTD",
                    "number": "12345678",
                    "link": "https://example.invalid/company/12345678",
                    "control": "75 to 100% of the shares",
                    "watched": True,
                }
            ],
        }
    ]
    + [
        {
            "name": f"Holder {i}",
            "followed": False,
            "kind": "corporate-entity",
            "holdings": [
                {
                    "company": "X",
                    "number": "1",
                    "link": "",
                    "control": "25 to 50% of the shares",
                    "watched": False,
                }
            ],
        }
        for i in range(14)
    ],
    "quiet_companies": ["QUIET LTD"],
}


def test_render_html_covers_every_section() -> None:
    """Every section renders, with the new problem in red and the old one muted."""
    html = render_html(
        DIGEST, title="Your Companies House week", summary="Line one.\n\nLine two."
    )
    for expected in (
        "Line one.",
        "Line two.",
        "New this week: needs attention",
        "Worth a look",
        "New companies",
        "set up 1 Sep 2026",
        "Jane Smith (director)",
        "Due in the next 30 days",
        "due today",
        "in 16 days",
        "Who controls what",
        "Mark Taylor</strong> ★",
        "and 3 more",
        "Still open",
        "OLD PIG LTD",
        "url=https://acme.co.uk&amp;size=128",
        "and 2 more",  # Jane's companies beyond six
        "3 changes (1 charge, 1 ownership change, 1 role change)",
        "background:#fee2e2",  # new badge
        "background:#f3f4f6;color:#6b7280",  # ongoing badge
        # Links to dig deeper: pages on every company, more on every change.
        'href="https://example.invalid/company/12345678/filing-history" style="color:#1d4ed8;text-decoration:none">Filing history',
        'href="https://example.invalid/company/12345678/insolvency" style="color:#1d4ed8;text-decoration:none">Insolvency',
        'href="https://example.invalid/company/12345678/charges" style="color:#1d4ed8;text-decoration:none">Charges',
        'href="https://example.invalid/company/3" style="color:#6b7280;text-decoration:none">COMPANY 3 LTD',
        "All their appointments",
        "Gazette notices",
        'href="https://example.invalid/company/17000001/officers" style="color:#1d4ed8;text-decoration:none">Officers',
        "persons-with-significant-control",
        # The strike-off countdown: the full line in the list, the short badge
        # on the card, the objection row in the deadlines with the notice PDF.
        "Strike-off proposed (compulsory) — 41 days to object",
        "Strike-off proposed</span>",
        "object to strike-off",
        'gaz1/document?format=pdf&amp;download=0" style="color:#1d4ed8;text-decoration:none">Gazette notice</a>',
    ):
        assert expected in html, expected
    assert "A quiet week" not in html
    assert html.count("filing-history?category=gazette") == 2  # once per company
    assert "41 days to object</span>" not in html  # the badge is the short form

    text = render_text(DIGEST, title="Your Companies House week", summary="Summary.")
    assert text.startswith("Your Companies House week\n\nSummary.\n\n")
    assert "New this week, needs attention:" in text
    assert "Worth a look:" in text
    assert "New companies:" in text
    assert "NEW CO LTD (set up 1 Sep 2026): Jane Smith" in text
    assert "Due in the next 30 days:" in text
    assert "  - 25 Oct 2026 ACME LTD: object to strike-off (in 41 days)" in text
    assert "  - ACME LTD: Strike-off proposed (compulsory) — 41 days to object" in text
    assert "Still open (known before this week):" in text


def test_render_quiet_week() -> None:
    """With nothing changed the report says so, once."""
    quiet = {
        **DIGEST,
        "top": [],
        "needs_attention": [],
        "still_open": [],
        "deadlines": [],
        "new_companies": [],
        "companies": [],
        "people": [],
        "summary": {**DIGEST["summary"], "changes": 0, "by_kind": {}},
    }
    html = render_html(quiet, title="T")
    assert "A quiet week" in html
    assert "Who controls what" not in html
    assert "0 changes ·" in html
    assert "Nothing changed at any watched company or person." in render_text(
        quiet, title="T"
    )


def test_helpers() -> None:
    """Small formatting helpers behave."""
    assert _due(None) == ("", "color:#6b7280;")
    assert _due(-3)[0] == "3 days overdue"
    assert _due(0)[0] == "due today"
    assert _due(5)[0] == "in 5 days"
    assert _due(20)[0] == "in 20 days"
    assert _pretty_date("2026-09-30") == "30 Sep 2026"
    assert _pretty_date("nonsense") == "nonsense"
    assert logo_for(" https://www.acme.co.uk/about ") == (
        "https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON"
        "&fallback_opts=TYPE,SIZE,URL&url=https://acme.co.uk&size=128"
    )
    assert logo_for("") == ""
    assert _normalise_person("Mr Mark John TAYLOR") == _normalise_person(
        "TAYLOR, Mark John"
    )
    assert _control_words(["significant-influence-or-control", "something-else"]) == (
        "significant influence or control, something else"
    )
    assert score_change("status", "strike-off-proposed", weight=3) == 30
    assert score_change("filing", "confirmation-statement", weight=1) == 1
    assert score_change("made", "up", weight=2) == 4
