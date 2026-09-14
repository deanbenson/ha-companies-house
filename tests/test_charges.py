"""Tests for the charge model and its plain-English helpers."""

from __future__ import annotations

from datetime import date
import json
from types import SimpleNamespace

from custom_components.companies_house.charges import (
    CHARGE_LIST_CAP,
    charge_filing_link,
    charge_payload,
    charge_record,
    charge_words,
    charges_overview,
    charges_page,
    lender,
    lenders,
    newest_first,
    security_words,
    shorten,
)
from custom_components.companies_house.digest import (
    _change_links,
    _charges_line,
    describe_change,
)
from custom_components.companies_house.models import (
    Charge,
    ChargeList,
    ChargeTransaction,
)

from .conftest import load_fixture

NUMBER = "12345678"
BASE = "https://find-and-update.company-information.service.gov.uk"
LIVE_ITEM = {
    # The shape the live API returns (a live company, 14 Sep 2026): objects, not
    # arrays, for classification and particulars; "fully-satisfied".
    "charge_code": "086791110001",
    "charge_number": 1,
    "classification": {
        "description": "A registered charge",
        "type": "charge-description",
    },
    "created_on": "2019-07-03",
    "delivered_on": "2019-07-05",
    "etag": "e",
    "links": {"self": "/company/08679111/charges/b9FzahOe7iQ9-bL74JCpArf8EKo"},
    "particulars": {
        "contains_fixed_charge": True,
        "contains_floating_charge": True,
        "contains_negative_pledge": True,
        "floating_charge_covers_all": True,
    },
    "persons_entitled": [{"name": "Lloyds Bank PLC"}],
    "satisfied_on": "2026-05-13",
    "status": "fully-satisfied",
    "transactions": [
        {
            "delivered_on": "2019-07-05",
            "filing_type": "create-charge-with-deed",
            "links": {
                "filing": "/company/08679111/filing-history/MzA5OTE3MTc1NmFkaXF6a2N4"
            },
        },
        {
            "delivered_on": "2026-05-14",
            "filing_type": "charge-satisfaction",
            "links": {
                "filing": "/company/08679111/filing-history/MzQ0MDAwMDAwMGFkaXF6a2N4"
            },
        },
    ],
}


