"""Tests for the report renderer and its helpers, on hand-built data."""

from __future__ import annotations

from custom_components.companies_house.digest import (
    _control_words,
    _due,
    _normalise_person,
    _pill,
    _pretty_date,
    _risk_counts,
    _risk_list,
    _risk_worsened,
    describe_change,
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
        "risk": {"red": 1, "amber": 1, "green": 1, "unknown": 0},
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
            "issues": ["Strike-off proposed"],
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
            "attention": [
                {"issue": "Strike-off proposed", "new": True, "since": None},
                {"issue": "Accounts overdue", "new": False, "since": "2026-01-01"},
                {"issue": "Risk rating red", "new": True, "kind": "risk"},
            ],
            "risk": {
                "band": "red",
                "score": 100,
                "reason": "Red: strike-off proposed (compulsory) on 3 Jul 2026; accounts 5 months overdue",
                "reasons": [
                    "strike-off proposed (compulsory) on 3 Jul 2026",
                    "accounts 5 months overdue",
                ],
            },
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
    "risk": [
        {
            "company": "ACME LTD",
            "number": "12345678",
            "link": "https://example.invalid/company/12345678",
            "band": "red",
            "score": 100,
            "reason": "Red: strike-off proposed (compulsory) on 3 Jul 2026; accounts 5 months overdue",
            "reasons": [
                "strike-off proposed (compulsory) on 3 Jul 2026",
                "accounts 5 months overdue",
            ],
            "pages": [
                {
                    "text": "Filing history",
                    "href": "https://example.invalid/company/12345678/filing-history",
                }
            ],
        },
        {
            "company": "SHAKY LTD",
            "number": "33333333",
            "link": "https://example.invalid/company/33333333",
            "band": "amber",
            "score": 12,
            "reason": "Amber: sole director; only 2 years old",
            "reasons": ["sole director", "only 2 years old"],
            "pages": [],
        },
    ],
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
        # The risk section, worst first, and the pill in the card heading.
        "🚦&nbsp; Risk",
        "Not a credit check.",
        ">red</span>",
        ">amber</span>",
        "strike-off proposed (compulsory) on 3 Jul 2026; accounts 5 months overdue",
        'href="https://example.invalid/company/33333333" style="color:#111827;text-decoration:none">SHAKY LTD',
        "sole director; only 2 years old",
        "risk: 1 red, 1 amber, 1 green",
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
    ):
        assert expected in html, expected
    assert "A quiet week" not in html
    # The risk badge is not doubled: the pill says it, so no grey badge for it.
    assert "Risk rating red" not in html

    text = render_text(DIGEST, title="Your Companies House week", summary="Summary.")
    assert text.startswith("Your Companies House week\n\nSummary.\n\n")
    assert "New this week, needs attention:" in text
    assert "Worth a look:" in text
    assert "New companies:" in text
    assert "NEW CO LTD (set up 1 Sep 2026): Jane Smith" in text
    assert "Due in the next 30 days:" in text
    assert "Risk (from the register alone, not a credit check):" in text
    assert (
        "  - RED ACME LTD — strike-off proposed (compulsory) on 3 Jul 2026; "
        "accounts 5 months overdue"
    ) in text
    assert "  - AMBER SHAKY LTD — sole director; only 2 years old" in text
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
        "risk": [],
        "summary": {**DIGEST["summary"], "changes": 0, "by_kind": {}, "risk": {}},
    }
    html = render_html(quiet, title="T")
    assert "A quiet week" in html
    assert "Who controls what" not in html
    assert "Risk" not in html
    assert "0 changes ·" in html
    assert "risk:" not in html
    assert "Risk (from" not in render_text(quiet, title="T")
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


def test_risk_helpers() -> None:
    """The pill, the band counts, the worst-first list and the worsened test."""
    assert _pill("green") == ""
    assert _pill(None) == ""
    assert "margin-left:6px" in _pill("amber")
    assert "margin-left:6px" not in _pill("red", margin=False)
    cards = [
        {
            "name": "A",
            "number": "1",
            "link": "l1",
            "risk": {
                "band": "amber",
                "score": 12,
                "reason": "Amber: x",
                "reasons": ["x"],
            },
        },
        {
            "name": "B",
            "number": "2",
            "link": "l2",
            "risk": {"band": "red", "score": 60, "reason": "Red: y", "reasons": ["y"]},
        },
        {
            "name": "C",
            "number": "3",
            "link": "l3",
            "risk": {
                "band": "green",
                "score": 0,
                "reason": "Green: no concerns on the register",
                "reasons": [],
            },
        },
        {"name": "D", "number": "4", "link": "l4", "risk": None},
        {
            "name": "E",
            "number": "5",
            "link": "l5",
            "risk": {"band": None, "score": 0, "reason": "Unknown", "reasons": []},
        },
        {
            "name": "F",
            "number": "6",
            "link": "l6",
            "risk": {
                "band": "amber",
                "score": 20,
                "reason": "Amber: z",
                "reasons": ["z"],
            },
        },
        {
            "name": "G",
            "number": "7",
            "link": "l7",
            "finished": True,
            "risk": {
                "band": "red",
                "score": 100,
                "reason": "Red: dissolved on 13 Aug 2024",
                "reasons": ["dissolved on 13 Aug 2024"],
            },
        },
    ]
    # The dissolved company counts as red but is not listed: not news.
    assert _risk_counts(cards) == {"red": 2, "amber": 2, "green": 1, "unknown": 2}
    assert [(r["band"], r["company"]) for r in _risk_list(cards)] == [
        ("red", "B"),
        ("amber", "F"),
        ("amber", "A"),
    ]
    assert _risk_list(cards)[0]["pages"] == []
    logged = [
        {
            "kind": "status",
            "event_type": "risk-changed",
            "payload": {"old_band": "amber", "new_band": "green"},
        },
        {"kind": "officer", "event_type": "resigned", "payload": {}},
    ]
    assert not _risk_worsened(logged)
    assert _risk_worsened(
        [
            *logged,
            {
                "kind": "status",
                "event_type": "risk-changed",
                "payload": {"old_band": "green", "new_band": "red"},
            },
        ]
    )
    assert not _risk_worsened([{"kind": "status", "event_type": "risk-changed"}])


def test_describe_risk_change() -> None:
    """A rating change reads as a sentence, with the reasons when known."""
    title, message, link = describe_change(
        "status",
        "risk-changed",
        {"old_band": "green", "new_band": "amber", "reason": "Amber: sole director"},
        subject="ACME LTD",
        number="12345678",
    )
    assert title == "ACME LTD: risk now amber"
    assert message == "Amber: sole director"
    assert link.endswith("/company/12345678")
    title, message, _ = describe_change(
        "status", "risk-changed", {"old_band": "amber", "new_band": "red"}, subject="X"
    )
    assert (title, message) == ("X: risk now red", "Red: was amber.")
    title, message, _ = describe_change("status", "risk-changed", {}, subject="X")
    assert (title, message) == ("X: risk now unknown", "Unknown: was unknown.")
