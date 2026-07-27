from tracker.legislative.classify import classify


def test_tax_classification():
    c = classify("RELATING TO REAL PROPERTY TAXATION.")
    assert "tax" in c.subjects


def test_transportation_classification():
    c = classify(
        "Relating to electric bicycles and bikeways on Oahu."
    )
    assert "transportation" in c.subjects


def test_affordable_housing_classification():
    c = classify(
        "Amending the comprehensive zoning ordinance to allow accessory dwelling units."
    )
    assert "affordable_housing" in c.subjects


def test_food_security_classification():
    c = classify(
        "Establishing a food bank coordination committee.",
        raw_subject="Hunger and nutrition policy",
    )
    assert "food_security" in c.subjects


def test_multi_label():
    c = classify(
        "BILL relating to TOD zoning near rail transit stations and inclusionary affordable housing."
    )
    assert "affordable_housing" in c.subjects
    assert "transportation" in c.subjects


def test_land_use_bills_tagged_housing():
    # Land-use / planning bills whose titles never say "housing" are the common
    # miss; the broadened keyword set should now catch them.
    for title in (
        "PROPOSING AN AMENDMENT TO CHAPTER 21 (THE LAND USE ORDINANCE).",
        "PROPOSING AN AMENDMENT TO CHAPTER 22 (THE SUBDIVISION ORDINANCE).",
        "RELATING TO SIGN STANDARDS FOR APARTMENT AND APARTMENT MIXED-USE DISTRICTS.",
        "ADOPTS THE COUNTY OF HAWAII GENERAL PLAN 2045.",
    ):
        assert "affordable_housing" in classify(title).subjects, title


def test_sma_single_family_permit_not_housing():
    # Coastal individual-permit bills say "single-family dwelling" but are not
    # housing policy — they must NOT be filed under affordable_housing on that
    # alone.
    c = classify(
        "GRANTING A SPECIAL MANAGEMENT AREA MAJOR PERMIT TO ALLOW FOR THE "
        "CONSTRUCTION OF A SINGLE-FAMILY DWELLING."
    )
    assert "affordable_housing" not in c.subjects


def test_sma_permit_still_housing_with_strong_term():
    # The SMA veto only suppresses the weak terms; an explicit housing term
    # still tags it.
    c = classify(
        "SPECIAL MANAGEMENT AREA PERMIT FOR AN AFFORDABLE HOUSING DEVELOPMENT."
    )
    assert "affordable_housing" in c.subjects


def test_hart_and_dts_tagged_transportation():
    # The transit agencies — "rapid transportation" (HART) and "transportation
    # services" (DTS) — were missed by the old keyword set.
    assert "transportation" in classify(
        "RELATING TO THE HONOLULU AUTHORITY FOR RAPID TRANSPORTATION."
    ).subjects
    assert "transportation" in classify(
        "REQUESTING THE DEPARTMENT OF TRANSPORTATION SERVICES TO ACT."
    ).subjects


def test_committee_name_does_not_tag_transportation():
    # The Maui ADEPT committee name carries "Public Transportation", but its
    # items (axis deer, biosecurity) are not transportation — bare
    # "transportation" must not match.
    c = classify(
        "AXIS DEER MITIGATION",
        raw_subject="Agriculture, Diversification, Environment and Public Transportation",
    )
    assert "transportation" not in c.subjects


def test_committee_name_raw_subject_ignored():
    # Maui's raw_subject is the referring committee's name; its keywords must
    # not tag the committee's whole docket. The title alone still classifies.
    c = classify(
        "COUNTY TRANSIT BUS ADVERTISING",
        raw_subject="Agriculture, Diversification, Environment, & Public Transportation Committee",
    )
    assert "food_security" not in c.subjects
    assert "transportation" in c.subjects  # from the title, not the committee

    for committee in (
        "Housing and Land Use Committee (2025-2027)",
        "Special Committee on Real Property Tax Reform",
        "Kōmike Aloha ʻĀina (2025-2027)",
        "Council of the County of Maui ",
    ):
        c = classify("APPROVING A SETTLEMENT AGREEMENT.", raw_subject=committee)
        assert c.subjects == [], committee


def test_acronyms_case_sensitive():
    # \bGET\b etc. used to be compiled IGNORECASE, tagging the verb "get" as
    # tax, "snap" as food_security, the name "Tod" as housing.
    assert "tax" not in classify("Urging the administration to get moving.").subjects
    assert "food_security" not in classify("A resolution to snap into action.").subjects
    assert "affordable_housing" not in classify("Honoring Tod Smith for his service.").subjects
    assert "tax" not in classify("Relating to tit-for-tat enforcement.").subjects
    # Genuine uppercase acronyms still match.
    assert "tax" in classify("Appropriating monies from the GET Fund.").subjects
    assert "food_security" in classify("Supporting SNAP outreach programs.").subjects
    assert "affordable_housing" in classify("Amending the TOD special district rules.").subjects


def test_bike_word_boundaries():
    # The old pattern r"\bbike|bicycle\b" parsed as (\bbike)|(bicycle\b) and
    # matched "motorbicycle"; prefix forms like "bikeway" must still match.
    assert "transportation" not in classify("Relating to motorbicycle racing.").subjects
    assert "transportation" in classify("Establishing a bikeway along the highway.").subjects


def test_bare_development_not_housing():
    # Bare "development" was the largest false-positive source (189 bills):
    # youth campuses, budget amendments, infrastructure plans.
    for title in (
        "OVERVIEW OF PROPOSED COMMUNITY RESILIENCE AND YOUTH DEVELOPMENT CAMPUS",
        "ADOPTING A REVISION TO THE PUBLIC INFRASTRUCTURE MAP FOR THE ʻEWA DEVELOPMENT PLAN AREA.",
    ):
        assert "affordable_housing" not in classify(title).subjects, title
    # Housing-meaning development compounds still count.
    assert "affordable_housing" in classify(
        "APPROVING A RESIDENTIAL DEVELOPMENT AGREEMENT."
    ).subjects


def test_non_substantive_types_unclassified():
    # Committee reports / communications / ceremonial resolutions duplicate or
    # aren't legislation; they carry no subjects even when keywords match.
    title = "Recommending FIRST READING of Bill 76, relating to food trucks."
    for bt in ("Committee Report", "Rule 7(B)", "County Communication",
               "Ceremonial Resolution", "Direct Referral"):
        assert classify(title, bill_type=bt).subjects == [], bt
    # Substantive and unknown types still classify.
    assert classify(title, bill_type="Bill").subjects
    assert classify(title, bill_type=None).subjects
    # The gate gets its answer from matter_class(), which fails open, so a type
    # name no council has used yet is treated as legislation rather than
    # silently dropped from every subject feed.
    for unknown in ("Charter Amendment", "Initiative Petition", "Reso"):
        assert classify(title, bill_type=unknown).subjects, unknown


def test_gift_disclosure_unclassified():
    # Gift-acceptance disclosures carry no policy subject even though their
    # contents mention meals / transportation.
    c = classify(
        "ACCEPTING A GIFT OF TRAVEL, LODGING, MEALS, AND GROUND TRANSPORTATION TO THE CITY."
    )
    assert c.subjects == []


def test_school_meals_food_security():
    assert "food_security" in classify(
        "URGING THE STATE TO ESTABLISH UNIVERSAL FREE SCHOOL MEALS."
    ).subjects


def test_unclassified():
    c = classify("Resolution honoring the retirement of John Smith.")
    assert c.subjects == []


def test_empty_input():
    c = classify(None)
    assert c.subjects == []
    assert c.confidence == 0.0
