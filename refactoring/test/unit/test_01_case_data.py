from stage_01_case_data import _concerns_from_row


def test_customer_scope_is_separate_from_explanation():
    concerns = _concerns_from_row({
        "SecretNarrative": (
            '[{"Issue type": "AML RISK", '
            '"Why it violates policy": "Policy reason", '
            '"Explanation given to auditor": '
            '"This applies to 2 contract(s): SE108426, SE108427. It was urgent."}]'
        )
    })

    assert concerns["AML RISK"]["applies_to_contracts"] == [
        "SE108426", "SE108427"
    ]
    assert concerns["AML RISK"]["explanation_for_auditor"] == "It was urgent."