def _charge(**overrides: object) -> Charge:
    return Charge(**overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------- the model


def test_charge_from_live_shape() -> None:
    """Every field the feature needs is read from the live payload shape."""
    charge = Charge.from_api(LIVE_ITEM)
    assert charge.charge_id == "b9FzahOe7iQ9-bL74JCpArf8EKo"
    assert charge.charge_code == "086791110001"
    assert charge.classification == "A registered charge"
    assert charge.classification_type == "charge-description"
    assert charge.contains_fixed_charge is True
    assert charge.contains_floating_charge is True
    assert charge.contains_negative_pledge is True
    assert charge.floating_charge_covers_all is True
    assert charge.chargor_acting_as_bare_trustee is None  # not sent
    assert charge.particulars_description is None
    assert charge.secured_description is None
    assert charge.persons_entitled == ["Lloyds Bank PLC"]
    assert charge.more_than_four_persons_entitled is False
    assert charge.status == "fully-satisfied"
    assert charge.is_satisfied
    assert not charge.is_outstanding
    assert charge.satisfied_on == date(2026, 5, 13)
    assert [t.filing_type for t in charge.transactions] == [
        "create-charge-with-deed",
        "charge-satisfaction",
    ]
    assert charge.transactions[0].delivered_on == date(2019, 7, 5)
    assert charge.transactions[0].is_creation
    assert not charge.transactions[0].is_satisfaction
    assert charge.transactions[1].is_satisfaction
    assert charge.creation_transaction_id == "MzA5OTE3MTc1NmFkaXF6a2N4"
    assert charge.satisfaction_transaction_id == "MzQ0MDAwMDAwMGFkaXF6a2N4"
    assert charge.insolvency_case_numbers == []


def test_charge_from_documented_array_shape_and_misspelt_key() -> None:
    """The specification's array shapes and its "assests" spelling are accepted."""
    charge = Charge.from_api(
        {
            "id": "abc",
            "classification": [
                {"description": "Legal mortgage", "type": "nature-of-charge"}
            ],
            "particulars": [
                {
                    "description": "  The property at 1 High Street  ",
                    "type": "charged-property-description",
                    "chargor_acting_as_bare_trustee": False,
                }
            ],
            "secured_details": [{"description": "£250,000", "type": "amount-secured"}],
            "assests_ceased_released": "part-of-the-property",
            "more_than_four_persons_entitled": True,
            "persons_entitled": [
                {"name": "A"},
                {"name": "B"},
                {"no_name": True},
                "junk",
            ],
            "resolved_on": "2020-02-02",
            "covering_instrument_date": "2019-01-01",
            "transactions": [
                {"filing_type": "create-charge-with-deed", "links": {}},
                "junk",
                {
                    "filing_type": "charge-part-satisfaction",
                    "insolvency_case_number": 3,
                    "links": {"filing": "/company/1/filing-history/TXPART"},
                },
            ],
            "insolvency_cases": [{"case_number": 3, "links": {}}, {"links": {}}, "x"],
            "status": "part-satisfied",
        }
    )
    assert charge.charge_id == "abc"
    assert charge.classification == "Legal mortgage"
    assert charge.classification_type == "nature-of-charge"
    assert charge.particulars_description == "  The property at 1 High Street  "
    assert charge.particulars_type == "charged-property-description"
    assert charge.chargor_acting_as_bare_trustee is False
    assert charge.contains_fixed_charge is None
    assert charge.secured_description == "£250,000"
    assert charge.secured_type == "amount-secured"
    assert charge.assets_ceased_released == "part-of-the-property"
    assert charge.more_than_four_persons_entitled is True
    assert charge.persons_entitled == ["A", "B"]
    assert charge.resolved_on == date(2020, 2, 2)
    assert charge.covering_instrument_date == date(2019, 1, 1)
    assert charge.is_outstanding
    assert not charge.is_satisfied
    # A creation filing without a link gives no id; the part satisfaction has one.
    assert charge.creation_transaction_id is None
    assert charge.satisfaction_transaction_id == "TXPART"
    assert charge.transactions[1].insolvency_case_number == "3"
    assert charge.insolvency_case_numbers == ["3"]
    # Empty arrays and the correctly spelt key work too.
    plain = Charge.from_api(
        {"classification": [], "particulars": [], "assets_ceased_released": "whole"}
    )
    assert plain.classification is None
    assert plain.assets_ceased_released == "whole"


def test_transaction_id_fallbacks() -> None:
    """An old charge listing one unlabelled filing treats it as the creation."""
    unlabelled = _charge(
        transactions=[ChargeTransaction(filing_type=None, transaction_id="TX1")]
    )
    assert unlabelled.creation_transaction_id == "TX1"
    assert unlabelled.satisfaction_transaction_id is None
    only_release = _charge(
        transactions=[
            ChargeTransaction(filing_type="charge-whole-release", transaction_id="TX2")
        ]
    )
    assert only_release.creation_transaction_id is None
    assert only_release.satisfaction_transaction_id == "TX2"
    assert _charge().creation_transaction_id is None
    assert _charge().satisfaction_transaction_id is None


def test_storage_round_trip_and_old_snapshots() -> None:
    """New fields survive the store and are absent, not broken, in old snapshots."""
    charges = ChargeList.from_api(load_fixture("company_active/charges"))
    stored = charges.to_storage()
    assert stored["items"][0]["transactions"][0]["transaction_id"]
    rebuilt = ChargeList.from_storage(json.loads(json.dumps(stored)))
    assert rebuilt == charges
    old = ChargeList.from_storage(
        {
            "total_count": 1,
            "items": [
                {
                    "charge_id": "chg-old",
                    "status": "outstanding",
                    "persons_entitled": ["Old Bank"],
                    "created_on": "2001-01-01",
                }
            ],
        }
    )
    assert old.items[0].transactions == []
    assert old.items[0].contains_fixed_charge is None
    assert old.items[0].more_than_four_persons_entitled is False
    assert old.items[0].creation_transaction_id is None


# ---------------------------------------------------------------- the words


def test_shorten_and_lender() -> None:
    """Long legal wording is cut at a word; lenders are named, with "and others"."""
    assert shorten(None) == ""
    assert shorten("   ") == ""
    assert shorten("  a   b  ") == "a b"
    long = " ".join(["word"] * 60)
    cut = shorten(long)
    assert cut.endswith("…")
    assert len(cut) <= 80
    assert shorten("alpha, beta, gamma", limit=13) == "alpha, beta…"
    assert shorten("alpha, beta, gamma", limit=12) == "alpha…"
    assert shorten("alpha, beta", limit=12) == "alpha, beta"
    assert lender(_charge()) == ""
    assert lender(_charge(persons_entitled=["A", "B"])) == "A, B"
    assert (
        lender(_charge(persons_entitled=["A"], more_than_four_persons_entitled=True))
        == "A and others"
    )
    assert lender(_charge(more_than_four_persons_entitled=True)) == ""


def test_charge_words_matrix() -> None:
    """The kind of charge reads naturally for every combination of flags."""
    assert charge_words(_charge()) == ""
    assert charge_words(_charge(contains_fixed_charge=True)) == "fixed charge"
    assert charge_words(_charge(contains_floating_charge=True)) == "floating charge"
    assert (
        charge_words(
            _charge(contains_floating_charge=True, floating_charge_covers_all=True)
        )
        == "floating charge over all the company's assets"
    )
    assert (
        charge_words(_charge(contains_fixed_charge=True, contains_floating_charge=True))
        == "fixed and floating charge"
    )
    assert (
        charge_words(
            _charge(
                contains_fixed_charge=True,
                contains_floating_charge=True,
                floating_charge_covers_all=True,
            )
        )
        == "fixed and floating charge over all the company's assets"
    )
    # "Covers all" without a floating charge is nonsense; it is ignored.
    assert (
        charge_words(
            _charge(contains_fixed_charge=True, floating_charge_covers_all=True)
        )
        == "fixed charge"
    )


def test_security_words() -> None:
    """Kind, what it is over and what it secures join into one readable clause."""
    assert security_words(_charge()) == ""
    assert (
        security_words(
            _charge(
                contains_fixed_charge=True,
                contains_floating_charge=True,
                floating_charge_covers_all=True,
                particulars_description="Everything, at length",
                secured_description="All monies due or to become due",
            )
        )
        == "fixed and floating charge over all the company's assets; "
        "secures all monies due or to become due"
    )
    assert (
        security_words(
            _charge(
                contains_fixed_charge=True,
                particulars_description="The property at 1 High Street",
                secured_description="£250,000",
            )
        )
        == "fixed charge over the property at 1 High Street; secures £250,000"
    )
    assert (
        security_words(_charge(particulars_description="HSBC's premises"))
        == "charge over HSBC's premises"
    )
    assert security_words(_charge(secured_description="X")) == "secures x"
    assert (
        security_words(
            _charge(contains_floating_charge=True, particulars_description=".")
        )
        == "floating charge over ."
    )


def test_links_payload_and_record() -> None:
    """Payload and attribute rows carry the words, the dates and the PDF link."""
    assert charges_page(NUMBER) == f"{BASE}/company/{NUMBER}/charges"
    assert charge_filing_link(NUMBER, None) == charges_page(NUMBER)
    assert charge_filing_link(NUMBER, "TX") == (
        f"{BASE}/company/{NUMBER}/filing-history/TX/document?format=pdf&download=0"
    )
    charge = Charge.from_api(LIVE_ITEM)
    payload = charge_payload(charge)
    assert payload == {
        "charge_code": "086791110001",
        "charge_id": "b9FzahOe7iQ9-bL74JCpArf8EKo",
        "persons_entitled": ["Lloyds Bank PLC"],
        "lender": "Lloyds Bank PLC",
        "created_on": "2019-07-03",
        "delivered_on": "2019-07-05",
        "satisfied_on": "2026-05-13",
        "acquired_on": None,
        "status": "fully-satisfied",
        "charge_kind": "fixed and floating charge over all the company's assets",
        "particulars": "",
        "secured": "",
        "security": "fixed and floating charge over all the company's assets",
        "negative_pledge": True,
        "created_transaction_id": "MzA5OTE3MTc1NmFkaXF6a2N4",
        "satisfied_transaction_id": "MzQ0MDAwMDAwMGFkaXF6a2N4",
        "transaction_ids": ["MzA5OTE3MTc1NmFkaXF6a2N4", "MzQ0MDAwMDAwMGFkaXF6a2N4"],
    }
    assert "kind" not in payload  # would clash with the event's own kind
    assert json.dumps(payload)
    # A satisfied charge links to the satisfaction filing (MR04); an
    # outstanding one to the filing that created it (MR01).
    record = charge_record(charge, NUMBER)
    assert record["link"].endswith(
        "/MzQ0MDAwMDAwMGFkaXF6a2N4/document?format=pdf&download=0"
    )
    assert record["lender"] == "Lloyds Bank PLC"
    assert record["satisfied_on"] == "2026-05-13"
    outstanding = Charge.from_api({**LIVE_ITEM, "status": "outstanding"})
    assert charge_record(outstanding, NUMBER)["link"].endswith(
        "/MzA5OTE3MTc1NmFkaXF6a2N4/document?format=pdf&download=0"
    )
    # Satisfied but the register lists no satisfaction filing: fall back to MR01.
    creation_only = Charge.from_api(
        {**LIVE_ITEM, "transactions": LIVE_ITEM["transactions"][:1]}
    )
    assert charge_record(creation_only, NUMBER)["link"].endswith(
        "/MzA5OTE3MTc1NmFkaXF6a2N4/document?format=pdf&download=0"
    )
    assert charge_record(_charge(), NUMBER)["link"] == charges_page(NUMBER)


def test_ordering_lenders_and_overview() -> None:
    """Charges list newest first; lenders are the distinct ones still owed."""
    a = _charge(
        charge_id="a",
        created_on=date(2020, 1, 1),
        status="outstanding",
        persons_entitled=["A"],
    )
    b = _charge(
        charge_id="b",
        created_on=date(2022, 1, 1),
        status="outstanding",
        persons_entitled=["B"],
    )
    b2 = _charge(
        charge_id="b2",
        created_on=date(2021, 1, 1),
        status="part-satisfied",
        persons_entitled=["B"],
    )
    old = _charge(charge_id="old", status="fully-satisfied", persons_entitled=["Gone"])
    anon = _charge(charge_id="anon", created_on=date(2023, 1, 1), status="outstanding")
    assert [c.charge_id for c in newest_first([a, old, b, anon, b2])] == [
        "anon",
        "b",
        "b2",
        "a",
        "old",
    ]
    assert lenders([a, old, b, anon, b2]) == ["B", "A"]

    company = SimpleNamespace(company_number=NUMBER, charges=None)
    assert charges_overview(company) is None  # type: ignore[arg-type]
    company.charges = SimpleNamespace(data=None)
    assert charges_overview(company) is None  # type: ignore[arg-type]
    company.charges.data = ChargeList(
        total_count=5,
        satisfied_count=1,
        part_satisfied_count=1,
        items=[a, old, b, anon, b2],
    )
    overview = charges_overview(company)  # type: ignore[arg-type]
    assert overview is not None
    assert overview["outstanding"] == 4
    assert overview["total"] == 5
    assert overview["satisfied"] == 1
    assert overview["part_satisfied"] == 1
    assert overview["lenders"] == ["B", "A"]
    assert overview["link"] == charges_page(NUMBER)
    assert [c["charge_code"] for c in overview["charges"]] == [None] * 5
    assert overview["charges"][0]["status"] == "outstanding"


def test_attribute_budget_for_a_company_with_many_charges() -> None:
    """Twenty-five worst-case charges still fit well inside the 16 KB state cap."""
    worst = Charge.from_api(
        {
            **LIVE_ITEM,
            "charge_code": "999999990025",
            "persons_entitled": [
                {
                    "name": "The Governor and Company of the Bank of Somewhere Very Long plc"
                }
            ]
            * 4,
            "more_than_four_persons_entitled": True,
            "particulars": {
                **LIVE_ITEM["particulars"],
                "floating_charge_covers_all": False,
                "description": "A "
                + "very " * 200
                + "long description of the property.",
            },
            "secured_details": {
                "description": "All " + "monies " * 100,
                "type": "obligations-secured",
            },
        }
    )
    company = SimpleNamespace(
        company_number=NUMBER,
        charges=SimpleNamespace(data=ChargeList(total_count=30, items=[worst] * 30)),
    )
    overview = charges_overview(company)  # type: ignore[arg-type]
    assert overview is not None
    assert len(overview["charges"]) == CHARGE_LIST_CAP
    assert len(json.dumps(overview["charges"])) < 16_000


# ---------------------------------------------------------------- the words in a change


def test_describe_charge_changes() -> None:
    """Alerts say who lent, what it is secured on, and link to the filing PDF."""
    created = charge_payload(
        Charge.from_api(
            {
                **LIVE_ITEM,
                "status": "outstanding",
                "satisfied_on": None,
                "secured_details": {
                    "description": "All monies due or to become due from the company",
                    "type": "obligations-secured",
                },
                "transactions": LIVE_ITEM["transactions"][:1],
            }
        )
    )
    title, message, link = describe_change(
        "charge", "created", created, subject="ACME LTD", number=NUMBER
    )
    assert title == "ACME LTD: new charge registered"
    assert message == (
        "A charge in favour of Lloyds Bank PLC was registered on 3 Jul 2019 — "
        "fixed and floating charge over all the company's assets; secures all "
        "monies due or to become due from the company."
    )
    assert link == charge_filing_link(NUMBER, "MzA5OTE3MTc1NmFkaXF6a2N4")

    satisfied = charge_payload(Charge.from_api(LIVE_ITEM))
    title, message, link = describe_change(
        "charge", "satisfied", satisfied, subject="ACME LTD", number=NUMBER
    )
    assert title == "ACME LTD: charge satisfied"
    assert message == (
        "The charge in favour of Lloyds Bank PLC (created 3 Jul 2019) was "
        "satisfied in full on 13 May 2026."
    )
    assert link == charge_filing_link(NUMBER, "MzQ0MDAwMDAwMGFkaXF6a2N4")

    acquired = charge_payload(
        Charge.from_api(
            {
                **LIVE_ITEM,
                "status": "outstanding",
                "acquired_on": "2026-09-01",
                "particulars": {"description": "The land at Example Farm"},
                "transactions": [],
            }
        )
    )
    title, message, link = describe_change(
        "charge", "acquired", acquired, subject="ACME LTD", number=NUMBER
    )
    assert title == "ACME LTD: charge acquired with property"
    assert message == (
        "Property acquired on 1 Sep 2026 came with a charge in favour of "
        "Lloyds Bank PLC (created 3 Jul 2019) — charge over the land at Example Farm."
    )
    assert link == charges_page(NUMBER)  # no filing id known

    part = charge_payload(
        Charge.from_api({**LIVE_ITEM, "status": "part-satisfied", "satisfied_on": None})
    )
    title, message, link = describe_change(
        "charge", "part-satisfied", part, subject="ACME LTD", number=NUMBER
    )
    assert title == "ACME LTD: charge part satisfied"
    assert message == (
        "The charge in favour of Lloyds Bank PLC (created 3 Jul 2019) has been "
        "partly satisfied — fixed and floating charge over all the company's assets."
    )
    assert link == charge_filing_link(NUMBER, "MzQ0MDAwMDAwMGFkaXF6a2N4")

    # A change logged by an older version carries only the basics; it still reads.
    legacy = {
        "persons_entitled": ["Old Bank"],
        "charge_code": "0001",
        "status": "outstanding",
    }
    title, message, link = describe_change(
        "charge", "created", legacy, subject="ACME LTD", number=NUMBER
    )
    assert message == "A charge in favour of Old Bank was registered."
    assert link == charges_page(NUMBER)
    title, message, link = describe_change(
        "charge", "part-satisfied", {}, subject="ACME LTD", number=NUMBER
    )
    assert message == "The charge in favour of a lender has been partly satisfied."
    title, message, link = describe_change(
        "charge", "satisfied", {}, subject="ACME LTD", number=""
    )
    assert message == "The charge in favour of a lender was satisfied in full."
    assert link == ""


def test_change_links_open_the_pdf_then_the_charges_page() -> None:
    """A charge change opens its filing PDF first, with the charges page behind it."""
    change = {
        "link": charge_filing_link(NUMBER, "TX"),
        "links": [{"text": "Charges", "href": charges_page(NUMBER)}],
    }
    assert [x["text"] for x in _change_links(change)] == ["Open PDF", "Charges"]
    assert [x["text"] for x in _change_links({"link": charges_page(NUMBER)})] == [
        "Open"
    ]
    assert _change_links({"link": ""}) == []


def test_charges_line() -> None:
    """The company card names the outstanding charges and the lenders behind them."""
    assert _charges_line(None) == ""
    assert _charges_line({"outstanding": 0, "lenders": ["X"]}) == ""
    assert _charges_line({"outstanding": 1, "lenders": []}) == "1 outstanding charge"
    assert (
        _charges_line({"outstanding": 2, "lenders": ["Lloyds Bank plc", "HSBC UK"]})
        == "2 outstanding charges · Lloyds Bank plc, HSBC UK"
    )
    assert (
        _charges_line({"outstanding": 6, "lenders": ["A", "B", "C", "D", "E"]})
        == "6 outstanding charges · A, B, C and 2 more"
    )
