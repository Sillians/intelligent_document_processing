from __future__ import annotations

import unittest

from fastapi import HTTPException

from shared.idp_common.page_contract import validate_pages


class PageContractTests(unittest.TestCase):
    def test_accepts_ordered_tenant_scoped_pages(self) -> None:
        pages = [
            {"page_number": 1, "preprocessed_key": "tenants/a/jobs/j/preprocessed/page-0001.png"},
            {"page_number": 2, "preprocessed_key": "tenants/a/jobs/j/preprocessed/page-0002.png"},
        ]
        self.assertEqual(len(validate_pages(pages, tenant_id="a", job_id="j", key_field="preprocessed_key", first_key=pages[0]["preprocessed_key"])), 2)

    def test_rejects_missing_middle_page_and_cross_tenant_key(self) -> None:
        for page in (
            {"page_number": 3, "preprocessed_key": "tenants/a/jobs/j/preprocessed/page-0002.png"},
            {"page_number": 2, "preprocessed_key": "tenants/b/jobs/j/preprocessed/page-0002.png"},
        ):
            with self.subTest(page=page), self.assertRaises(HTTPException):
                validate_pages([{"page_number": 1, "preprocessed_key": "tenants/a/jobs/j/preprocessed/page-0001.png"}, page], tenant_id="a", job_id="j", key_field="preprocessed_key")
