from decimal import Decimal

from django.test import TestCase

from sales.services import parse_decimal


class ParseDecimalTests(TestCase):
    def test_parse_decimal_should_handle_money_in_cents_without_100x_scale_error(self):
        self.assertEqual(parse_decimal("R$ 1234,56"), Decimal("1234.56"))
        self.assertEqual(parse_decimal("123456"), Decimal("1234.56"))
        self.assertEqual(parse_decimal(123456), Decimal("1234.56"))
